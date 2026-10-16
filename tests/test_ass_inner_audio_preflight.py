"""Audio source preflight for explicit *internal* ASS -> complete MKV export.

A valid sealed but subtitle-stale MKV remains a usable lossless audio source;
a loose/unknown MKV is not provenance. Refuse before rewriting ASS/SRT/receipt.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.archive_bundle_lifecycle import (
    OUTPUT, RECEIPT, bundle_audio_source_status, bundle_export_status,
    record_bundle_publication,
)
from app.auto_delivery import queue_ass_regeneration
from app.delivery_artifacts import seal_complete_delivery
from app.subtitle_export_status import record_subtitle_export, mark_ass_style_stale


class InternalAssAudioPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        (self.output / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
        (self.output / "HSR_Voice_Archive.ass").write_text("old-ass", encoding="utf-8")
        (self.output / "HSR_Voice_Archive.srt").write_text("old-srt", encoding="utf-8")
        record_subtitle_export(self.output, state="current", ass_required=True)
        self.config = SimpleNamespace(
            root=str(self.root), name="test", output_dir="output",
            delivery_mode="complete", source_text_language="en",
            target_language="zh-CN",
        )

    def test_no_audio_rejects_before_mutating_subtitle_files_or_receipt(self):
        names = ("manifest.json", "HSR_Voice_Archive.ass",
                 "HSR_Voice_Archive.srt", "subtitle_export_receipt.json")
        before = {name: (self.output / name).read_bytes() for name in names}
        with patch("app.auto_delivery.create_job",
                   side_effect=lambda _kind, runner, **kwargs: runner(lambda *args: None)), \
             patch("app.subtitles.refresh_subtitle_artifacts_from_settings") as render:
            with self.assertRaisesRegex(ValueError, "生成 ASS（内）被阻止"):
                queue_ass_regeneration(self.config, self.output)
            render.assert_not_called()
        self.assertEqual(before, {
            name: (self.output / name).read_bytes() for name in names
        })
        self.assertIn("没有完整 MKV", bundle_export_status(self.output)["blockers"][0])

    def test_unverified_mkv_does_not_impersonate_verified_audio(self):
        (self.output / OUTPUT).write_bytes(b"old-video-unverified")
        status = bundle_audio_source_status(self.output)
        self.assertFalse(status["available"])
        self.assertIn("缺少有效的发布校验回执", status["reason"])
        self.assertEqual(bundle_export_status(self.output)["state"], "unverified")
        self.assertTrue((self.output / OUTPUT).exists())

    def test_matching_but_unsealed_receipt_is_not_trusted_without_flac(self):
        (self.output / "continuous.flac").write_bytes(b"old-flac")
        (self.output / OUTPUT).write_bytes(b"old-video")
        record_bundle_publication(
            self.output, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False)
        (self.output / "continuous.flac").unlink()
        status = bundle_audio_source_status(self.output)
        self.assertFalse(status["available"])
        self.assertIn("尚未封存", status["reason"])
        self.assertTrue((self.output / OUTPUT).exists())

    def test_sealed_subtitle_stale_bundle_keeps_verified_audio_source(self):
        (self.output / "continuous.flac").write_bytes(b"old-flac")
        (self.output / OUTPUT).write_bytes(b"old-verified-video")
        record_bundle_publication(
            self.output, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False)
        seal_complete_delivery(self.output)
        self.assertFalse((self.output / "continuous.flac").exists())
        self.assertTrue(bundle_audio_source_status(self.output)["available"])
        self.assertEqual(bundle_audio_source_status(self.output)["source"], "sealed-mkv")
        mark_ass_style_stale(self.output)
        health = bundle_export_status(self.output)
        self.assertEqual(health["state"], "stale")
        self.assertIn("原 MKV 文件仍在，原 FLAC 音轨保持可复用", health["message"])
        self.assertTrue(health["prerequisites"]["continuous_flac"])
        self.assertEqual(health["prerequisites"]["audio_source"], "sealed-mkv")
        self.assertFalse(health["prerequisites"]["ass_current"])
        self.assertTrue((self.output / OUTPUT).exists())

    def test_modified_sealed_mkv_never_becomes_audio_source(self):
        (self.output / "continuous.flac").write_bytes(b"old-flac")
        (self.output / OUTPUT).write_bytes(b"old-verified-video")
        record_bundle_publication(
            self.output, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False)
        seal_complete_delivery(self.output)
        (self.output / OUTPUT).write_bytes(b"externally-modified-video")
        status = bundle_audio_source_status(self.output)
        self.assertFalse(status["available"])
        self.assertIn("文件身份与封存回执不一致", status["reason"])


if __name__ == "__main__":
    unittest.main()
