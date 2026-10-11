"""A revised official transcript must not silently inherit stale Chinese edits."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.project import create_project
from app.subtitles import (
    get_project_subtitles,
    load_subtitle_overrides,
    refresh_subtitle_artifacts,
    update_project_subtitles,
)


class HumanTranslationSourceRevisionTests(unittest.TestCase):
    def _project(self, root: Path):
        output = root / "output"
        output.mkdir()
        manifest = {
            "entries": [{
                "index": 1,
                "filename": "line.wav",
                "logical_id": "line-1",
                "source_member_id": "chapter/line.wav",
                "source_text": "Original official line",
                "target_text": "原始中文",
                "start_seconds": 1.0,
                "audio_end_seconds": 3.0,
                "display_end_seconds": 3.0,
            }]
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
        )
        config = create_project(
            root, name="revision-test", index_csv="index.csv",
            wav_source="wavs", output_dir="output",
        )
        return config, output

    def test_new_human_edit_binds_source_then_rebuild_requires_recheck(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg, output = self._project(Path(temp))
            with patch("app.subtitles.invalidate_subtitle_stages"):
                saved = update_project_subtitles(cfg, output, [{
                    "id": 1, "final_chs": "人工翻译一", "confirmed": True,
                }])
            self.assertTrue(saved["saved"])
            overrides_before = (output / "subtitles_overrides.json").read_bytes()
            record = load_subtitle_overrides(output)["1"]
            self.assertEqual(len(record["source_text_sha256"]), 64)
            self.assertTrue(get_project_subtitles(cfg, output)[0]["confirmed"])

            manifest_path = output / "manifest.json"
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            old["entries"][0]["source_text"] = "Revised official line"
            old["entries"][0]["target_text"] = "新版原始中文"
            manifest_path.write_text(
                json.dumps(old, ensure_ascii=False), encoding="utf-8",
            )
            result = get_project_subtitles(cfg, output)[0]
            self.assertEqual(result["source_text"], "Revised official line")
            self.assertEqual(result["final_chs"], "新版原始中文")
            self.assertFalse(result["modified"])
            self.assertFalse(result["confirmed"])
            self.assertTrue(result["translation_override_stale"])
            self.assertEqual(result["translation_override_previous_chs"], "人工翻译一")

            refresh_subtitle_artifacts(cfg, output)
            saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = saved_manifest["entries"][0]
            self.assertEqual(entry["source_text"], "Revised official line")
            self.assertEqual(entry["final_chs"], "新版原始中文")
            self.assertNotIn("translation_override_stale", entry)
            self.assertNotIn("translation_override_previous_chs", entry)
            self.assertEqual((output / "subtitles_overrides.json").read_bytes(), overrides_before)
            srt = (output / "HSR_Voice_Archive.srt").read_text(encoding="utf-8-sig")
            self.assertIn("新版原始中文", srt)
            self.assertNotIn("人工翻译一", srt)

            with patch("app.subtitles.invalidate_subtitle_stages"):
                saved_again = update_project_subtitles(cfg, output, [{
                    "id": 1, "final_chs": "新版人工复核译文",
                    "expected_final_chs": "新版原始中文",
                    "expected_source_member_id": "chapter/line.wav",
                }])
            self.assertTrue(saved_again["saved"])
            after = get_project_subtitles(cfg, output)[0]
            self.assertEqual(after["source_text"], "Revised official line")
            self.assertEqual(after["final_chs"], "新版人工复核译文")
            self.assertFalse(after["translation_override_stale"])
            self.assertFalse(after["confirmed"])
            self.assertEqual(after["translation_override_previous_chs"], "")
            self.assertNotEqual(
                load_subtitle_overrides(output)["1"]["source_text_sha256"],
                record["source_text_sha256"],
            )

    def test_legacy_untagged_translation_kept_for_backward_compatibility(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg, output = self._project(Path(temp))
            (output / "subtitles_overrides.json").write_text(
                json.dumps({
                    "old-id": {
                        "source_member_id": "chapter/line.wav",
                        "filename": "line.wav",
                        "final_chs": "历史校对", "modified": True,
                    }
                }, ensure_ascii=False), encoding="utf-8",
            )
            row = get_project_subtitles(cfg, output)[0]
            self.assertEqual(row["final_chs"], "历史校对")
            self.assertTrue(row["modified"])
            self.assertFalse(row["translation_override_stale"])

    def test_same_source_text_whitespace_change_keeps_translation(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg, output = self._project(Path(temp))
            with patch("app.subtitles.invalidate_subtitle_stages"):
                update_project_subtitles(cfg, output, [{
                    "id": 1, "final_chs": "已校对文本",
                }])
            manifest_path = output / "manifest.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["entries"][0]["source_text"] = "  Original  official\n line "
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
            row = get_project_subtitles(cfg, output)[0]
            self.assertEqual(row["final_chs"], "已校对文本")
            self.assertFalse(row["translation_override_stale"])

    def test_proofreading_ui_exposes_old_text_only_as_read_only_evidence(self):
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text(
            encoding="utf-8",
        )
        self.assertIn('id="subRevisionWarning"', html)
        self.assertIn('id="subPreviousTranslation"', html)
        self.assertIn("oldTranslation.textContent=", html)
        self.assertIn("sub.translation_override_stale", html)
        self.assertIn("sub.translation_override_previous_chs", html)


if __name__ == "__main__":
    unittest.main()
