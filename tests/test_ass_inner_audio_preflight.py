"""Audio source preflight for explicit *internal* ASS -> complete MKV export.

A valid sealed but subtitle-stale MKV remains a usable lossless audio source;
a loose/unknown MKV is not provenance. Refuse before rewriting ASS/SRT/receipt.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.archive_bundle_lifecycle import (
    OUTPUT, RECEIPT, bundle_audio_source_status, bundle_export_status,
    diagnose_bundle_audio_source,
    record_bundle_publication, require_bound_voice_timeline,
)
from app.auto_delivery import queue_ass_regeneration
from app.delivery_artifacts import (
    seal_complete_delivery, verify_internal_ass_audio_source,
)
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

    def test_nonempty_but_broken_flac_rejects_before_any_subtitle_mutation(self):
        (self.output / "continuous.flac").write_bytes(
            b"nonempty-but-unparseable-lossless-audio"
        )
        tracked = ("manifest.json", "HSR_Voice_Archive.ass",
                   "HSR_Voice_Archive.srt", "subtitle_export_receipt.json")
        original = {name: (self.output / name).read_bytes()
                    for name in tracked}
        with patch("app.auto_delivery.create_job",
                   side_effect=lambda _kind, runner, **kwargs:
                       runner(lambda *args: None)), \
             patch("app.subtitles.refresh_subtitle_artifacts_from_settings") as render:
            with self.assertRaisesRegex(ValueError, "FFprobe"):
                queue_ass_regeneration(self.config, self.output)
            render.assert_not_called()
        self.assertEqual(original, {
            name: (self.output / name).read_bytes() for name in tracked
        })

    def test_non_flac_primary_track_is_rejected_before_subtitle_mutation(self):
        (self.output / "continuous.flac").write_bytes(b"placeholder")
        with patch("app.delivery_artifacts._probe",
                   return_value=[
                       {"codec_type": "audio", "codec_name": "aac"},
                   ]):
            with self.assertRaisesRegex(ValueError, "第一条音轨不是预期"):
                verify_internal_ass_audio_source(self.output)
        self.assertEqual((self.output / "HSR_Voice_Archive.ass").read_text("utf-8"),
                         "old-ass")

    def test_verified_flac_source_passes_bounded_preflight(self):
        (self.output / "continuous.flac").write_bytes(b"fake-with-mocked-probe")
        with patch("app.delivery_artifacts._probe",
                   return_value=[{"codec_type": "video", "codec_name": "h264"},
                                 {"codec_type": "audio", "codec_name": "flac"}]):
            source = verify_internal_ass_audio_source(self.output)
        self.assertEqual(source["source"], "independent-flac")

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

    def test_sealed_archive_rejects_modified_voice_clocks_before_mutation(self):
        import json
        (self.output / "manifest.json").write_text(json.dumps({"entries": [
            {"index": 1, "start_seconds": 10.0,
             "audio_end_seconds": 12.0, "english": "Before"},
        ]}), encoding="utf-8")
        (self.output / "continuous.flac").write_bytes(b"fixture-flac")
        (self.output / OUTPUT).write_bytes(b"sealed-fixture-mkv")
        record_subtitle_export(self.output, state="current", ass_required=True)
        record_bundle_publication(
            self.output, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False)
        seal_complete_delivery(self.output)
        # Legal user subtitle changes don't alter voice-derived time anchors.
        (self.output / "manifest.json").write_text(json.dumps({"entries": [
            {"index": 1, "start_seconds": 10.0,
             "audio_end_seconds": 12.0, "english": "Edited subtitle"},
        ]}), encoding="utf-8")
        require_bound_voice_timeline(self.output)
        # But rewriting the PCM timeline after MKV publication is not legal.
        (self.output / "manifest.json").write_text(json.dumps({"entries": [
            {"index": 1, "start_seconds": 11.0,
             "audio_end_seconds": 12.0, "english": "Edited subtitle"},
        ]}), encoding="utf-8")
        names = ("manifest.json", "subtitle_export_receipt.json")
        previous = {name: (self.output / name).read_bytes() for name in names}
        with patch("app.auto_delivery.create_job",
                   side_effect=lambda _kind, runner, **kwargs:
                       runner(lambda *args: None)), \
             patch("app.subtitles.refresh_subtitle_artifacts_from_settings") as render:
            with self.assertRaisesRegex(ValueError, "语音时间轴.*不一致"):
                queue_ass_regeneration(self.config, self.output)
            render.assert_not_called()
        self.assertEqual(previous, {
            name: (self.output / name).read_bytes() for name in names
        })
        self.assertTrue((self.output / OUTPUT).is_file())

    def test_legacy_sealed_receipt_without_timeline_binding_stays_readable(self):
        import json
        (self.output / "continuous.flac").write_bytes(b"fixture-flac")
        (self.output / OUTPUT).write_bytes(b"existing-legacy-mkv")
        record_bundle_publication(
            self.output, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False)
        seal_complete_delivery(self.output)
        receipt = self.output / RECEIPT
        record = json.loads(receipt.read_text("utf-8"))
        record.pop("voice_timeline_sha256", None)
        receipt.write_text(json.dumps(record), encoding="utf-8")
        require_bound_voice_timeline(self.output)

    def test_published_sha_accepts_only_byte_identical_mkv_after_mtime_change(self):
        (self.output / "continuous.flac").write_bytes(b"working-flac")
        (self.output / OUTPUT).write_bytes(b"authentic-media-contents")
        record_bundle_publication(
            self.output, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False)
        seal_complete_delivery(self.output)
        mkv = self.output / OUTPUT
        initial = mkv.stat()
        os.utime(mkv, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000))
        self.assertTrue(bundle_audio_source_status(self.output, prefer_sealed=True)["available"])
        self.assertEqual(bundle_audio_source_status(self.output, prefer_sealed=True)["source"],
                         "sealed-mkv")
        # Keep size and even restore the original mtime: changed bytes cannot
        # pass the content digest recorded at publication.
        mkv.write_bytes(b"tamperedxxmedia-contents")
        os.utime(mkv, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        status = bundle_audio_source_status(self.output, prefer_sealed=True)
        self.assertFalse(status["available"])
        self.assertIn("文件身份与封存回执不一致", status["reason"])

    def test_legacy_receipt_mtime_mismatch_remains_unverified(self):
        import json
        (self.output / "continuous.flac").write_bytes(b"working-flac")
        (self.output / OUTPUT).write_bytes(b"legacy-media")
        record_bundle_publication(
            self.output, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False)
        seal_complete_delivery(self.output)
        receipt_file = self.output / RECEIPT
        record = json.loads(receipt_file.read_text(encoding="utf-8"))
        record.pop("artifact_sha256")
        receipt_file.write_text(json.dumps(record), encoding="utf-8")
        mkv = self.output / OUTPUT
        original = mkv.stat()
        os.utime(mkv, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000))
        status = bundle_audio_source_status(self.output, prefer_sealed=True)
        self.assertFalse(status["available"])

    def test_invalid_sealed_mkv_blocks_internal_but_preserves_external_ass(self):
        import json
        from app.subtitles import export_independent_ass

        (self.output / "manifest.json").write_text(json.dumps({"entries": [
            {"index": 17, "english": "One archived line", "chinese": "一句译文",
             "start_seconds": 1.0, "audio_end_seconds": 2.0,
             "display_end_seconds": 2.0},
        ]}), encoding="utf-8")
        (self.output / "continuous.flac").write_bytes(b"flac-fixture")
        # The manifest changed after setUp's export receipt; re-certify the
        # exact test source before sealing, as the real pipeline does.
        record_subtitle_export(self.output, state="current", ass_required=True)
        (self.output / OUTPUT).write_bytes(b"verified-old-media")
        record_bundle_publication(
            self.output, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False)
        seal_complete_delivery(self.output)
        (self.output / OUTPUT).write_bytes(b"replaced-old-media")
        original = (self.output / OUTPUT).read_bytes()
        self.assertFalse(bundle_audio_source_status(self.output)["available"])
        with self.assertRaisesRegex(ValueError, "文件身份与封存回执"):
            verify_internal_ass_audio_source(self.output)
        def mock_ass(_adapters, target, **_kwargs):
            target.write_text("[Events]\\n", encoding="utf-8")
        with patch("app.subtitles.write_ass", side_effect=mock_ass):
            external = export_independent_ass(self.config, self.output)
        self.assertTrue(Path(external["ass_file"]).is_file())
        self.assertTrue(external["mkv_untouched"])
        self.assertEqual((self.output / OUTPUT).read_bytes(), original)
        self.assertFalse((self.output / "continuous.flac").exists())

    def test_audio_integrity_report_separates_legacy_and_content_mismatch(self):
        (self.output / "continuous.flac").write_bytes(b"original-audio")
        (self.output / OUTPUT).write_bytes(b"verified-archive")
        record_bundle_publication(
            self.output, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False)
        seal_complete_delivery(self.output)
        status = diagnose_bundle_audio_source(self.output)
        self.assertEqual(status["code"], "verified_sealed_mkv")
        self.assertTrue(status["digest_bound"])
        self.assertTrue(status["artifact_verified"])
        (self.output / OUTPUT).write_bytes(b"modified-archive")
        changed = diagnose_bundle_audio_source(self.output)
        self.assertEqual(changed["code"], "artifact_content_mismatch")
        self.assertFalse(changed["artifact_verified"])
        self.assertTrue(changed["external_ass_possible"])
        self.assertEqual(changed["recommended_action"], "rebuild_from_original_sources")
        import json
        receipt_file = self.output / RECEIPT
        data = json.loads(receipt_file.read_text("utf-8"))
        data.pop("artifact_sha256")
        receipt_file.write_text(json.dumps(data), encoding="utf-8")
        legacy = diagnose_bundle_audio_source(self.output)
        self.assertEqual(legacy["code"], "legacy_stat_mismatch")
        self.assertFalse(legacy["digest_bound"])
        self.assertIn("无法判断", legacy["detail"])
        self.assertEqual((self.output / OUTPUT).read_bytes(), b"modified-archive")

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
