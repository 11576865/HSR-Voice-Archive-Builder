"""Backed-up human Chinese revisions can only be recovered by exact provenance."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.project import create_project
from app.recovery import build_text_recovery
from app.security import api_token
from app.server import app, _set_active, _clear_active
from app.subtitle_recovery import (
    preview_human_subtitle_recovery,
    apply_human_subtitle_recovery,
)
from app.subtitles import (
    get_project_subtitles,
    load_subtitle_overrides,
    update_project_subtitles,
)


class HumanChineseRecoveryTests(unittest.TestCase):
    def _project(self, root: Path, *, changed_text: bool = False,
                 changed_audio: bool = False, reindex: int = 1):
        output = root / "output"
        output.mkdir(parents=True)
        (output / "manifest.json").write_text(
            json.dumps({"entries": [{
                "index": reindex,
                "filename": "dup.wav",
                "source_member_id": "chapterA/dup.wav",
                "source_text": "A different line" if changed_text else "Hello world",
                "target_text": "旧的基础翻译",
                "sha256": "2" * 64 if changed_audio else "1" * 64,
                "start_seconds": 0.0,
                "display_end_seconds": 2.0,
            }, {
                "index": reindex + 1,
                "filename": "dup.wav",
                "source_member_id": "chapterB/dup.wav",
                "source_text": "Another line",
                "target_text": "另一条中文",
                "sha256": "3" * 64,
                "start_seconds": 2.0,
                "display_end_seconds": 3.0,
            }]}, ensure_ascii=False), encoding="utf-8",
        )
        config = create_project(
            root, name="RestoreCase",
            index_csv="index.csv", wav_source="wavs", output_dir="output",
        )
        return config, output

    def _backup(self, root: Path):
        cfg, output = self._project(root)
        with patch("app.subtitles.invalidate_subtitle_stages"):
            saved = update_project_subtitles(cfg, output, [{
                "id": 1, "final_chs": "人工译文保留", "confirmed": True,
            }])
        self.assertTrue(saved["saved"])
        item = load_subtitle_overrides(output)["1"]
        self.assertEqual(item["source_text_hash_version"], 2)
        self.assertEqual(item["source_audio_sha256"], "1" * 64)
        self.assertTrue((output / "subtitle_review_state.json").exists())
        backup, _, _ = build_text_recovery(cfg)
        return backup.decode("utf-8")

    def _apply(self, cfg, text, preview, members=None):
        return apply_human_subtitle_recovery(
            cfg, text,
            selected_source_members=members or ["chapterA/dup.wav"],
            expected_backup_sha256=preview["backup_sha256"],
            expected_manifest_sha256=preview["manifest_sha256"],
            expected_project_root=preview["project_root"],
        )

    def test_preview_is_read_only_and_apply_matches_member_across_reindex(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backup = self._backup(root / "source")
            target, output = self._project(root / "target", reindex=12)
            preview = preview_human_subtitle_recovery(target, backup)
            self.assertEqual(preview["total"], 1)
            self.assertEqual(preview["restorable"], 1)
            self.assertEqual(preview["rows"][0]["current_id"], 12)
            self.assertFalse((output / "subtitles_overrides.json").exists())
            applied = self._apply(target, backup, preview)
            self.assertEqual(applied["restored_count"], 1)
            self.assertEqual(applied["restored_source_members"], ["chapterA/dup.wav"])
            self.assertTrue(Path(applied["preserved_package"]).is_file())
            self.assertEqual(load_subtitle_overrides(output)["12"]["final_chs"],
                             "人工译文保留")
            rows = get_project_subtitles(target, output)
            self.assertEqual(rows[0]["final_chs"], "人工译文保留")
            self.assertFalse(rows[0]["confirmed"], "review must not be restored implicitly")
            self.assertEqual(rows[1]["final_chs"], "另一条中文")
            self.assertEqual(preview_human_subtitle_recovery(target, backup)["counts"],
                             {"current_edit_exists": 1})

    def test_missing_text_audio_and_legacy_provenance_are_review_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backup = self._backup(root / "source")
            for suffix, kw in [
                ("text", {"changed_text": True}),
                ("audio", {"changed_audio": True}),
            ]:
                with self.subTest(case=suffix):
                    target, output = self._project(root / suffix, **kw)
                    preview = preview_human_subtitle_recovery(target, backup)
                    self.assertEqual(preview["restorable"], 0)
                    self.assertEqual(preview["rows"][0]["status"],
                                     "source_text_changed" if suffix == "text"
                                     else "source_audio_changed")
                    with self.assertRaisesRegex(ValueError, "not safely restorable"):
                        self._apply(target, backup, preview)
                    self.assertFalse((output / "subtitles_overrides.json").exists())

            data = json.loads(backup)
            record = next(iter(json.loads(
                __import__("base64").b64decode(data["files"]["output/subtitles_overrides.json"]["base64"])
            ).values()))
            record.pop("source_text_hash_version")
            payload = json.dumps({"1": record}, ensure_ascii=False).encode("utf-8")
            import base64, hashlib
            data["files"]["output/subtitles_overrides.json"] = {
                "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                "base64": base64.b64encode(payload).decode("ascii"),
            }
            legacy_backup = json.dumps(data, ensure_ascii=False)
            target, _ = self._project(root / "legacy")
            legacy = preview_human_subtitle_recovery(target, legacy_backup)
            self.assertEqual(legacy["rows"][0]["status"], "legacy_provenance")

    def test_missing_current_audio_fingerprint_and_duplicate_cue_id_are_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backup = self._backup(root / "source")
            target, output = self._project(root / "target")
            manifest_file = output / "manifest.json"
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            data["entries"][0]["sha256"] = ""
            manifest_file.write_text(json.dumps(data), encoding="utf-8")
            no_audio = preview_human_subtitle_recovery(target, backup)
            self.assertEqual(no_audio["rows"][0]["status"], "missing_audio_provenance")
            self.assertEqual(no_audio["restorable"], 0)

            data["entries"][0]["sha256"] = "1" * 64
            data["entries"][1]["index"] = data["entries"][0]["index"]
            manifest_file.write_text(json.dumps(data), encoding="utf-8")
            duplicate = preview_human_subtitle_recovery(target, backup)
            self.assertEqual(duplicate["rows"][0]["status"],
                             "source_missing_or_ambiguous")
            self.assertEqual(duplicate["restorable"], 0)

    def test_conflicting_current_proofreading_and_changed_manifest_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backup = self._backup(root / "source")
            target, output = self._project(root / "target")
            preview = preview_human_subtitle_recovery(target, backup)
            manifest = output / "manifest.json"
            doc = json.loads(manifest.read_text(encoding="utf-8"))
            doc["report"] = {"revision": "later"}
            manifest.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed; preview again"):
                self._apply(target, backup, preview)
            self.assertFalse((output / "subtitles_overrides.json").exists())
            fresh = preview_human_subtitle_recovery(target, backup)
            with patch("app.subtitles.invalidate_subtitle_stages"):
                update_project_subtitles(target, output, [{
                    "id": 1, "final_chs": "本地已有人工校对",
                }])
            conflict = preview_human_subtitle_recovery(target, backup)
            self.assertEqual(conflict["rows"][0]["status"], "current_edit_exists")
            with self.assertRaisesRegex(ValueError, "not safely restorable"):
                self._apply(target, backup, conflict)
            self.assertEqual(load_subtitle_overrides(output)["1"]["final_chs"],
                             "本地已有人工校对")

    def test_empty_selection_and_wrong_project_fail_without_side_effect(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backup = self._backup(root / "source")
            target, output = self._project(root / "target")
            preview = preview_human_subtitle_recovery(target, backup)
            with self.assertRaisesRegex(ValueError, "Select at least one"):
                apply_human_subtitle_recovery(
                    target, backup, selected_source_members=[],
                    expected_backup_sha256=preview["backup_sha256"],
                    expected_manifest_sha256=preview["manifest_sha256"],
                    expected_project_root=preview["project_root"],
                )
            with self.assertRaisesRegex(ValueError, "changed; preview again"):
                apply_human_subtitle_recovery(
                    target, backup, selected_source_members=["chapterA/dup.wav"],
                    expected_backup_sha256=preview["backup_sha256"],
                    expected_manifest_sha256=preview["manifest_sha256"],
                    expected_project_root=str(root / "wrong"),
                )
            self.assertFalse((output / "subtitles_overrides.json").exists())

    def test_fastapi_routes_and_termux_interface_use_same_recovery_guard(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backup = self._backup(root / "source")
            target, output = self._project(root / "target")
            _set_active(target)
            try:
                with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                    headers = {"X-HSR-Token": api_token()}
                    response = client.post(
                        "/api/recovery/human/preview",
                        data={"recovery_text": backup}, headers=headers,
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    preview = response.json()["result"]
                    response = client.post(
                        "/api/recovery/human/apply",
                        data={
                            "recovery_text": backup,
                            "selected_source_members": '["chapterA/dup.wav"]',
                            "expected_backup_sha256": preview["backup_sha256"],
                            "expected_manifest_sha256": preview["manifest_sha256"],
                            "expected_project_root": preview["project_root"],
                        }, headers=headers,
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["result"]["restored_count"], 1)
                    self.assertEqual(load_subtitle_overrides(output)["1"]["final_chs"],
                                     "人工译文保留")
            finally:
                _clear_active()
        lite = (Path(__file__).resolve().parents[1] / "app/lite_server.py").read_text(
            encoding="utf-8",
        )
        self.assertIn('path == "/api/recovery/human/preview"', lite)
        self.assertIn('path == "/api/recovery/human/apply"', lite)
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text(
            encoding="utf-8",
        )
        self.assertIn('id="previewHumanRestoreBtn"', html)
        self.assertIn('id="applyHumanRestoreBtn" disabled', html)
        self.assertIn("checkbox.checked=false", html)


if __name__ == "__main__":
    unittest.main()
