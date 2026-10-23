"""Read-only audit of prior Japanese/English incremental contamination."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.incremental_locale_audit import audit_incremental_audio_locale
from app.project import ProjectConfig


class IncrementalLocaleAuditTests(unittest.TestCase):
    def _project(self, root: Path, *, language: str = "ja") -> ProjectConfig:
        return ProjectConfig(
            schema_version=1, name="JP", root=str(root),
            index_csv=str(root / ".generated" / "quick_index_incremental.csv"),
            wav_source=str(root / ".generated" / "combined_audio_versions" / "combined-x"),
            output_dir="output", audio_language=language,
            source_text_language="ja",
        )

    def test_japanese_legacy_english_receipt_reports_original_candidates_without_editing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            orig = root / "original-japanese.zip"
            orig.write_bytes(b"trusted user data placeholder")
            generated = root / ".generated"
            generated.mkdir()
            original_index = generated / "quick_index.csv"
            original_index.write_text("filename,source_member_id\njp.wav,jp.wav\n")
            (generated / "quick_scan.json").write_text(json.dumps({
                "english": {
                    "source": str(orig),
                    "fingerprint": {"digest": "recorded-fingerprint"},
                }
            }), encoding="utf-8")
            active = generated / "combined_audio_versions" / "combined-x"
            active.mkdir(parents=True)
            active_index = generated / "quick_index_incremental.csv"
            active_index.write_text("filename\njp.wav\n")
            output = root / "output"
            output.mkdir()
            receipt = output / "update_apply_report.json"
            receipt.write_text(json.dumps({
                "project_updated": True, "applied_filenames": ["en.wav"]
            }), encoding="utf-8")
            before = {p.relative_to(root).as_posix(): p.read_bytes()
                      for p in root.rglob("*") if p.is_file()}
            result = audit_incremental_audio_locale(self._project(root))
            after = {p.relative_to(root).as_posix(): p.read_bytes()
                     for p in root.rglob("*") if p.is_file()}
            self.assertTrue(result["suspicious_wrong_language_apply"])
            self.assertTrue(result["requires_manual_review"])
            self.assertEqual(result["legacy_apply"]["audio_language"], "en")
            self.assertEqual(result["original_source_candidate"], str(orig))
            self.assertTrue(result["original_index_exists"])
            self.assertFalse(result["original_source_hash_verified_now"])
            self.assertFalse(result["current_is_original"])
            self.assertEqual(before, after)

    def test_english_legacy_receipt_does_not_imply_cross_locale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            output.mkdir()
            (output / "update_apply_report.json").write_text(json.dumps({
                "project_updated": True
            }), encoding="utf-8")
            result = audit_incremental_audio_locale(self._project(root, language="en"))
            self.assertFalse(result["suspicious_wrong_language_apply"])

    def test_corrupt_receipt_does_not_claim_safe_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            output.mkdir()
            (output / "update_apply_report.json").write_text("not JSON")
            result = audit_incremental_audio_locale(self._project(root))
            self.assertTrue(result["requires_manual_review"])
            self.assertTrue(any("损坏" in msg for msg in result["issues"]))

    def test_both_server_surfaces_expose_same_read_only_api(self):
        src_root = Path(__file__).resolve().parents[1]
        for filename in ("app/server.py", "app/lite_server.py"):
            with self.subTest(filename=filename):
                code = (src_root / filename).read_text(encoding="utf-8")
                self.assertIn("/api/update/locale-audit", code)
                self.assertIn("audit_incremental_audio_locale(_active_config())", code)
        markup = (src_root / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="auditRemoteLocaleBtn"', markup)
        self.assertIn('id="remoteLocaleAuditResult"', markup)
        self.assertIn("原始包哈希本次未核验", markup)


if __name__ == "__main__":
    unittest.main()
