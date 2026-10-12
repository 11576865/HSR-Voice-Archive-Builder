"""Production-JS timing edit workflow: draft ownership, project guard and save races."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node required for production-JS timing editor regression")
class SubtitleDisplayTimingEditorTests(unittest.TestCase):
    def test_edit_revert_save_reset_and_late_response(self):
        html=(Path(__file__).resolve().parents[1]/"app/static/index.html").read_text(encoding="utf-8")
        start=html.index("let layoutStressSample=null;")
        end=html.index("async function updateLayoutPreview()",start)
        source=html[start:end]
        harness=r"""
const vm=require('node:vm');
const assert=require('node:assert/strict');
const nodes=new Map();
function node(id){
  if(!nodes.has(id)){
    const hidden=new Set(['hidden']);
    nodes.set(id,{
      value:'',type:'text',textContent:'',innerHTML:'',className:'',dataset:{},disabled:false,
      pause(){},load(){},removeAttribute(){},
      setAttribute(k,v){this[k]=v},
      classList:{add:x=>hidden.add(x),remove:x=>hidden.delete(x)}
    });
  }
  return nodes.get(id);
}
const project={root:'/project-a',name:'project-a',config:{}};
const subtitles=[
  {id:11,source_text:'Hello',final_chs:'你好',start:2,end:4,source_start:2,source_end:4,timing_modified:false},
  {id:12,source_text:'Another',final_chs:'第二条',start:5,end:7,source_start:5,source_end:7,timing_modified:false}
];
const requests=[];
let invalidations=0;
const sandbox={
  document:{getElementById:node,querySelectorAll:()=>[],activeElement:null},
  currentProject:project,
  escapeHtml:x=>String(x),
  formatClockTime:n=>String(n),
  api:(path,opt)=>{
    if(path.endsWith('/subtitles?selector=all'))return Promise.resolve({subtitles,persistable:true});
    if(path.endsWith('/timing')||path.endsWith('/subtitles/artifacts/refresh'))
      return new Promise(resolve=>requests.push({path,payload:JSON.parse(opt.body),resolve}));
    throw Error('Unexpected request '+path);
  },
  subtitleSettingsPayload:()=>({base_chs_size:48}),
  updateLayoutPreview:()=>{},
  scheduleLayoutPreviewUpdate:()=>{},
  invalidateLibassPreview:()=>{invalidations++},
  shortError:e=>String(e?.message||e),
  setLog:e=>{throw Error(String(e))},
  console
};
vm.createContext(sandbox);
vm.runInContext(source,sandbox);
const act=x=>vm.runInContext(x,sandbox);
(async()=>{
  await act('loadLayoutStressSample({force:true,selectId:11})');
  assert.equal(node('layoutCueTimingStart').value,'2');
  assert.equal(node('layoutCueTimingEnd').value,'4');
  act("layoutCueUpdateTiming('start','2.1')");
  assert.equal(node('layoutCueTimingSaveBtn').disabled,false);
  assert.match(node('layoutCueTimingStatus').textContent,/尚未保存/);
  await act('loadLayoutStressSample({force:true,selectId:12})');
  await act('loadLayoutStressSample({force:true,selectId:11})');
  assert.equal(node('layoutCueTimingStart').value,'2.1','time draft survives cue switching');

  const first=act('saveLayoutCueTiming()');
  assert.equal(requests.length,1);
  assert.equal(requests[0].payload.expected_project_root,'/project-a');
  assert.equal(requests[0].payload.expected_start,2);
  assert.equal(requests[0].payload.start,2.1);
  act("layoutCueUpdateTiming('start','2.2')");
  requests.shift().resolve({timing:{id:'11',start:2.1,end:4,timing_modified:true},refreshed:{ass_error:''}});
  assert.equal(await first,false,'newer time draft must not be marked saved');
  assert.equal(node('layoutCueTimingSaveBtn').disabled,false);
  assert.equal(subtitles[0].start,2.1,'server-saved effective time is reflected in corpus');
  assert.equal(node('layoutCueTimingStart').value,'2.2','newer user draft remains');

  const second=act('saveLayoutCueTiming()');
  assert.equal(requests[0].payload.expected_start,2.1);
  requests.shift().resolve({timing:{id:'11',start:2.2,end:4,timing_modified:true},refreshed:{ass_error:''}});
  assert.equal(await second,true);
  assert.equal(node('layoutCueTimingSaveBtn').disabled,true);
  act("layoutCueUpdateTiming('end','3.5')");
  act('layoutCueRevertTimingDraft()');
  assert.equal(node('layoutCueTimingEnd').value,'4');
  assert.equal(node('layoutCueTimingSaveBtn').disabled,true);

  const reset=act('saveLayoutCueTiming({reset:true})');
  assert.equal(requests[0].payload.reset,true);
  requests.shift().resolve({timing:{id:'11',start:2,end:4,timing_modified:false},refreshed:{ass_error:''}});
  await reset;
  assert.equal(subtitles[0].start,2);
  assert.equal(node('layoutCueTimingResetBtn').disabled,false,'reset needs to show uncommitted draft if it differs');

  // An archived source member can change on rebuild: an old display override
  // must be explicitly cleared instead of applied to the new audio clock.
  subtitles[1].timing_conflict=true;
  subtitles[1].timing_modified=true;
  await act('loadLayoutStressSample({force:true,selectId:12})');
  assert.match(node('layoutCueTimingBadge').textContent,/冲突/);
  assert.equal(node('layoutCueTimingResetBtn').disabled,false);
  assert.equal(await act('saveLayoutCueTiming()'),false,'non-reset conflict write must fail');
  const recovery=act('saveLayoutCueTiming({reset:true})');
  assert.equal(requests.length,1,'stale source reset must make a real backend request');
  assert.equal(requests[0].payload.reset,true);
  requests.shift().resolve({timing:{id:'12',start:5,end:7,timing_modified:false},refreshed:{ass_error:''}});
  assert.equal(await recovery,true);
  assert.equal(subtitles[1].timing_conflict,false);

  act("layoutCueUpdateTiming('end','7.2')");
  const partial=act('saveLayoutCueTiming()');
  assert.equal(requests.length,1);
  requests.shift().resolve({
    saved:true,artifacts_current:false,artifact_error:'synthetic ASS export failure',
    timing:{id:'12',start:5,end:7.2,timing_modified:true},
    refreshed:{ass_error:'synthetic ASS export failure'}
  });
  assert.equal(await partial,true,'time edit really committed although export failed');
  assert.equal(subtitles[1].end,7.2);
  assert.equal(node('layoutCueTimingSaveBtn').disabled,true,'do not ask to re-save the committed timing');
  assert.equal(node('layoutCueTimingRetryExportBtn').hidden,false);
  assert.match(node('layoutCueTimingStatus').textContent,/已保存/);
  const retry=act('layoutCueRetryArtifactExport()');
  assert.equal(requests.length,1,'retry must be a separate endpoint');
  assert.match(requests[0].path,/subtitles\/artifacts\/refresh$/);
  assert.equal(requests[0].payload.expected_project_root,'/project-a');
  assert.equal(requests[0].payload.force_ass,true);
  requests.shift().resolve({
    artifacts_current:true,artifact_error:'',refreshed:{ass_error:''}
  });
  assert.equal(await retry,true);
  assert.equal(node('layoutCueTimingRetryExportBtn').hidden,true);
  assert.equal(subtitles[1].end,7.2,'retry cannot change committed cue boundary');

  assert.ok(invalidations>=3);
  process.stdout.write('timing editor production JS contract: OK\n');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        script="const source = "+json.dumps(source,ensure_ascii=False)+";\n"+harness
        result=subprocess.run(["node","-"],input=script,text=True,capture_output=True,timeout=20,check=False)
        self.assertEqual(result.returncode,0,result.stdout+"\n"+result.stderr)


if __name__=="__main__":
    unittest.main()
