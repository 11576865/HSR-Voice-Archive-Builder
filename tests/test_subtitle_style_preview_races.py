"""Browser-preview identity regression using the same source JS as production.

Node is available on hosted GitHub Actions runners. The test skips in Python-only
deployments such as a minimal Termux installation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is needed for the JS state-machine regression")
class SubtitlePreviewIdentityTests(unittest.TestCase):
    def test_real_scheduler_is_independent_of_geometry_and_works_in_effects(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "app/static" / "index.html").read_text(
            encoding="utf-8"
        )
        start = html.index("function queueLiveRealPreview(){")
        end = html.index("let geometryPreviewGeneration = 0;", start)
        scheduling_source = html[start:end]
        schedule_start = html.index("function scheduleLayoutPreviewUpdate()")
        schedule_end = html.index("function renderPreviewCanvasSvg(", schedule_start)
        invalidation_source = html[schedule_start:schedule_end]
        update_start = html.index("async function updateLayoutPreview()")
        update_end = html.index("function scheduleLayoutPreviewUpdate()", update_start)
        update_source = html[update_start:update_end]
        # The cue's true-render request cannot wait for the best-effort geometry
        # endpoint or FontFace loading. Check ordering as well as state logic.
        self.assertLess(
            update_source.index("queueLiveRealPreview();"),
            update_source.index("await Promise.allSettled(["),
        )
        self.assertLess(
            invalidation_source.index("queueLiveRealPreview();"),
            invalidation_source.index("\n}"),
        )
        self.assertIn("['layout','effects'].includes(document.body.dataset.workspaceTask)",
                      scheduling_source)
        # Exercise the actual production scheduler, not a duplicate JS mock.
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let invocationCount = 0, active = null;
const state = {
  currentProject: {name: 'fixture'},
  lastRenderedAssIdentity: null,
  liveRealPreviewArmed: true,
  liveRealPreviewTimer: null,
  fontImportTransactionActive: false,
  document: {body: {dataset: {workspace: 'layout', workspaceTask: 'layout'}}},
  currentLibassPreviewIdentity: () => 'cue-1',
  renderLibassPreview: () => {invocationCount++; return Promise.resolve();},
  videoPreviewController: null,
  clearTimeout: () => { active = null; },
  setTimeout: (callback, ms) => { assert.equal(ms, 850); active = callback; return 1; }
};
vm.createContext(state);
vm.runInContext(source, state);
for (const task of ['layout','effects']) {
  state.document.body.dataset.workspaceTask=task;
  vm.runInContext('queueLiveRealPreview()',state);
  assert.equal(typeof active,'function',task+' should schedule real preview');
  active();
  assert.equal(invocationCount,task==='layout'?1:2);
}
state.document.body.dataset.workspace='project';
vm.runInContext('queueLiveRealPreview()',state);
assert.equal(active,null,'leaving Layout must stop true preview scheduling');
state.document.body.dataset.workspace='layout';
state.document.body.dataset.workspaceTask='layout';
state.lastRenderedAssIdentity='cue-1';
vm.runInContext('queueLiveRealPreview()',state);
assert.equal(typeof active,'function');
active();
assert.equal(invocationCount,2,'decoded matching frame must suppress duplicate work');
process.stdout.write('Live libass scheduler decoupled from geometry: OK\n');
"""
        script = "const source = " + json.dumps(scheduling_source, ensure_ascii=False) + ";\n" + harness
        completed = subprocess.run(
            ["node", "-"],
            input=script,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)

    def test_geometry_glyphs_are_explicit_diagnostics_only(self) -> None:
        html=(Path(__file__).resolve().parents[1]/"app/static/index.html").read_text("utf-8")
        start=html.index("function setPreviewEvidence(mode, detail = '') {")
        end=html.index("function invalidateLibassPreview()",start)
        code=html[start:end]
        harness=r"""
const vm=require('node:vm'), assert=require('node:assert/strict');
const nodes=new Map();
function node(id){
 if(!nodes.has(id)){
  const classes=new Set();
  nodes.set(id,{dataset:{},textContent:'',attributes:{},
   classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),
    contains:x=>classes.has(x),toggle:(x,hidden)=>hidden?classes.add(x):classes.delete(x)},
   setAttribute(k,v){this.attributes[k]=v;}
  });
 }
 return nodes.get(id);
}
const ctx={document:{getElementById:node,querySelector:()=>node('evidence')},
  geometryExplicitlyRequested:false};
vm.createContext(ctx); vm.runInContext(source,ctx);
for(const mode of ['geometry','rendering','stale','failed','libass']){
 ctx.setPreviewEvidence(mode);
 assert.equal(node('previewSvg').classList.contains('hidden'),true,mode);
}
ctx.geometryExplicitlyRequested=true;
ctx.setPreviewEvidence('geometry');
assert.equal(node('previewSvg').classList.contains('hidden'),false);
ctx.setPreviewEvidence('rendering');
assert.equal(node('previewSvg').classList.contains('hidden'),false);
process.stdout.write('Guides remain transparent overlay on real pixels: OK\n');
"""
        script="const source = "+json.dumps(code,ensure_ascii=False)+";\n"+harness
        p=subprocess.run(["node","-"],input=script,capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stdout+"\n"+p.stderr)

    def test_user_video_preempts_auto_still_render(self) -> None:
        html=(Path(__file__).resolve().parents[1]/"app/static/index.html").read_text("utf-8")
        block=html[html.index("async function renderHsrEffectVideo(){"):][:450]
        self.assertLess(block.index("clearTimeout(liveRealPreviewTimer)"),
                        block.index("clearVideoPreview()"))
        self.assertLess(block.index("liveRealPreviewArmed=false;"),
                        block.index("clearVideoPreview()"))
        scheduler=html[html.index("function queueLiveRealPreview(){"):
                       html.index("let geometryPreviewGeneration = 0;")]
        self.assertIn("if(typeof videoPreviewController!=='undefined'&&videoPreviewController)return;",scheduler)
        self.assertIn("!videoPreviewController&&",scheduler)

    def test_stale_real_frames_and_instant_before_after_comparison(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text("utf-8")
        start = html.index("let previewDebounceTimer = null;")
        end = html.index("async function loadWordAlignmentDiagnostics(){", start)
        production = html[start:end]
        harness = r"""
const vm=require('node:vm'), assert=require('node:assert/strict');
const nodes=new Map(),pending=[],decodes=[],urls=[],revoked=[];
function node(id){
  if(!nodes.has(id)){
    const classes=new Set(['hidden']);
    nodes.set(id,{value:'',textContent:'',disabled:false,className:'',
      dataset:{},style:{},attributes:{},src:'',
      classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),
        contains:x=>classes.has(x),
        toggle:(x,force)=>{const v=force===undefined?!classes.has(x):!!force;
           if(v)classes.add(x);else classes.delete(x);return v;}},
      setAttribute(k,v){this.attributes[k]=v;},
      removeAttribute(k){if(k==='src')this.src='';}
    });
  }
  return nodes.get(id);
}
const ctx={
 document:{getElementById:node,querySelector:()=>node('evidence')},
 currentProject:{name:'project',config:{}},layoutStressSample:null,
 subtitleSettingsPayload:()=>({chs_font:'Fixture',base_chs_size:Number(node('prevChsSize').value||48)}),
 fetch:()=>new Promise(r=>pending.push(r)),headers:()=>({}),
 shortError:e=>String(e.message||e),setLog:()=>{},
 updateLayoutPreview:()=>Promise.resolve(),
 Image:class{decode(){return new Promise((resolve,reject)=>decodes.push({resolve,reject}));}},
 URL:{createObjectURL:()=>{const u='blob:frame-'+(urls.length+1);urls.push(u);return u;},
   revokeObjectURL:u=>revoked.push(u)},
 console,setTimeout,clearTimeout
};
vm.createContext(ctx);vm.runInContext(source,ctx);
const settle=()=>new Promise(r=>setImmediate(r));
function respond(){const f=pending.shift();assert.ok(f,'pending FFmpeg response');
 f({ok:true,blob:async()=>({size:300,type:'image/png'})});}
(async()=>{
 const obsolete=vm.runInContext('renderLibassPreview()',ctx);
 node('prevChsText').value='cue one';
 vm.runInContext('invalidateLibassPreview()',ctx);
 respond();await obsolete;
 assert.equal(urls.length,0,'obsolete network reply must not create blob');

 const first=vm.runInContext('renderLibassPreview()',ctx);
 respond();await settle();
 assert.equal(decodes.length,1);
 assert.equal(node('libassPreviewImage').classList.contains('hidden'),true);
 decodes.shift().resolve();await first;
 assert.equal(node('evidence').dataset.previewEvidence,'libass');
 assert.equal(node('libassPreviewImage').src,'blob:frame-1');

 node('prevChsSize').value='58';
 vm.runInContext('invalidateLibassPreview()',ctx);
 assert.equal(node('libassPreviewImage').classList.contains('hidden'),false,
     'old genuine frame remains visible with stale badge');
 assert.equal(node('evidence').dataset.previewEvidence,'stale');
 const second=vm.runInContext('renderLibassPreview()',ctx);
 vm.runInContext('showGeometryPreview()',ctx);
 assert.equal(node('previewSvg').classList.contains('hidden'),false,
   'guides overlay live preview without cancelling FFmpeg');
 respond();await settle();decodes.shift().resolve();await second;
 assert.equal(node('evidence').dataset.previewEvidence,'libass');
 assert.equal(node('libassPreviewImage').src,'blob:frame-2');
 assert.equal(node('previewCompareBtn').disabled,false);
 vm.runInContext('toggleRealFrameComparison()',ctx);
 assert.equal(node('libassCompareImage').src,'blob:frame-1');
 assert.equal(node('libassCompareImage').classList.contains('hidden'),false);
 vm.runInContext('toggleRealFrameComparison()',ctx);
 assert.equal(node('libassCompareImage').classList.contains('hidden'),true);

 node('prevChsSize').value='65';
 vm.runInContext('invalidateLibassPreview()',ctx);
 const invalid=vm.runInContext('renderLibassPreview()',ctx);
 respond();await settle();decodes.shift().reject(new Error('Invalid PNG'));
 await invalid;
 assert.equal(node('evidence').dataset.previewEvidence,'failed');
 assert.equal(node('libassPreviewImage').classList.contains('hidden'),false,
   'failed render retains last good frame but never accepts it as new proof');

 node('prevChsText').value='different cue';
 vm.runInContext('invalidateLibassPreview()',ctx);
 assert.equal(node('libassPreviewImage').classList.contains('hidden'),true);
 assert.equal(node('previewCompareBtn').disabled,true);
 assert.ok(revoked.includes('blob:frame-1')&&revoked.includes('blob:frame-2'),
   'switching cue releases both stale frame URLs');
 process.stdout.write('Stale true frames, visual compare and cue-safe memory: OK\n');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        script = "const source=" + json.dumps(production, ensure_ascii=False) + ";\n" + harness
        p = subprocess.run(["node", "-"], input=script, text=True,
                           capture_output=True, timeout=15, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + "\n" + p.stderr)


if __name__ == "__main__":
    unittest.main()
