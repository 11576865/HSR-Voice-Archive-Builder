"""Browser/source invariants for same-family font replacement and real preview."""
from pathlib import Path
import unittest
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]


class StudioFontReplacementContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "app/static/index.html").read_text("utf-8")
        cls.fonts = (ROOT / "app/project_fonts.py").read_text("utf-8")

    def test_font_import_proof_is_serialized(self):
        s=self.html
        start=s.index("async function importCurrentProjectFont(){")
        end=s.index("document.getElementById('projectFontImportBtn')?.addEventListener",start)
        fn=s[start:end]
        self.assertIn("if(fontImportTransactionActive)return;",fn)
        self.assertIn("fontImportTransactionActive=true;",fn)
        self.assertIn("fontImportTransactionActive=false;",fn)
        self.assertIn("if(field)field.value=family;",fn)
        self.assertNotIn("field.dispatchEvent(new Event('input'",fn)
        self.assertIn("const verified=await renderLibassPreview();",fn)
        self.assertIn("lastRenderedAssIdentity!==currentLibassPreviewIdentity()",fn)
        self.assertIn("lastLibassPreviewError",fn)
        self.assertIn("if(fontImportTransactionActive)return;\n  clearTimeout(liveRealPreviewTimer)",s)

    def test_geometry_is_boxes_not_a_second_caption(self):
        s=self.html
        start=s.index("function renderPreviewCanvasSvg(")
        end=s.index("svg.innerHTML = elements.join('');",start)
        block=s[start:end]
        self.assertNotIn("geometryStyledText(",block)
        self.assertNotIn("escapeHtml(line.text)",block)
        self.assertIn("const boxW = bbox.x_max - bbox.x_min",block)
        self.assertIn("辅助边界图（无文字渲染）",s)

    def test_real_import_twice_without_duplicate_validation_or_false_failure(self):
        """Run the production browser handler: first + repeat + real failure."""
        source=self.html
        start=source.index("async function importCurrentProjectFont(){")
        end=source.index("document.getElementById('projectFontImportBtn')?.addEventListener",start)
        production=source[start:end]
        harness=r"""
const vm=require('node:vm'), assert=require('node:assert/strict');
const fields={
 projectFontFile:{files:[{name:'HYRunYuan-55W.ttf',size:480}]},
 projectFontTarget:{value:'prevChsFont'},
 projectFontImportStatus:{textContent:'',className:''},
 projectFontImportBtn:{disabled:false},
 prevChsFont:{value:'BeforeFont',dispatchEvent:()=>{throw Error('Unexpected competing input render');}},
 fontResolveStatus:{textContent:''}
};
let uploadCount=0, saveCount=0, renderCount=0, completeRender=null, uploads=[];
class FileReader{
 readAsDataURL(){this.result='data:font/ttf;base64,Zm9udA==';this.onload();}
}
const ctx={
 fontImportTransactionActive:false,
 liveRealPreviewTimer:null,
 previewDebounceTimer:null,
 currentProject:{root:'/work/archive'},
 document:{getElementById:id=>fields[id]||null},
 FileReader, clearTimeout:()=>{},
 api:async(url,opts)=>{assert.equal(url,'/api/subtitle-layout/import-font');uploadCount++;uploads.push(opts.body);return {font:{family:'HYRunYuan'}};},
 hostFontFaces:new Map([['HYRunYuan',{state:'loaded'}]]),
 invalidateHostFont:()=>{},
 ensureHostFontLoaded:async()=>{ctx.hostFontFaces.set('HYRunYuan',{state:'loaded'});return 'alias';},
 invalidateLibassPreview:()=>{ctx.lastRenderedAssIdentity=null;},
 currentLibassPreviewIdentity:()=>JSON.stringify(fields.prevChsFont.value),
 lastRenderedAssIdentity:null,
 lastLibassPreviewError:null,
 renderLibassPreview:()=>{renderCount++;return new Promise(resolve=>{completeRender=ok=>{
   if(ok)ctx.lastRenderedAssIdentity=ctx.currentLibassPreviewIdentity();
   else ctx.lastLibassPreviewError='libass font face mismatch';
   resolve(ok);
 };});},
 saveSubtitleLayoutSettings:async()=>{saveCount++;return true;},
 shortError:err=>String(err.message||err)
};
vm.createContext(ctx);vm.runInContext(source,ctx);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 const first=vm.runInContext('importCurrentProjectFont()',ctx);
 await settle();
 assert.equal(ctx.fontImportTransactionActive,true);
 assert.equal(uploadCount,1);assert.equal(renderCount,1);
 assert.equal(fields.prevChsFont.value,'HYRunYuan');
 const duplicate=vm.runInContext('importCurrentProjectFont()',ctx);
 await duplicate;
 assert.equal(uploadCount,1,'double-tap must not upload/validate twice');
 completeRender(true); await first;
 assert.equal(saveCount,1);assert.equal(ctx.fontImportTransactionActive,false);
 assert.match(fields.projectFontImportStatus.textContent,/验证并保存/);
 assert.equal(fields.projectFontImportBtn.disabled,false);

 const repeat=vm.runInContext('importCurrentProjectFont()',ctx);
 await settle();assert.equal(uploadCount,2);assert.equal(renderCount,2);
 completeRender(true);await repeat;
 assert.equal(saveCount,2,'re-import of identical font must remain idempotent');
 assert.match(fields.projectFontImportStatus.textContent,/验证并保存/);

 fields.prevChsFont.value='BeforeFont';
 const bad=vm.runInContext('importCurrentProjectFont()',ctx);
 await settle();completeRender(false);await bad;
 assert.equal(saveCount,2,'failed real proof must not save user setting');
 assert.equal(fields.prevChsFont.value,'BeforeFont','failed font proof must restore draft');
 assert.match(fields.projectFontImportStatus.textContent,/libass font face mismatch/);
 assert.equal(ctx.fontImportTransactionActive,false);
 process.stdout.write('Font import success, double tap, idempotent retry, rollback: OK\n');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        script="const source="+json.dumps(production,ensure_ascii=False)+";\n"+harness
        p=subprocess.run(["node","-"],input=script,encoding="utf-8",
                         capture_output=True,timeout=10,check=False)
        self.assertEqual(p.returncode,0,p.stdout+"\n"+p.stderr)

    def test_old_registered_fontface_is_removed_not_only_cache_entry(self):
        s = self.html
        self.assertIn("function invalidateHostFont(family)", s)
        self.assertIn("document.fonts.delete(old.face)", s)
        self.assertIn("hostFontEpochs.set(key", s)
        self.assertIn("if(!isCurrent())return key", s)
        self.assertIn("invalidateHostFont(family);", s)
        self.assertIn("function invalidateAllHostFonts()", s)
        self.assertIn("invalidateAllHostFonts();", s)
        self.assertNotIn("hostFontFaces.delete(family); // A prior browser fallback", s)

    def test_full_libass_preview_excludes_geometry_and_duplicate_canvas(self):
        s = self.html
        self.assertIn("document.getElementById('previewSvg')?.classList.toggle('hidden',realFrame)", s)
        self.assertIn("document.getElementById('assCanvasPreview')?.classList.toggle('hidden',realFrame)", s)
        self.assertIn("overlay.getContext('2d')?.clearRect", s)
        self.assertIn("overlay.style.display='none'", s)
        self.assertNotIn("renderCanvasSubtitleOverlay(data);", s)
        self.assertIn("if(!stillCurrent())return;", s)

    def test_appearance_edits_commit_real_libass_preview_not_just_svg(self):
        s = self.html
        self.assertIn("function scheduleCommittedAppearancePreview()", s)
        self.assertIn("liveRealPreviewArmed=true;", s)
        self.assertIn("['prevOutlineWidth','prevShadowDepth','prevBlurRadius','prevEnableCard',", s)
        self.assertIn("document.getElementById(id)?.addEventListener('change',scheduleCommittedAppearancePreview)", s)

    def test_project_font_replacement_has_single_fontconfig_candidate(self):
        s = self.fonts
        self.assertIn("for previous in list_project_fonts(output)", s)
        self.assertIn("old_path.unlink(missing_ok=True)", s)
        self.assertIn('"replaced_files": replaced', s)
        self.assertIn("if old_path.is_symlink()", s)

if __name__ == "__main__":
    unittest.main()
