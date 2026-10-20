"""Regression guards for persistent incremental source generations."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.remote_apply_generation import publish_audio_generation


class IncrementalAudioGenerationTests(unittest.TestCase):
    def test_new_generation_keeps_last_active_audio_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            generated = Path(td) / ".generated"
            active = generated / "combined_audio"
            active.mkdir(parents=True)
            (active / "previous.wav").write_bytes(b"original audio")
            staged = generated / "combined-audio-new" / "combined"
            staged.mkdir(parents=True)
            (staged / "new.wav").write_bytes(b"next audio")

            published = publish_audio_generation(
                generated, staged, "combined-audio-new"
            )
            self.assertEqual(published, generated / "combined_audio_versions" / "combined-audio-new")
            self.assertEqual((active / "previous.wav").read_bytes(), b"original audio")
            self.assertEqual((published / "new.wav").read_bytes(), b"next audio")
            self.assertFalse(staged.exists())

    def test_failure_during_generation_publish_does_not_remove_active_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            generated = Path(td) / ".generated"
            active = generated / "combined_audio"
            active.mkdir(parents=True)
            (active / "previous.wav").write_bytes(b"original audio")
            staged = generated / "combined-audio-new"
            staged.mkdir()
            (staged / "new.wav").write_bytes(b"new audio")
            with patch("app.remote_apply_generation.os.replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    publish_audio_generation(generated, staged, "combined-audio-new")
            self.assertEqual((active / "previous.wav").read_bytes(), b"original audio")
            self.assertTrue((staged / "new.wav").is_file())

    def test_reject_existing_or_untrusted_generation_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            generated = Path(td) / ".generated"
            staged = generated / "combined-audio-new"
            staged.mkdir(parents=True)
            with self.assertRaises(ValueError):
                publish_audio_generation(generated, staged, "../escape")
            existing = generated / "combined_audio_versions" / "combined-audio-new"
            existing.mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                publish_audio_generation(generated, staged, "combined-audio-new")
            self.assertTrue(staged.exists())

    def test_both_api_adapters_use_versioned_generation_and_index(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in ("server.py", "lite_server.py"):
            with self.subTest(adapter=name):
                source = (root / "app" / name).read_text(encoding="utf-8")
                self.assertIn("publish_audio_generation(generated,", source)
                self.assertIn('combined_audio_versions', source)
                self.assertIn('quick_index_incremental_{temporary_root.name}', source)
                self.assertNotIn("shutil.rmtree(combined)", source)


if __name__ == "__main__":
    unittest.main()
