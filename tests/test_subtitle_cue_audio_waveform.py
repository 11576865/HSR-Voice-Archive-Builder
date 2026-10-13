"""Original WAV belongs to Review; Layout owns no hidden audio transport.

Exercise the production Review loader in a Node VM with a genuine WAV header
fixture and verify object-URL revocation and invalid format rejection.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node required for source WAV behavior")
class ReviewOriginalAudioTests(unittest.TestCase):
    def test_one_review_wav_loader_and_url_lifecycle(self):
        html=(ROOT/"app/static/index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("<audio "),1)
        self.assertIn('id="subOriginalAudio"',html)
        for obsolete in ("layoutCueAudio", "layoutCueWaveform",
                         "layoutCueLoadAudio", "layoutCuePrepareWaveform",
                         "layoutCueSeekAudio"):
            self.assertNotIn(obsolete,html)

        begin=html.index("function clearReferenceAudio(){")
        end=html.index("function renderReferenceControls(",begin)
        production=html[begin:end]
        harness=r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function node(id){
  if(!nodes.has(id))nodes.set(id,{
    textContent:'',src:'',pause(){},load(){},
    removeAttribute(k){delete this[k]}
  });
  return nodes.get(id);
}
const signature=[82,73,70,70,0,0,0,0,87,65,86,69];
const valid={size:300, slice:()=>({arrayBuffer:async()=>new Uint8Array(signature).buffer})};
const invalid={size:300,slice:()=>({arrayBuffer:async()=>new Uint8Array(12).buffer})};
let nextBlob=valid;
let selected={id:7,source_member_id:'member-7'};
const created=[],revoked=[];
const context={
  document:{getElementById:node},
  currentProject:{name:'alpha',root:'/project/alpha'},
  currentSubtitle:()=>selected,
  fetch:async()=>({ok:true,blob:async()=>nextBlob}),
  headers:()=>({}),AbortController,
  URL:{
    createObjectURL:()=>{const k='blob:review-'+created.length;created.push(k);return k},
    revokeObjectURL:k=>revoked.push(k)
  },
  shortError:x=>String(x.message||x), console,
};
vm.createContext(context);
vm.runInContext("let referenceAudioKey=null,referenceAudioLabel='',referenceAudioAbortController=null,referenceAudioObjectUrl=null;\n"+__PRODUCTION__,context);
const run=x=>vm.runInContext(x,context);
(async()=>{
  await run("loadReferenceAudio({id:7,source_member_id:'member-7'})");
  assert.equal(created.length,1,'Review allocates exactly one WAV object URL');
  assert.equal(node('subOriginalAudio').src,'blob:review-0');
  await run("loadReferenceAudio({id:7,source_member_id:'member-7'})");
  assert.equal(created.length,1,'same selected cue must reuse existing Review audio');
  run("clearReferenceAudio()");
  assert.deepEqual(revoked,['blob:review-0']);
  assert.equal(node('subOriginalAudio').src,undefined);

  nextBlob=invalid;
  await run("loadReferenceAudio({id:7,source_member_id:'member-7'})");
  assert.equal(created.length,1,'invalid WAV must never create an object URL');
  assert.match(node('subAudioStatus').textContent,/WAV/);
  console.log('PASS: single Review WAV authority with URL lifecycle and header validation');
})().catch(e=>{console.error(e);process.exitCode=1});
""".replace("__PRODUCTION__", json.dumps(production,ensure_ascii=False))
        result=subprocess.run(["node","-e",harness],cwd=ROOT,capture_output=True,
                              text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+"\n"+result.stderr)


if __name__=="__main__":
    unittest.main()
