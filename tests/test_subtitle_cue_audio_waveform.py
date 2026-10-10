"""Source WAV waveform / transport behavior on production UI JavaScript."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node required for WAV waveform UI regression")
class CueAudioWaveformTests(unittest.TestCase):
    def test_waveform_decodes_audio_and_seeks_without_changing_subtitle_timing(self):
        html=(Path(__file__).resolve().parents[1]/"app/static/index.html").read_text(encoding="utf-8")
        begin=html.index("let layoutStressSample=null;")
        end=html.index("async function updateLayoutPreview()",begin)
        source=html[begin:end]
        harness=r"""
const vm=require('node:vm');
const assert=require('node:assert/strict');
const nodes=new Map();
let waveDraws=0;
function node(id){
  if(!nodes.has(id)){
    const hidden=new Set(['hidden']);
    nodes.set(id,{
      value:'',textContent:'',innerHTML:'',className:'',dataset:{},
      width:960,height:90,disabled:false,
      classList:{add:x=>hidden.add(x),remove:x=>hidden.delete(x)},
      setAttribute(k,v){this[k]=v;},
      pause(){},load(){},removeAttribute(k){delete this[k]},
      getContext:()=>({
        clearRect(){waveDraws++},fillRect(){},fillText(){},
        set fillStyle(v){},set font(v){}
      })
    });
  }
  return nodes.get(id);
}
node('layoutCueAudio').duration=4;
node('layoutCueAudio').currentTime=0;
const signatures=[...'RIFF',0,0,0,0,...'WAVE'];
const wav={
  size:32000,
  slice:()=>({arrayBuffer:async()=>new Uint8Array(signatures).buffer}),
  arrayBuffer:async()=>new ArrayBuffer(128)
};
const objectUrls=[];
const revoked=[];
const context={
  document:{getElementById:node,querySelectorAll:()=>[]},
  currentProject:{name:'alpha',root:'/project/alpha',config:{}},
  escapeHtml:x=>String(x),formatClockTime:n=>String(n),
  api:async()=>({subtitles:[{id:7,source_text:'Audio line',final_chs:'中文',start:3,end:7}],persistable:true}),
  updateLayoutPreview:()=>{},invalidateLibassPreview:()=>{},
  subtitleSettingsPayload:()=>({}),shortError:e=>String(e.message||e),
  setLog:e=>{throw Error(e)},headers:()=>({}),
  AbortController,
  fetch:async()=>({ok:true,blob:async()=>wav}),
  URL:{
    createObjectURL:()=>{const id='blob:'+objectUrls.length;objectUrls.push(id);return id;},
    revokeObjectURL:u=>revoked.push(u)
  },
  window:{
    AudioContext:class{
      async decodeAudioData(){
        return {getChannelData:()=>Float32Array.from({length:1000},(_,i)=>Math.sin(i/9)*0.5)};
      }
      close(){return Promise.resolve()}
    }
  },console
};
vm.createContext(context);
vm.runInContext(source,context);
const act=x=>vm.runInContext(x,context);
(async()=>{
  await act('loadLayoutStressSample({force:true,selectId:7})');
  await act('layoutCueLoadAudio()');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(objectUrls.length,1);
  assert.equal(node('layoutCueAudio').src,'blob:0');
  assert.equal(act('layoutCueWaveformPeaks.length'),240);
  assert.ok(waveDraws>0);
  act('layoutCueSeekAudio(0.5)');
  assert.equal(node('layoutCueAudio').currentTime,2);
  assert.equal(node('layoutCueStart').textContent,'3');
  assert.equal(node('layoutCueEnd').textContent,'7');
  act('layoutCueResetProject(null)');
  assert.deepEqual(revoked,['blob:0']);
  assert.equal(act('layoutCueWaveformPeaks'),null);
  process.stdout.write('cue WAV waveform contract: OK\n');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        script="const source = "+json.dumps(source,ensure_ascii=False)+";\n"+harness
        outcome=subprocess.run(["node","-"],input=script,text=True,capture_output=True,timeout=15,check=False)
        self.assertEqual(outcome.returncode,0,outcome.stdout+"\n"+outcome.stderr)


if __name__=="__main__":
    unittest.main()
