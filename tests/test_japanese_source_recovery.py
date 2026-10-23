"""Acceptance for isolated source-only Japanese recovery candidates."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.japanese_source_recovery import create_isolated_japanese_recovery
from app.project import PROJECT_FILENAME, ProjectConfig, save_project
from app.quick import source_inventory


class JapaneseSourceRecoveryTests(unittest.TestCase):
    def prepare(self, base: Path) -> tuple[ProjectConfig, Path]:
        root = base / "Original-JP"
        generated = root / ".generated"
        generated.mkdir(parents=True)
        zip_path = base / "original_voice.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("chapter/voice_jp_001.wav", b"RIFF" + b"\x00" * 32)
            archive.writestr("chapter/voice_jp_001.lab", "日本語テキスト")
        inventory = source_inventory(zip_path)
        initial_csv = generated / "quick_index.csv"
        with initial_csv.open("w", newline="", encoding="utf-8-sig") as out:
            writer = csv.DictWriter(out, fieldnames=[
                "index", "group", "filename", "source_member_id", "english",
            ])
            writer.writeheader()
            writer.writerow({
                "index": "1", "group": "chapter",
                "filename": "voice_jp_001.wav",
                "source_member_id": "chapter/voice_jp_001.wav",
                "english": "日本語テキスト",
            })
        (generated / "quick_scan.json").write_text(json.dumps({
            "source_text_language": "ja",
            "english": {
                "source": str(zip_path),
                "fingerprint": inventory["fingerprint"],
            },
        }), encoding="utf-8")
        # Simulate a legacy wrong-language incremental source without touching original ZIP.
        corrupted = generated / "combined_audio_versions" / "old"
        corrupted.mkdir(parents=True)
        (corrupted / "english.wav").write_bytes(b"EN")
        output = root / "output"
        output.mkdir()
        (output / "update_apply_report.json").write_text(json.dumps({
            "project_updated": True, "applied_filenames": ["english.wav"],
        }))
        (output / "continuous.flac").write_bytes(b"old product")
        cfg = ProjectConfig(
            schema_version=1, name="Old JP",
            root=str(root),
            index_csv=str(generated / "quick_index_incremental.csv"),
            wav_source=str(corrupted),
            output_dir="output",
            audio_language="ja",
            source_text_language="ja",
            target_language="zh-CN",
            game_id="honkai-star-rail",
        )
        save_project(cfg)
        return cfg, zip_path

    def test_creates_clean_unbuilt_candidate_and_preserves_old_project_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, original = self.prepare(base)
                root = Path(cfg.root)
                before = {
                    p.relative_to(root).as_posix(): p.read_bytes()
                    for p in root.rglob("*") if p.is_file()
                }
                result = create_isolated_japanese_recovery(cfg)
                after = {
                    p.relative_to(root).as_posix(): p.read_bytes()
                    for p in root.rglob("*") if p.is_file()
                }
                self.assertEqual(before, after)
                self.assertTrue(result["source_fingerprint_verified"])
                self.assertEqual(result["wav_members"], 1)
                self.assertFalse(result["built"])
                self.assertFalse(result["listening_verified"])
                restored = Path(result["candidate_project"])
                self.assertTrue(restored.is_dir())
                self.assertEqual(result["source"], str(original))
                restored_cfg = json.loads((restored / PROJECT_FILENAME).read_text(encoding="utf-8"))
                self.assertEqual(restored_cfg["wav_source"], str(original))
                self.assertEqual(restored_cfg["index_csv"], ".generated/quick_index.csv")
                self.assertEqual(restored_cfg["audio_language"], "ja")
                self.assertEqual(restored_cfg["source_text_language"], "ja")
                self.assertTrue(restored_cfg["remote_index_url"].endswith("/JP.xlsx"))
                self.assertFalse((restored / "output").exists())
                self.assertFalse((restored / ".state").exists())
                self.assertFalse((restored / ".generated" / "combined_audio_versions").exists())
                self.assertFalse((restored / ".generated" / "incremental_audio").exists())
                self.assertTrue((root / "output" / "continuous.flac").is_file())

    def test_tampered_original_source_is_refused_without_new_project(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, original = self.prepare(base)
                with original.open("ab") as handle:
                    handle.write(b"tampered")
                with self.assertRaisesRegex(ValueError, "指纹不一致"):
                    create_isolated_japanese_recovery(cfg)
                self.assertFalse(any("source-recovery" in p.name for p in base.iterdir()))

    def test_mismatched_index_members_are_refused(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, _original = self.prepare(base)
                initial = Path(cfg.root) / ".generated" / "quick_index.csv"
                initial.write_text(
                    "filename,source_member_id\nother.wav,chapter/other.wav\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "不完全匹配"):
                    create_isolated_japanese_recovery(cfg)
                self.assertFalse(any("source-recovery" in p.name for p in base.iterdir()))

    def test_ambiguous_auto_language_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, _ = self.prepare(base)
                cfg.audio_language = "auto"
                save_project(cfg)
                with self.assertRaisesRegex(ValueError, "ja"):
                    create_isolated_japanese_recovery(cfg)

    def test_both_servers_expose_explicit_recovery_confirmation(self):
        repository = Path(__file__).resolve().parents[1]
        for filename in ("server.py", "lite_server.py"):
            with self.subTest(filename=filename):
                code = (repository / "app" / filename).read_text(encoding="utf-8")
                self.assertIn("/api/update/create-japanese-recovery", code)
                self.assertIn('"create-isolated"', code)
                self.assertIn('create_isolated_japanese_recovery(', code)
        ui = (repository / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="createJapaneseRecoveryBtn"', ui)
        self.assertIn("独立恢复工程", ui)
        jobs = (repository / "app/jobs.py").read_text(encoding="utf-8")
        self.assertIn('"locale-recovery"', jobs)


if __name__ == "__main__":
    unittest.main()
