"""Regressions for user-reported voice choices, Termux image/API and chapter tracks."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.archive_bundle_backgrounds import MAX_BUNDLE_JSON_BYTES, MAX_IMAGE_BYTES
from app.archive_bundle_lifecycle import PREFERENCES, previous_bundle_settings
from app.chapter_audio_tracks import chapter_audio_tracks
from app.credentials import TranslationCredentials, test_saved_translation_credentials as check_api
from app.lite_server import _bool, _stringify_json_form
from app.project import ProjectConfig


class UserReportedSetupRegressions(unittest.TestCase):
    def test_new_project_timing_defaults_and_russian_preset_are_consistent(self):
        c = ProjectConfig(1, "test", "/tmp/hsr", "index.csv", "voice.zip")
        self.assertEqual((c.intro_gap, c.same_group_gap, c.group_gap), (10.0, 2.5, 5.0))
        template = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text("utf-8")
        self.assertNotIn('<option value="ru">Русский</option>', template)
        for ident, value in (("quickIntroGap", "10.00"), ("quickSameGroupGap", "2.50"),
                             ("quickGroupGap", "5.00")):
            self.assertIn(f'id="{ident}" type="number" min="0" step="0.01" value="{value}"', template)

    def test_termux_json_adapter_preserves_image_spec_and_true_string_flag(self):
        # The lite server stringifies dict/boolean JSON fields before routing.
        body = _stringify_json_form({
            "background_spec": {"mode": "image", "resolution": "720p"},
            "reuse_saved_background": True,
        })
        self.assertEqual(json.loads(body["background_spec"])["mode"], "image")
        self.assertTrue(_bool(body["reuse_saved_background"]))
        lite = (Path(__file__).resolve().parents[1] / "app/lite_server.py").read_text("utf-8")
        self.assertIn("raw_spec = json.loads(raw_spec)", lite)
        self.assertIn('_bool(data.get("reuse_saved_background"))', lite)

    def test_bounded_large_image_policy_and_missing_reuse_are_explicit(self):
        self.assertEqual(MAX_IMAGE_BYTES, 64 * 1024 * 1024)
        self.assertGreaterEqual(MAX_BUNDLE_JSON_BYTES, 90 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            (output / PREFERENCES).write_text(json.dumps({
                "schema_version": 1, "attach_project_fonts": False,
                "background": {"kind": "image", "color": "#000000",
                               "spec": {"mode": "image"}, "image_name": "missing.png",
                               "sha256": "0" * 64, "extension": ".png"},
            }), encoding="utf-8")
            settings = previous_bundle_settings(output)
            self.assertTrue(settings["known"])
            self.assertFalse(settings["image_saved"])

    def test_chapter_manifest_orders_tracks_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            directory = root / "chapter_flac"
            directory.mkdir()
            tracks = []
            for index in (1, 2):
                path = directory / f"{index:02d}_chapter.flac"
                data = b"fLaC" + bytes([index]) * 128
                path.write_bytes(data)
                tracks.append({"ordinal": index, "group_id": f"chapter{index}",
                               "path": str(path), "file_sha256": hashlib.sha256(data).hexdigest()})
            (directory / "chapter_flac_manifest.json").write_text(json.dumps({
                "schema_version": 1, "chapter_count": 2, "outputs": tracks,
            }), encoding="utf-8")
            self.assertEqual([group for _, group in chapter_audio_tracks(root)],
                             ["chapter1", "chapter2"])
            (directory / "02_chapter.flac").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "校验不一致"):
                chapter_audio_tracks(root)

    def test_translation_api_test_is_real_not_cached_and_never_returns_key(self):
        fake = TranslationCredentials(
            "custom", "https://translation.example/v1", "very-secret-key",
            "compatible-model", "local",
        )
        with (
            patch("app.credentials.load_translation_credentials", return_value=fake),
            patch("app.translator.OpenAIResponsesHTTPClient") as client,
            patch("app.translator.ensure_translation_capability",
                  return_value={"ok": True, "usage_supported": True}) as run,
        ):
            outcome = check_api(timeout=4)
        self.assertTrue(outcome["schema_compatible"])
        self.assertNotIn("very-secret-key", json.dumps(outcome))
        self.assertEqual(client.call_args.kwargs["max_retries"], 0)
        self.assertTrue(run.call_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
