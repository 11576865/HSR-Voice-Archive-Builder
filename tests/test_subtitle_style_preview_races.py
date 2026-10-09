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
        contains: cls => classes.has(cls)
      },
      setAttribute(name, value) { this.attributes[name] = value; }
    });
  }
  return nodes.get(id);
}
const pending = [];
const madeUrls = [];
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
async function settle() { await Promise.resolve(); await Promise.resolve(); }
function complete() {
  const resolve = pending.shift();
  assert.ok(resolve, 'expected a pending backend request');
  resolve({ok:true,blob:async()=>({size:100})});
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

  // Request 2: a current, successful backend result is marked as actual libass.
  const current = vm.runInContext('renderLibassPreview()', sandbox);
  complete();
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
  process.stdout.write('Subtitle preview identity regression: OK\n');
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
