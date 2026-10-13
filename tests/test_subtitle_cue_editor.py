"""Production JS regression for per-cue subtitle drafts and project-scoped saves.

Runs with Node in CI. The test drives the actual shipped layout workbench source
and uses deferred backend responses; no screenshot or FFmpeg rendering is claimed.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node required for cue editor behavior tests")
class SubtitleCueEditingTests(unittest.TestCase):
    def test_cue_draft_navigation_and_inflight_save(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text(
            encoding="utf-8"
        )
        start = html.index("let layoutStressSample=null;")
        end = html.index("async function updateLayoutPreview()", start)
        source = html[start:end]
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function node(id){
  if(!elements.has(id)){
    const classSet = new Set(['hidden']);
    elements.set(id,{
      value:'',textContent:'',innerHTML:'',type:'text',
      disabled:false,dataset:{},className:'',
      pause(){},load(){},removeAttribute(){},
      classList:{add:c=>classSet.add(c),remove:c=>classSet.delete(c),contains:c=>classSet.has(c)},
      setAttribute(k,v){this[k]=v}
    });
  }
  return elements.get(id);
}
const subtitles=[
  {id:11,source_text:'First source',final_chs:'第一条',source_member_id:'voice/scene-A/11.wav',start:5,end:9,group:'Scene A'},
  {id:12,source_text:'Second source',final_chs:'第二条',start:10,end:12,group:'Scene B'}
];
const saves=[];
let previewUpdates=0;
let invalidations=0;
const context={
  document:{getElementById:node,querySelectorAll:()=>[],activeElement:null},
  currentProject:{root:'/project-alpha',name:'alpha',config:{}},
  window:{confirm:()=>true},
  escapeHtml:x=>String(x),formatClockTime:n=>'T'+Number(n||0).toFixed(1),
  api:(path,opts)=>{
    if(path.endsWith('/subtitles?selector=all'))return Promise.resolve({subtitles,persistable:true});
    if(path.endsWith('/subtitles'))return new Promise((resolve,reject)=>saves.push({opts,resolve,reject}));
    throw Error('Unexpected request: '+path);
  },
  subtitleSettingsPayload:()=>({base_chs_size:48}),
  updateLayoutPreview:()=>{previewUpdates++},
  scheduleLayoutPreviewUpdate:()=>{previewUpdates++},
  invalidateLibassPreview:()=>{invalidations++},
  shortError:e=>String(e?.message||e),setLog:e=>{throw Error(String(e))},
  console
};
vm.createContext(context);
vm.runInContext(source,context);
const run=x=>vm.runInContext(x,context);
(async()=>{
  await run('loadLayoutStressSample({force:true,selectId:11})');
  assert.equal(node('layoutCueFinalText').value,'第一条');
  assert.equal(node('layoutCueFinalText').readOnly,true);
  assert.equal(node('layoutCueReadOnlyText').textContent,'第一条');
  assert.equal(node('layoutCueSaveBtn').disabled,true);
  assert.equal(node('layoutCueScrubber').disabled,false);

  // The former editable Layout source path must be inert, not a second
  // independently persisted owner. Review is the only write surface.
  run("layoutCueUpdateDraft('不得从排版写入')");
  assert.equal(node('layoutCueFinalText').value,'第一条');
  assert.equal(node('layoutCueReadOnlyText').textContent,'第一条');
  assert.equal(await run('saveLayoutCueText()'),false);
  assert.equal(saves.length,0,'Hidden Layout must never POST a text mutation');

  // Selection, source-time display and project corpus refresh still work.
  await run('loadLayoutStressSample({force:true,selectId:12})');
  assert.equal(node('layoutCueFinalText').value,'第二条');
  await run('loadLayoutStressSample({force:true,selectId:11})');
  assert.equal(node('layoutCueFinalText').value,'第一条');

  // A change made by Review / another session is adopted on refresh.
  subtitles[0].final_chs='校对已经保存的新正文';
  await run('loadLayoutStressSample({force:true,refresh:true,selectId:11})');
  assert.equal(node('layoutCueFinalText').value,'校对已经保存的新正文');
  assert.equal(node('layoutCueReadOnlyText').textContent,'校对已经保存的新正文');
  assert.equal(node('layoutCueSaveBtn').disabled,true);
  assert.equal(saves.length,0);

  node('layoutCueScrubber').value='75';
  run('layoutCueTimePreview()');
  assert.equal(node('layoutCueScrubberTime').textContent,'T8.0 · 75%');

  context.currentProject={root:'/project-beta',name:'beta',config:{}};
  run("layoutCueResetProject('/project-beta')");
  assert.equal(node('layoutCueFinalText').disabled,true);
  assert.equal(node('layoutCueSaveBtn').disabled,true);
  assert.ok(previewUpdates>=3);
  process.stdout.write('layout read-only + cue timing production JS contract: OK\\n');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        script = "const source = " + json.dumps(source, ensure_ascii=False) + ";\n" + harness
        result = subprocess.run(
            ["node", "-"], input=script, text=True, capture_output=True, timeout=15, check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)


if __name__ == "__main__":
    unittest.main()
