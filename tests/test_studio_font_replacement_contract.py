"""Browser/source invariants for same-family font replacement and real preview."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StudioFontReplacementContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "app/static/index.html").read_text("utf-8")
        cls.fonts = (ROOT / "app/project_fonts.py").read_text("utf-8")

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
