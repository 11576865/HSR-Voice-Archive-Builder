"""Regressions for cross-workspace corpus reuse without stale, filtered, or dirty rows."""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "app/static/index.html"


@unittest.skipUnless(shutil.which("node"), "Node required for production source checks")
class SharedSubtitleCorpusTests(unittest.TestCase):
    def test_production_snapshot_accepts_only_live_matching_clean_corpus(self):
        html = HTML.read_text(encoding="utf-8")
        start = html.index("// Shared immutable *complete* subtitle snapshot")
        end = html.index("let layoutStressSample=null;", start)
        source = html[start:end]
        js = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const context={Date:{now:()=>1000}, dirtyDrafts:()=>new Map(), console};
vm.createContext(context);
vm.runInContext(__SOURCE__, context);
const run=x=>vm.runInContext(x,context);
run("rememberReviewSubtitleCorpus('/project-A',{subtitles:[{id:1,final_chs:'original'}],persistable:true,export_status:{state:'current'}})");
const first=run("getReviewSubtitleCorpus('/project-A')");
assert.equal(first.subtitles[0].final_chs,'original');
first.subtitles[0].final_chs='changed-client-copy';
assert.equal(run("getReviewSubtitleCorpus('/project-A')").subtitles[0].final_chs,'original');
assert.equal(run("getReviewSubtitleCorpus('/project-B')"),null);
context.dirtyDrafts=()=>new Map([['1',{}]]);
assert.equal(run("getReviewSubtitleCorpus('/project-A')"),null);
context.dirtyDrafts=()=>new Map();
context.Date.now=()=>20000;
assert.equal(run("getReviewSubtitleCorpus('/project-A')"),null);
context.Date.now=()=>1000;
run("invalidateSharedSubtitleCorpus()");
assert.equal(run("getReviewSubtitleCorpus('/project-A')"),null);
console.log('Complete snapshot TTL / mutation / project / dirty / clone checks: OK');
""".replace("__SOURCE__", json.dumps(source, ensure_ascii=False))
        proc = subprocess.run(["node", "-e", js], capture_output=True,
                              text=True, timeout=15, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_server_response_stale_guard_and_explicit_refresh_fallback(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("mutationGeneration!==sharedSubtitleCorpusMutationGeneration)return;", html)
        self.assertIn("requestGeneration!==subtitleFetchGeneration", html)
        self.assertIn("invalidateSharedSubtitleCorpus();\n    subtitleFetchGeneration++;", html)
        self.assertIn("if(!completeSelection)invalidateSharedSubtitleCorpus()", html)
        self.assertIn("reuseReviewSnapshot:true", html)
        self.assertIn("const source=reuseReviewSnapshot?getReviewSubtitleCorpus(projectKey):null;", html)
        self.assertIn("const data=source||await api(", html)
        self.assertIn("invalidateSharedSubtitleCorpus();\n      const target=loadedSubtitles", html)
        self.assertIn("invalidateSharedSubtitleCorpus();\n    const row=layoutCueRow(id)", html)
        self.assertIn("if(!drafts.size&&completeSelection){", html)


if __name__ == "__main__":
    unittest.main()
