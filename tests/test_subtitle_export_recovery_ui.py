"""Reload of stale derived-export receipt exposes real retry without rewriting cues."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node required for production JS state proof")
class SubtitleExportRecoveryUiTests(unittest.TestCase):
    def test_health_is_restored_by_get_and_retry_is_independent_of_text_edit(self):
        html=(Path(__file__).resolve().parents[1]/"app/static/index.html").read_text(encoding="utf-8")
        start=html.index("let layoutStressSample=null;")
        end=html.index("async function updateLayoutPreview()",start)
        source=html[start:end]
        harness=r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function node(id){
  if(!nodes.has(id)){
    const cls=new Set();
    nodes.set(id,{
      value:'',textContent:'',innerHTML:'',dataset:{},disabled:false,
      hidden:false,style:{},className:'',pause(){},load(){},removeAttribute(){},
      classList:{add:x=>cls.add(x),remove:x=>cls.delete(x)},
      setAttribute(k,v){this[k]=v}
    });
  }
  return nodes.get(id);
}
const rows=[{
  id:21,source_text:'Story',final_chs:'已保存正文',
  start:3,end:5,source_start:3,source_end:5,
  source_member_id:'voice/story-21.wav',timing_modified:false
}];
const project={root:'/project-qa',name:'qa',config:{}};
const requests=[];
const context={
  document:{getElementById:node,querySelectorAll:()=>[],activeElement:null},
  currentProject:project,
  escapeHtml:x=>String(x),formatClockTime:x=>String(x),
  api:(url,options)=>{
    if(url.endsWith('/subtitles?selector=all'))
      return Promise.resolve({
        subtitles:rows,persistable:true,
        export_status:{
          state:'failed',artifacts_current:false,ass_required:true,
          artifact_error:'SRT disk full'
        }
      });
    if(url.endsWith('/subtitles/artifacts/refresh'))
      return new Promise((resolve,reject)=>requests.push({
        url,body:JSON.parse(options.body),resolve,reject
      }));
    throw Error('unexpected API '+url);
  },
  subtitleSettingsPayload:()=>({base_chs_size:48}),
  updateLayoutPreview:()=>{},scheduleLayoutPreviewUpdate:()=>{},
  invalidateLibassPreview:()=>{},
  shortError:e=>String(e?.message||e),setLog:e=>{throw Error(e)},
  console
};
vm.createContext(context);
vm.runInContext(source,context);
const run=x=>vm.runInContext(x,context);
(async()=>{
  await run('loadLayoutStressSample({force:true,selectId:21})');
  assert.equal(node('layoutExportHealthNotice').hidden,false,'persisted failure must survive reload');
  assert.match(node('layoutExportHealthDetail').textContent,/SRT disk full/);
  assert.equal(node('layoutExportHealthRetryBtn').hidden,false);
  assert.equal(node('layoutCueTimingRetryExportBtn').hidden,false);
  const retry=run('layoutCueRetryArtifactExport()');
  assert.equal(requests.length,1);
  assert.match(requests[0].url,/subtitles\/artifacts\/refresh$/);
  assert.equal(requests[0].body.expected_project_root,'/project-qa');
  assert.equal(requests[0].body.force_ass,true);
  requests.shift().resolve({
    artifacts_current:true,artifact_error:'',
    refreshed:{ass_error:''},
    export_status:{state:'current',artifacts_current:true,ass_required:true,artifact_error:''}
  });
  assert.equal(await retry,true);
  assert.equal(node('layoutExportHealthRetryBtn').hidden,true);
  assert.equal(node('layoutCueTimingRetryExportBtn').hidden,true);
  assert.match(node('layoutExportHealthTitle').textContent,/已校验/);
  assert.equal(rows[0].final_chs,'已保存正文','export retry must not rewrite cue text');
  assert.equal(rows[0].start,3,'export retry must not retime audio');
  process.stdout.write('persistent export health and independent retry PASS\n');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        script="const source = "+json.dumps(source,ensure_ascii=False)+";\n"+harness
        result=subprocess.run(
            ["node","-"],input=script,text=True,capture_output=True,timeout=20,check=False
        )
        self.assertEqual(result.returncode,0,result.stdout+"\n"+result.stderr)


if __name__=="__main__":
    unittest.main()
