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

    def test_source_whitespace_cannot_merge_two_english_words(self):
        from app.subtitles import _official_source_sha256
        a = _official_source_sha256({"source_text": "now here"})
        b = _official_source_sha256({"source_text": "nowhere"})
        self.assertNotEqual(a, b)
        self.assertEqual(a, _official_source_sha256({"source_text": "now   here"}))

    def test_stale_editor_same_member_same_baseline_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg, output = self._project(Path(temp))
            before = get_project_subtitles(cfg, output)[0]
            from app.subtitles import _official_source_sha256
            manifest = output / "manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["entries"][0]["source_text"] = "Totally different official line"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Official source text changed"):
                update_project_subtitles(cfg, output, [{
                    "id": 1, "final_chs": "旧编辑窗口草稿",
                    "expected_final_chs": before["final_chs"],
                    "expected_source_member_id": before["source_member_id"],
                    "expected_source_text_sha256": before["source_text_sha256"],
                }])
            self.assertEqual(
                _official_source_sha256(data["entries"][0]),
                get_project_subtitles(cfg, output)[0]["source_text_sha256"],
            )
            self.assertFalse((output / "subtitles_overrides.json").exists())
            self.assertFalse((output / "subtitles_override_history.jsonl").exists())

    def test_revised_translations_remain_in_history_after_replace_and_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg, output = self._project(Path(temp))
            with patch("app.subtitles.invalidate_subtitle_stages"):
                update_project_subtitles(cfg, output, [{
                    "id": 1, "final_chs": "第一版校对",
                }])
                update_project_subtitles(cfg, output, [{
                    "id": 1, "final_chs": "第二版校对",
                }])
                update_project_subtitles(cfg, output, [{
                    "id": 1, "final_chs": "原始中文",
                }])
            history = [
                json.loads(x)
                for x in (output / "subtitles_override_history.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                [row["prior"]["final_chs"] for row in history],
                ["第一版校对", "第二版校对"],
            )
            self.assertEqual(
                [row["operation"] for row in history],
                ["superseded", "restore_base"],
            )
            self.assertEqual(load_subtitle_overrides(output), {})
            self.assertEqual(get_project_subtitles(cfg, output)[0]["final_chs"], "原始中文")

    def test_confirmation_does_not_migrate_by_reused_item_index(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg, output = self._project(Path(temp))
            with patch("app.subtitles.invalidate_subtitle_stages"):
                update_project_subtitles(cfg, output, [{
                    "id": 1, "confirmed": True,
                }])
            state_path = output / "subtitle_review_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["1"]["source_member_id"], "chapter/line.wav")
            self.assertTrue(get_project_subtitles(cfg, output)[0]["confirmed"])
            manifest_path = output / "manifest.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["entries"][0].update(
                source_member_id="other/line.wav",
                source_text="Reassigned voice line",
            )
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(get_project_subtitles(cfg, output)[0]["confirmed"])
            self.assertIn("1", json.loads(state_path.read_text(encoding="utf-8")))

    def test_legacy_bool_confirmation_is_unverified_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg, output = self._project(Path(temp))
            state_path = output / "subtitle_review_state.json"
            state_path.write_text('{"1":true}', encoding="utf-8")
            self.assertFalse(get_project_subtitles(cfg, output)[0]["confirmed"])
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), {"1": True})

    def test_legacy_same_basename_same_index_without_member_does_not_transplant(self):
        from app.subtitles import _apply_derived_subtitle_fields
        entries = [
            {"index": 1, "filename": "dup.wav",
             "source_member_id": "female/dup.wav", "target_text": "女性字幕"},
            {"index": 2, "filename": "dup.wav",
             "source_member_id": "male/dup.wav", "target_text": "男性字幕"},
        ]
        overrides = {
            "1": {"filename": "dup.wav", "final_chs": "无身份旧翻译", "modified": True}
        }
        _apply_derived_subtitle_fields(entries, overrides)
        self.assertEqual(
            [x["final_chs"] for x in entries], ["女性字幕", "男性字幕"],
        )

    def test_ui_passes_source_revision_for_both_editors_and_explicit_selection(self):
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text(
            encoding="utf-8",
        )
        self.assertIn("expected_source_text_sha256:String(targetSub.source_text_sha256", html)
        self.assertIn("expected_source_text_sha256:String(sub.source_text_sha256", html)
        self.assertIn("selectedRemoteAdditions=new Set();", html)

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
