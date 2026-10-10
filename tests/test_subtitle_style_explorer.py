"""Behavioral contract for the integrated subtitle explorer and geometry audit.

These tests execute the production browser JavaScript with a deterministic corpus.
They prove navigation/filter semantics and solver-result classification; they do not
replace real FFmpeg/libass rendering or production screenshot evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(shutil.which("node"), "Node needed for production JavaScript workbench checks")
class SubtitleCorpusExplorerTests(unittest.TestCase):
    def test_navigation_search_and_bounded_solver_audit(self) -> None:
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        start = html.index("let layoutStressSample=null;")
        end = html.index("async function updateLayoutPreview()", start)
        source = html[start:end]
        harness = r"""
const vm=require('node:vm');
const assert=require('node:assert/strict');
const nodes=new Map();
function node(id){
  if(!nodes.has(id)){
    const classes=new Set(['hidden']);
    nodes.set(id,{
      value:'',textContent:'',innerHTML:'',className:'',disabled:false,
      classList:{
        add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x)
      },
      setAttribute(k,v){this[k]=v;}
    });
  }
  return nodes.get(id);
}
const data=[
{id:1,source_text:'Hello',final_chs:'你好',group:'Scene 1',start:1,end:3},
{id:2,source_text:'A much longer English subtitle',final_chs:'一个更长的测试字幕',group:'Scene 2',start:4,end:8,modified:true},
{id:3,source_text:'Overflow condition',final_chs:'这条字幕溢出了',group:'Scene 3',start:9,end:12}
];
let previewCalls=0;
const requests=[];
const context={
  document:{getElementById:node,querySelectorAll:()=>[]},
  currentProject:{name:'example',root:'/example',config:{source_text_language:'en',target_language:'zh-CN'}},
  escapeHtml:v=>String(v),formatClockTime:v=>String(v),
  api:async(path,config)=>{
    requests.push({path,config});
    if(path.endsWith('/subtitles?selector=all'))return {ok:true,subtitles:data};
    if(path.endsWith('/preview')){
      const q=JSON.parse(config.body);
      const kind=q.english_text==='Overflow condition'?'failed':
        q.english_text.startsWith('A much longer')?'scaled':'ok';
      return {ok:true,layout:{
        failed:kind==='failed',failed_condition:kind==='failed'?'HORIZONTAL_OVERFLOW':null,
        scale_percent:kind==='scaled'?85:100
      }};
    }
    throw Error('Unexpected request '+path);
  },
  subtitleSettingsPayload:()=>({
    base_chs_size:48,base_primary_size:42,
    margin_horizontal_percent:0.03,margin_vertical_percent:0.05,
    min_central_gap:20,chs_font:'Demo',primary_font:'Demo'
  }),
  updateLayoutPreview:()=>{previewCalls++;},
  invalidateLibassPreview:()=>{},
  setLog:err=>{throw Error(String(err));},
  shortError:e=>String(e?.message||e),console
};
vm.createContext(context);
vm.runInContext(source,context);
const act=s=>vm.runInContext(s,context);
(async()=>{
  await act('loadLayoutStressSample({force:true})');
  assert.equal(node('layoutCorpusCount').textContent,'3 / 3');
  assert.equal(node('layoutCorpusList').innerHTML.includes('Scene 2'),true);
  assert.equal(previewCalls,1);

  node('layoutCorpusSearch').value='Scene 2';
  act('renderLayoutCorpusRail()');
  assert.equal(node('layoutCorpusCount').textContent,'1 / 3');
  node('layoutCorpusSearch').value='';
  act("layoutCorpusFilter='all';renderLayoutCorpusRail()");
  await act('loadLayoutStressSample({force:true,selectId:2})');
  assert.equal(node('prevChsText').value,'一个更长的测试字幕');
  assert.equal(node('layoutPreviousSampleBtn').disabled,false);
  assert.equal(node('layoutNextSampleBtn').disabled,false);

  await act('runLayoutBatchAudit()');
  assert.equal(requests.filter(r=>r.path.endsWith('/preview')).length,3);
  assert.match(node('layoutAuditStatus').textContent,/溢出 1/);
  assert.match(node('layoutAuditStatus').textContent,/缩小 1/);
  act("layoutCorpusFilter='issues';renderLayoutCorpusRail()");
  assert.equal(node('layoutCorpusCount').textContent,'2 / 3');
  assert.equal(node('layoutCorpusList').innerHTML.includes('溢出'),true);

  act('invalidateLayoutBatchAudit()');
  assert.equal(node('layoutCorpusCount').textContent,'0 / 3');
  assert.equal(node('layoutAuditBtn').disabled,false);
  process.stdout.write('Corpus explorer and geometry audit: OK\n');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        script = "const source = " + json.dumps(source, ensure_ascii=False) + ";\n" + harness
        process = subprocess.run(
            ["node", "-"], input=script, text=True, capture_output=True,
            timeout=20, check=False
        )
        self.assertEqual(process.returncode, 0, process.stdout + "\n" + process.stderr)


if __name__ == "__main__":
    unittest.main()
