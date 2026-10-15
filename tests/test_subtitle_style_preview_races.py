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
  document: {body: {dataset: {workspace: 'layout', workspaceTask: 'layout'}}},
  currentLibassPreviewIdentity: () => 'cue-1',
  renderLibassPreview: () => {invocationCount++; return Promise.resolve();},
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
  assert.equal(node('evidence-root').dataset.previewEvidence, 'geometry');

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
