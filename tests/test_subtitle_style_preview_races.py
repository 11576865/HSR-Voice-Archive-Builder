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
    def test_real_scheduler_skips_layout_and_keeps_effects(self) -> None:
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
        # Geometry updates must not trigger real ASS in Layout; the previous
        # independent Effects scheduler remains unchanged.
        self.assertLess(
            update_source.index("queueLiveRealPreview();"),
            update_source.index("await Promise.allSettled(["),
        )
        self.assertLess(
            invalidation_source.index("queueLiveRealPreview();"),
            invalidation_source.index("\n}"),
        )
        self.assertIn("if(document.body.dataset.workspaceTask==='layout')return;", scheduling_source)
        self.assertIn("['layout','effects'].includes(document.body.dataset.workspaceTask)",
                      scheduling_source)
        self.assertIn("document.getElementById('previewLibassBtn')?.addEventListener('click',renderLibassPreview)",html)
        self.assertIn("geometryExplicitlyRequested=true;",invalidation_source)
        self.assertIn("liveRealPreviewArmed=false;",invalidation_source)
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
state.document.body.dataset.workspaceTask='layout';
vm.runInContext('queueLiveRealPreview()',state);
assert.equal(active,null,'Layout must never automatically invoke FFmpeg/libass');
assert.equal(invocationCount,0);
state.document.body.dataset.workspaceTask='effects';
vm.runInContext('queueLiveRealPreview()',state);
assert.equal(typeof active,'function','Effects scheduler should remain functional');
active();
assert.equal(invocationCount,1);
state.lastRenderedAssIdentity='cue-1';
vm.runInContext('queueLiveRealPreview()',state);
assert.equal(typeof active,'function');
active();
assert.equal(invocationCount,1,'decoded matching frame must suppress duplicate work');
state.document.body.dataset.workspace='project';
vm.runInContext('queueLiveRealPreview()',state);
assert.equal(active,null,'leaving workbench must stop true preview scheduling');
state.document.body.dataset.workspace='layout';
state.document.body.dataset.workspaceTask='layout';
state.lastRenderedAssIdentity=null;
vm.runInContext('queueLiveRealPreview()',state);
assert.equal(active,null,'Even an invalidated Layout frame must not auto-render');
process.stdout.write('Layout manual-only preview and Effects scheduler: OK\n');
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
assert.equal(node('previewSvg').classList.contains('hidden'),true);
process.stdout.write('Only manual geometry may show non-authoritative glyphs: OK\n');
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

    def test_stale_libass_responses_and_manual_geometry_choice(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "app/static" / "index.html").read_text(
            encoding="utf-8"
        )
        start = html.index("let previewDebounceTimer = null;")
        end = html.index("async function loadWordAlignmentDiagnostics(){", start)
        production_js = html[start:end]

        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) {
    const classes = new Set(['hidden']);
    nodes.set(id, {
      value: '',
      textContent: '',
      className: '',
      disabled: false,
      dataset: {},
      style: {},
      attributes: {},
      classList: {
        add: cls => classes.add(cls),
        remove: cls => classes.delete(cls),
        contains: cls => classes.has(cls),
        toggle: (cls,force) => { const add=force===undefined?!classes.has(cls):!!force; if(add)classes.add(cls);else classes.delete(cls);return add; }
      },
      setAttribute(name, value) { this.attributes[name] = value; }
    });
  }
  return nodes.get(id);
}
const pending = [];
const madeUrls = [];
const decodes = [];
const sandbox = {
  document: {
    getElementById: node,
    querySelector: () => node('evidence-root')
  },
  currentProject: {name:'fixture-project', config:{}},
  layoutStressSample: null,
  subtitleSettingsPayload: () => ({chs_font:'Fixture', base_chs_size:48}),
  fetch: () => new Promise(resolve => pending.push(resolve)),
  headers: () => ({}),
  shortError: e => String(e.message || e),
  setLog: () => {},
  URL: {
    createObjectURL: () => { const url = 'blob:fixture-' + (madeUrls.length + 1); madeUrls.push(url); return url; },
    revokeObjectURL: () => {}
  },
  console,
  setTimeout,
  clearTimeout
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
node('libassPreviewImage').decode = () => new Promise((resolve,reject)=>decodes.push({resolve,reject}));
async function settle() { await new Promise(resolve=>setImmediate(resolve)); }
function complete() {
  const resolve = pending.shift();
  assert.ok(resolve, 'expected a pending backend request');
  resolve({ok:true,blob:async()=>({size:100,type:'image/png'})});
}
(async () => {
  // Request 1: edit occurs before its response, so it cannot publish an old frame.
  const old = vm.runInContext('renderLibassPreview()', sandbox);
  assert.equal(node('evidence-root').dataset.previewEvidence, 'rendering');
  node('prevChsText').value = 'changed';
  vm.runInContext('invalidateLibassPreview()', sandbox);
  complete();
  await old;
  assert.equal(madeUrls.length, 0, 'obsolete response must not allocate a new image URL');
  assert.equal(node('libassPreviewImage').classList.contains('hidden'), true);
  assert.equal(node('evidence-root').dataset.previewEvidence, 'rendering');

  // Request 2: transport success is not yet decoded-image success.
  const current = vm.runInContext('renderLibassPreview()', sandbox);
  complete();
  await settle();
  assert.equal(decodes.length, 1);
  assert.equal(node('evidence-root').dataset.previewEvidence, 'rendering');
  assert.equal(node('libassPreviewImage').classList.contains('hidden'), true);
  decodes.shift().resolve();
  await current;
  assert.equal(node('evidence-root').dataset.previewEvidence, 'libass');
  assert.equal(node('libassPreviewImage').classList.contains('hidden'), false);
  assert.equal(node('previewLibassBtn').attributes['aria-pressed'], 'true');

  // Request 3: choosing Geometry while rendering wins over late success.
  const racing = vm.runInContext('renderLibassPreview()', sandbox);
  vm.runInContext('showGeometryPreview()', sandbox);
  complete();
  await racing;
  assert.equal(node('evidence-root').dataset.previewEvidence, 'geometry');
  assert.equal(node('libassPreviewImage').classList.contains('hidden'), true);
  assert.equal(madeUrls.length, 1, 'manual geometry choice must suppress stale frame publishing');

  // Request 4: a non-decodable image must never claim renderer evidence.
  const invalid = vm.runInContext('renderLibassPreview()', sandbox);
  complete();
  await settle();
  assert.equal(node('evidence-root').dataset.previewEvidence, 'rendering');
  decodes.shift().reject(new Error('Invalid PNG bytes'));
  await invalid;
  assert.equal(node('evidence-root').dataset.previewEvidence, 'failed');
  assert.equal(node('libassPreviewImage').classList.contains('hidden'), true);
  assert.equal(node('previewLibassBtn').disabled, false);
  process.stdout.write('Subtitle preview identity and image decode regression: OK\n');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        script = "const source = " + json.dumps(production_js, ensure_ascii=False) + ";\n" + harness
        completed = subprocess.run(
            ["node", "-"],
            input=script,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stdout + "\n" + completed.stderr
        )


if __name__ == "__main__":
    unittest.main()
