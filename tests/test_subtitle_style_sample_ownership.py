"""Tests the production HSR sample loader and save transaction using deferred requests.

The tests exercise JS sliced from the shipped HTML, rather than a rewritten
Python approximation. Node is optional on Python-only lightweight installs.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def run_node(source: str, harness: str) -> None:
    script = "const source = " + json.dumps(source, ensure_ascii=False) + ";\n" + harness
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True,
        timeout=15, check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stdout + "\n" + completed.stderr)


@unittest.skipUnless(shutil.which("node"), "Node required for production JS state regression")
class SubtitleStyleSampleOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")

    def test_late_project_sample_never_overwrites_manual_test_text(self) -> None:
        start = self.html.index("let layoutStressSample=null;")
        end = self.html.index("async function updateLayoutPreview()", start)
        source = self.html[start:end]
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {value:'',textContent:'',className:''});
  return nodes.get(id);
};
const pending = [];
let previewCalls = 0;
const project = {name:'project-a',root:'/projects/a'};
const context = {
  document:{getElementById:node,querySelectorAll:()=>[]},
  escapeHtml:x=>String(x),
  formatClockTime:x=>String(x),
  currentProject:project,
  api:()=>new Promise(resolve=>pending.push(resolve)),
  updateLayoutPreview:()=>{previewCalls++;},
  invalidateLibassPreview:()=>{},
  shortError:e=>String(e),
  console
};
vm.createContext(context);
vm.runInContext(source,context);
const doAction = x => vm.runInContext(x,context);
const fixture={subtitles:[{
  id:41,source_text:'Original project line',final_chs:'项目自动选出的中文字幕',
  start:0,end:2,word_alignment_ready:false
}]};
async function settle(){await Promise.resolve();await Promise.resolve();}
(async()=>{
  const late=doAction('loadLayoutStressSample()');
  assert.equal(pending.length,1);
  node('prevChsText').value='I am editing my own sample';
  doAction("layoutTrialTextRevision++;layoutSampleSource='manual';layoutSampleProjectIdentity=layoutActiveProjectKey();layoutStressSample=null;updateLayoutSampleOrigin()");
  pending.shift()(fixture);
  await late;
  assert.equal(node('prevChsText').value,'I am editing my own sample');
  assert.equal(node('layoutSampleOrigin').textContent,'测试文本 · 自定义（不会被自动样本覆盖）');

  await doAction('loadLayoutStressSample()');
  assert.equal(pending.length,0,'workspace reentry should not refetch over manual text');
  assert.equal(node('prevChsText').value,'I am editing my own sample');

  const reload=doAction('loadLayoutStressSample({force:true})');
  assert.equal(pending.length,1);
  pending.shift()(fixture);
  await reload;
  assert.equal(node('prevChsText').value,'项目自动选出的中文字幕');
  assert.equal(node('prevPriText').value,'Original project line');
  assert.equal(node('layoutSampleOrigin').textContent,'测试文本 · 项目字幕 #41');
  assert.equal(node('layoutCorpusCount').textContent,'1 / 1');
  assert.equal(node('layoutCorpusList').innerHTML.includes('项目自动选出的中文字幕'),true);

  // Project change during fetch must not publish the previous project's text.
  const oldProject=doAction('loadLayoutStressSample({refresh:true})');
  project.name='project-b';
  project.root='/projects/b';
  pending.shift()(fixture);
  await oldProject;
  assert.equal(node('prevChsText').value,'项目自动选出的中文字幕');
  assert.equal(previewCalls>=2,true);
  process.stdout.write('sample ownership: OK\n');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        run_node(source, harness)

    def test_save_reentry_is_blocked_and_newer_trial_is_preserved(self) -> None:
        start = self.html.index("async function saveSubtitleLayoutSettings(){")
        end = self.html.index("let layoutStressSample=null;", start)
        source = self.html[start:end]
        harness = r"""
const vm=require('node:vm');
const assert=require('node:assert/strict');
const nodes=new Map();
const node=id=>{
  if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',className:'',disabled:false});
  return nodes.get(id);
};
const requests=[];
const project={name:'fixture-project',root:'/projects/fixture'};
let size=48;
let fillCount=0;
const context={
  document:{getElementById:node},
  currentProject:project,
  subtitleSettingsPayload:()=>({base_chs_size:size}),
  api:(path,request)=>new Promise(resolve=>requests.push({path,request,resolve})),
  fillProject:()=>{fillCount++;size=48;},
  markSubtitleSettingsDirty:()=>{},
  shortError:e=>String(e),
  setLog:()=>{},
  console
};
vm.createContext(context);
vm.runInContext('let subtitleSettingsSavePending=false;let savedSubtitleSettings=null;',context);
vm.runInContext(source,context);
const act=x=>vm.runInContext(x,context);
(async()=>{
  const first=act('saveSubtitleLayoutSettings()');
  const duplicate=await act('saveSubtitleLayoutSettings()');
  assert.equal(duplicate,false);
  assert.equal(requests.length,1,'one owned save at a time');
  assert.equal(node('saveSubtitleLayoutBtn').disabled,true);
  size=54;
  requests.shift().resolve({project:{...project,config:{}}});
  assert.equal(await first,false,'new unsaved draft prevents export');
  assert.equal(size,54,'late save response must not reset newer input');
  assert.equal(fillCount,0,'live draft was preserved');
  assert.equal(node('saveSubtitleLayoutBtn').disabled,false);

  const current=act('saveSubtitleLayoutSettings()');
  assert.equal(requests.length,1);
  const submitted=JSON.parse(requests[0].request.body);
  assert.equal(submitted.base_chs_size,54);
  requests.shift().resolve({project:{...project,config:{}}});
  assert.equal(await current,true);
  assert.equal(fillCount,1);
  assert.equal(node('generateSubtitleAssBtn').disabled,false);
  process.stdout.write('save transaction: OK\n');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        run_node(source, harness)


if __name__ == "__main__":
    unittest.main()
