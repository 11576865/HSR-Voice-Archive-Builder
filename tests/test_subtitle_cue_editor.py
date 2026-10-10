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
  escapeHtml:x=>String(x),formatClockTime:n=>'T'+Number(n||0).toFixed(1),
  api:(path,opts)=>{
    if(path.endsWith('/subtitles?selector=all'))return Promise.resolve({subtitles,persistable:true});
    if(path.endsWith('/subtitles'))return new Promise(resolve=>saves.push({opts,resolve}));
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
  assert.equal(node('layoutCueStart').textContent,'T5.0');
  assert.equal(node('layoutCueEnd').textContent,'T9.0');
  assert.equal(node('layoutCueScrubber').disabled,false);
  run("layoutCueUpdateDraft('第一条未保存')");
  assert.equal(node('prevChsText').value,'第一条未保存');
  assert.equal(node('layoutCueSaveBtn').disabled,false);
  assert.equal(node('layoutCorpusList').innerHTML.includes('未保存正文'),true);

  await run('loadLayoutStressSample({force:true,selectId:12})');
  assert.equal(node('layoutCueFinalText').value,'第二条');
  await run('loadLayoutStressSample({force:true,selectId:11})');
  assert.equal(node('layoutCueFinalText').value,'第一条未保存','cue draft survives navigation');

  const first=run('saveLayoutCueText()');
  assert.equal(saves.length,1);
  const sent=JSON.parse(saves[0].opts.body);
  assert.equal(sent.expected_project_root,'/project-alpha');
  assert.deepEqual(sent.subtitles,[{
    id:11,final_chs:'第一条未保存',
    expected_final_chs:'第一条',
    expected_source_member_id:'voice/scene-A/11.wav'
  }]);
  assert.equal(await run('saveLayoutCueText()'),false,'save single flight');
  run("layoutCueUpdateDraft('保存期间继续编辑')");
  saves.shift().resolve({ok:true,result:{updated_count:1,ass_error:''}});
  assert.equal(await first,false,'subsequent draft must remain dirty');
  assert.equal(node('layoutCueFinalText').value,'保存期间继续编辑');
  assert.equal(node('layoutCueSaveBtn').disabled,false);
  assert.equal(subtitles[0].final_chs,'第一条未保存','saved old snapshot is kept separate');

  const next=run('saveLayoutCueText()');
  assert.equal(saves.length,1);
  assert.equal(JSON.parse(saves[0].opts.body).subtitles[0].final_chs,'保存期间继续编辑');
  assert.equal(JSON.parse(saves[0].opts.body).subtitles[0].expected_final_chs,'第一条未保存');
  saves.shift().resolve({ok:true,result:{updated_count:1,ass_error:''}});
  assert.equal(await next,true);
  assert.equal(node('layoutCueSaveBtn').disabled,true);
  assert.equal(subtitles[0].final_chs,'保存期间继续编辑');

  run("layoutCueUpdateDraft('试验修改')");
  run('layoutCueRevertDraft()');
  assert.equal(node('layoutCueFinalText').value,'保存期间继续编辑');
  assert.equal(node('layoutCueSaveBtn').disabled,true);

  node('layoutCueScrubber').value='75';
  run('layoutCueTimePreview()');
  assert.equal(node('layoutCueScrubberTime').textContent,'T8.0 · 75%');

  // Project switching discards old project drafts and resets the selected editor.
  context.currentProject={root:'/project-beta',name:'beta',config:{}};
  run("layoutCueResetProject('/project-beta')");
  assert.equal(node('layoutCueFinalText').disabled,true);
  assert.equal(node('layoutCueSaveBtn').disabled,true);
  assert.ok(previewUpdates>=3);
  assert.ok(invalidations>=2);
  process.stdout.write('cue editor production JS contract: OK\n');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        script = "const source = " + json.dumps(source, ensure_ascii=False) + ";\n" + harness
        result = subprocess.run(
            ["node", "-"], input=script, text=True, capture_output=True, timeout=15, check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)


if __name__ == "__main__":
    unittest.main()
