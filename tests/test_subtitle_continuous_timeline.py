"""Production browser-JS contract for project-wide display cue timeline.

Tests actual code extracted from app/static/index.html in Node VM, with deferred
persistence. This is interaction coverage, NOT a production-browser screenshot.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node is required for timeline interaction tests")
class SubtitleContinuousTimelineTests(unittest.TestCase):
    def test_timeline_zoom_navigation_pointer_drag_and_explicit_save(self):
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
      value:'',textContent:'',innerHTML:'',className:'',disabled:false,
      dataset:{},style:{},type:'text',
      pause(){},load(){},removeAttribute(){},
      classList:{add:x=>hidden.add(x),remove:x=>hidden.delete(x),contains:x=>hidden.has(x)},
      getBoundingClientRect:()=>({left:0,width:600}),
      setAttribute(k,v){this[k]=v}
    });
  }
  return nodes.get(id);
}
node('layoutTimelineSnap').checked=true;
const sourceRows=[
{id:11,source_text:'First subtitle',final_chs:'第一条',start:8,end:10,source_start:8,source_end:10},
{id:12,source_text:'Second subtitle',final_chs:'第二条',start:10.4,end:12.5,source_start:10.4,source_end:12.5},
{id:13,source_text:'Third subtitle',final_chs:'第三条',start:12.3,end:13.7,source_start:12.3,source_end:13.7},
{id:14,source_text:'Far away',final_chs:'远端条目',start:305,end:308,source_start:305,source_end:308}
];
const project={name:'test-project',root:'/project/root',config:{}};
const posted=[];
const context={
  document:{getElementById:node,querySelectorAll:()=>[],activeElement:null},
  currentProject:project,
  escapeHtml:x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
  formatClockTime:n=>Number(n).toFixed(1),
  api:(path,opts)=>{
    if(path.endsWith('/subtitles?selector=all'))
      return Promise.resolve({subtitles:sourceRows,persistable:true});
    if(path.endsWith('/timing'))
      return new Promise(resolve=>posted.push({path,payload:JSON.parse(opts.body),resolve}));
    throw Error('unexpected endpoint '+path);
  },
  subtitleSettingsPayload:()=>({base_chs_size:48}),
  updateLayoutPreview:()=>{},scheduleLayoutPreviewUpdate:()=>{},invalidateLibassPreview:()=>{},
  shortError:e=>String(e?.message||e),setLog:e=>{throw Error(e)},console
};
vm.createContext(context);
vm.runInContext(source,context);
const act=text=>vm.runInContext(text,context);
(async()=>{
  await act('loadLayoutStressSample({force:true,selectId:11})');
  const ruler=node('layoutTimelineRuler').innerHTML;
  const clips=node('layoutTimelineTracks').innerHTML;
  assert.match(ruler,/layout-timeline-tick/,'time ruler must be a real viewport');
  assert.match(clips,/data-timeline-cue="11"/);
  assert.match(clips,/data-timeline-cue="12"/);
  assert.match(clips,/data-timeline-edge="start"/);
  assert.equal(node('layoutTimelinePan').disabled,false,'pan enabled for long archive');
  assert.equal(node('layoutTimelineScope').textContent.includes('可编辑显示时间'),true);
  assert.equal(node('layoutCueTimingSaveBtn').disabled,true);
  const start=act('layoutTimelineWindowStart'),span=act('layoutTimelineWindowSeconds');
  const pointer={target:{closest:s=>s==='[data-timeline-edge]'?{dataset:{timelineEdge:'end'}}:null},pointerId:21,preventDefault(){}};
  act('layoutTimelinePointerDown('+ '({target:{closest:s=>s==="[data-timeline-edge]"?{dataset:{timelineEdge:"end"}}:null},pointerId:21,preventDefault(){}})' +')');
  // The pointer is within the snap threshold of the following subtitle's 10.4s start.
  const x=(10.37-start)/span*600;
  context.moveEvent={pointerId:21,clientX:x,preventDefault(){}};
  act('layoutTimelinePointerMove(moveEvent)');
  assert.equal(Number(act('layoutTimingState(layoutCueRow(11)).end')),10.4,'snap to neighboring boundary');
  assert.equal(sourceRows[0].end,10,'drag must not persist or mutate server-backed row yet');
  assert.equal(node('layoutCueTimingSaveBtn').disabled,false);
  act('layoutTimelinePointerUp({pointerId:21})');
  const result=act('saveLayoutCueTiming()');
  assert.equal(posted.length,1);
  assert.equal(posted[0].payload.expected_end,10);
  assert.equal(posted[0].payload.end,10.4);
  assert.equal(posted[0].payload.expected_project_root,'/project/root');
  posted.shift().resolve({
    timing:{id:'11',start:8,end:10.4,timing_modified:true},
    refreshed:{ass_error:''}
  });
  assert.equal(await result,true);
  assert.equal(sourceRows[0].end,10.4,'only explicit Save updates effective project time');
  assert.equal(node('layoutCueTimingSaveBtn').disabled,true);

  act('layoutTimelinePanBy(150)');
  assert.equal(act('layoutTimelineFollowSelected'),false);
  assert.ok(act('layoutTimelineWindowStart')>50);
  await act('loadLayoutStressSample({force:true,selectId:14})');
  assert.ok(act('layoutTimelineWindowStart')>=280,'selection recenters distant cue');
  assert.match(node('layoutTimelineTracks').innerHTML,/data-timeline-cue="14"/);

  const before=act('layoutTimelineWindowStart');
  act('layoutTimelineWindowSeconds=60;layoutTimelineWindowStart=layoutTimelineClampStart('+before+');layoutTimelineRender()');
  assert.ok(node('layoutTimelineRuler').innerHTML.includes('layout-timeline-tick'));
  assert.ok(node('layoutTimelineTracks').innerHTML.length<15000,'rendered DOM remains bounded');
  process.stdout.write('multi-cue timeline: select, snap, zoom, pan, explicit persisted edit PASS\n');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        script="const source = "+json.dumps(source,ensure_ascii=False)+";\n"+harness
        run=subprocess.run(
            ["node","-"],input=script,text=True,capture_output=True,timeout=20,check=False
        )
        self.assertEqual(run.returncode,0,run.stdout+"\n"+run.stderr)


if __name__=="__main__":
    unittest.main()
