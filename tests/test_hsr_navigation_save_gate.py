"""Production JS regression: Review must save before entering Layout/Effects."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js required for production navigation regression")
class NavigationSaveGateTests(unittest.TestCase):
    def test_failed_and_stale_save_cannot_switch_workspaces(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        begin = html.index("let workspaceNavigationGeneration=0;")
        end = html.index("for(const button of workspaceButtons)button.addEventListener", begin)
        production = html[begin:end]
        script = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = __SOURCE__;
let active = {id:11, _dirty:true, _saving:false};
const state = {
  doc: {workspace:'review',workspaceTask:'review'},
  project:{root:'/fixture/a',name:'alpha'},
  frames:[],pending:[],loads:[],messages:[],mode:'', panel:'', invalidated:0,
  buttons:[{dataset:{workspaceTarget:'review'},setAttribute(){},removeAttribute(){}},
           {dataset:{workspaceTarget:'layout'},setAttribute(){},removeAttribute(){}},
           {dataset:{workspaceTarget:'effects'},setAttribute(){},removeAttribute(){}}],
};
const context = {
  document:{body:{dataset:state.doc}},
  currentProject:state.project,
  currentSubtitle:()=>active,
  syncEditorToMemory:()=>{},
  saveCurrentSubtitle:()=>new Promise(resolve=>state.pending.push(resolve)),
  workspaceButtons:state.buttons,
  autoPinnedPanelKey:null, panelCollapseMode:'manual',
  updatePanelTiers:x=>{state.panel=x},
  requestAnimationFrame:fn=>state.frames.push(fn),
  setHsrStudioMode:x=>{state.mode=x},
  setHsrStudioFocusMode:()=>{}, // Focus-mode UI is tested in real Chromium separately.
  updateLayoutPreview:()=>{},
  loadLayoutStressSample:async options=>{state.loads.push(options)},
  setSubtitleSaveUi:(status,message)=>state.messages.push([status,message]),
  shortError:x=>String(x?.message||x),
  setLog:e=>state.messages.push(['log',String(e)]),
  layoutSampleRequestGeneration:0,
  window:{scrollTo(){}},console
};
vm.createContext(context);
vm.runInContext(source,context);
const execute=x=>vm.runInContext(x,context);
const settle=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));};
const drain=async()=>{const callbacks=state.frames.splice(0);for(const callback of callbacks)await callback();};
(async()=>{
  // Failure cannot expose a stale preview before server persistence.
  execute("showWorkspace('effects')");
  assert.equal(state.doc.workspace,'review');
  await settle();
  assert.equal(state.pending.length,1);
  state.pending.shift()(false);
  await settle();
  assert.equal(state.doc.workspace,'review');
  assert.equal(state.loads.length,0);
  assert.equal(state.messages.at(-1)[0],'error');

  // A successful save transitions only after the edited row is clean.
  execute("showWorkspace('effects')");
  await settle();
  active._dirty=false;
  state.pending.shift()(true);
  await settle();
  assert.equal(state.doc.workspace,'layout');
  assert.equal(state.doc.workspaceTask,'effects');
  assert.equal(state.mode,'effects');
  await drain();
  assert.equal(state.loads.length,1);
  assert.equal(state.loads[0].selectId,11);
  assert.equal(state.loads[0].refresh,true);

  // Leaving a pending load invalidates the previous request generation.
  execute("showWorkspace('review')");
  assert.equal(execute("layoutSampleRequestGeneration"),1);
  active._dirty=true;
  execute("showWorkspace('layout')");
  await settle();
  // User navigates somewhere else before the in-flight save resolves.
  execute("showWorkspace('export')");
  assert.equal(state.doc.workspace,'export');
  active._dirty=false;
  state.pending.shift()(true);
  await settle();
  assert.equal(state.doc.workspace,'export');
  assert.equal(state.loads.length,1);

  // Even a clean visit's queued requestAnimationFrame is invalidated on exit.
  execute("showWorkspace('review')");
  execute("showWorkspace('effects')");
  execute("showWorkspace('project')");
  await drain();
  assert.equal(state.loads.length,1);

  console.log('PASS: guarded Review transitions and obsolete request invalidation');
})().catch(err=>{console.error(err);process.exit(1)});
""".replace("__SOURCE__", json.dumps(production))
        result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True,
                                capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_hidden_legacy_layout_text_writer_is_not_wired_to_ui(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn("function layoutCueState(sub)", html)
        self.assertIn("const persisted=String(sub.final_chs??'')", html)
        self.assertIn('id="layoutCueReadOnlyText"', html)
        for removed in ("layoutCueFinalText", "saveLayoutCueText",
                        "layoutCueUpdateDraft", "layoutCueSaveBtn"):
            self.assertNotIn(removed, html)

    def test_review_writer_carries_optimistic_lock_and_fixed_project_identity(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        begin = html.index("async function saveSubtitleItemInBackground(")
        end = html.index("async function saveCurrentSubtitle(", begin)
        writer = html[begin:end]
        self.assertIn("const expectedText=String(targetSub._savedFinal??'')", writer)
        self.assertIn("const expectedMember=String(targetSub.source_member_id||'')", writer)
        self.assertIn("const projectName=currentProject.name||'active'", writer)
        self.assertIn("const projectRoot=currentProject.root||null", writer)
        self.assertIn("expected_project_root:projectRoot", writer)
        self.assertIn("expected_final_chs:expectedText", writer)
        self.assertIn("expected_source_member_id:expectedMember", writer)
        self.assertIn("project context changed", writer)



if __name__ == "__main__":
    unittest.main()
