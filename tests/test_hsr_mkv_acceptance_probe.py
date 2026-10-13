"""Technical evidence probe contracts: no device playback claims from CI."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import hsr_mkv_acceptance_probe as probe


def valid_ffprobe() -> dict:
    return {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
            {"codec_type": "audio", "codec_name": "flac"},
            {"codec_type": "subtitle", "codec_name": "ass"},
            {"codec_type": "attachment", "tags": {
                "filename": "Archive.ttf", "mimetype": "application/x-truetype-font",
            }},
        ],
        "format": {"format_name": "matroska,webm", "duration": "2.0"},
    }


class TechnicalEvidenceContractTests(unittest.TestCase):
    def test_accepted_tracks_and_font_identity(self):
        errors, info = probe._verify_streams(valid_ffprobe())
        self.assertEqual(errors, [])
        self.assertEqual(info["attachment_names"], ["Archive.ttf"])
        self.assertEqual(info["video_codec"], "h264")
        self.assertEqual(info["subtitle_codec"], "ass")

    def test_reject_wrong_track_container_font_and_duration(self):
        cases = []
        no_flac = valid_ffprobe()
        no_flac["streams"] = [s for s in no_flac["streams"] if s["codec_type"] != "audio"]
        cases.append(no_flac)
        wrong_container = valid_ffprobe()
        wrong_container["format"]["format_name"] = "mov,mp4"
        cases.append(wrong_container)
        bad_font = valid_ffprobe()
        bad_font["streams"][3]["tags"]["filename"] = "../unsafe.ttf"
        cases.append(bad_font)
        bad_duration = valid_ffprobe()
        bad_duration["format"]["duration"] = "NaN"
        cases.append(bad_duration)
        wrong_subtitle = valid_ffprobe()
        wrong_subtitle["streams"][2]["codec_name"] = "subrip"
        cases.append(wrong_subtitle)
        for i, item in enumerate(cases):
            with self.subTest(case=i):
                errors, _ = probe._verify_streams(item)
                self.assertTrue(errors)

    def test_missing_source_does_not_call_ffmpeg_or_claim_device_support(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(probe, "_version", return_value="ffmpeg fake"), \
             patch.object(probe, "_filters", return_value={}), \
             patch.object(probe, "_device_info", return_value={"environment": "test"}), \
             patch.object(probe, "_run") as invoke:
            result, images = probe.inspect(Path(td) / "not-found.mkv",
                                           ffmpeg="ffmpeg", ffprobe="ffprobe")
        self.assertEqual(result["status"], "technical_fail")
        self.assertEqual(result["device_playback"], "unverified")
        self.assertEqual(result["visual_banding"], "unverified")
        self.assertEqual(result["long_duration_performance"], "unverified")
        self.assertTrue(result["errors"])
        self.assertEqual(images, {})
        invoke.assert_not_called()

    def test_ffprobe_ok_decode_failure_does_not_claim_pass(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(probe, "_version", return_value="ffmpeg fake"), \
             patch.object(probe, "_filters", return_value={}), \
             patch.object(probe, "_device_info", return_value={"environment": "test"}), \
             patch.object(probe, "_run", return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps(valid_ffprobe()).encode(), stderr=b"",
             )), \
             patch.object(probe, "_decode_png", side_effect=RuntimeError("bad encoded frame")):
            path = Path(td) / "source.mkv"
            path.write_bytes(b"mock media")
            result, frames = probe.inspect(path, ffmpeg="ffmpeg", ffprobe="ffprobe")
        self.assertEqual(result["status"], "technical_fail")
        self.assertEqual(result["device_playback"], "unverified")
        self.assertTrue(any("bad encoded frame" in x for x in result["errors"]))
        self.assertEqual(frames, {})

    def test_evidence_written_separately_no_intermediate_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / "archive.mkv"
            original.write_bytes(b"unchanged")
            before = original.read_bytes()
            evidence = root / "evidence"
            png = probe.PNG_HEADER + b"fixture"
            info = {"status": "technical_pass", "device_playback": "unverified"}
            probe._save_evidence(evidence, info, {"decoded_h264_0.png": png})
            self.assertEqual((evidence / "decoded_h264_0.png").read_bytes(), png)
            self.assertEqual(json.loads((evidence / "report.json").read_text())["device_playback"],
                             "unverified")
            self.assertFalse((evidence / "report.json.part").exists())
            self.assertEqual(original.read_bytes(), before)

    def test_no_dependency_fails_closed(self):
        with patch.object(probe.shutil, "which", return_value=None):
            self.assertEqual(probe.main(["missing.mkv"]), 2)

    def test_ffmpeg_filter_parser_detects_available_filters(self):
        output = (b" T.. gradients        |->V       Draw a gradient.\n"
                  b" ... gblur            V->V       Apply Gaussian Blur.\n"
                  b" ... ass              V->V       Render ASS.\n")
        with patch.object(probe, "_run", return_value=SimpleNamespace(
            returncode=0, stdout=output, stderr=b"",
        )):
            status = probe._filters("ffmpeg")
        self.assertEqual(status, {"gradients": True, "gblur": True, "ass": True})


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Requires actual FFmpeg and FFprobe")
class RealEncodedArtifactTests(unittest.TestCase):
    def test_probe_decodes_two_ends_of_real_h264_flac_ass_mkv(self):
        with tempfile.TemporaryDirectory(prefix="hsr-device-probe-test-") as td:
            root = Path(td)
            subtitle = root / "fixture.ass"
            subtitle.write_text(
                "[Script Info]\nScriptType: v4.00+\nPlayResX: 160\nPlayResY: 90\n"
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                "Style: Default,Arial,18,&H00FFFFFF,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,5,5,5,1\n"
                "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:00.00,0:00:01.80,Default,,0,0,0,,Technical Evidence\n",
                encoding="utf-8",
            )
            mkv = root / "fixture.mkv"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=1:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-i", str(subtitle),
                "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "flac", "-c:s", "ass", "-t", "2", str(mkv),
            ], check=True, capture_output=True, timeout=45)
            fingerprint = hashlib.sha256(mkv.read_bytes()).hexdigest()
            evidence = root / "evidence"
            exit_code = probe.main([str(mkv), "--evidence-dir", str(evidence)])
            self.assertEqual(exit_code, 0)
            report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "technical_pass")
            self.assertEqual(report["device_playback"], "unverified")
            self.assertEqual(report["visual_banding"], "unverified")
            self.assertEqual(report["media"]["video_codec"], "h264")
            self.assertEqual(report["media"]["audio_codec"], "flac")
            self.assertEqual(report["media"]["subtitle_codec"], "ass")
            self.assertEqual(len(report["samples"]), 2)
            for item in report["samples"]:
                png = (evidence / item["png_name"]).read_bytes()
                self.assertTrue(png.startswith(probe.PNG_HEADER))
                self.assertEqual(hashlib.sha256(png).hexdigest(),
                                 item["decoded_png_sha256"])
            self.assertEqual(hashlib.sha256(mkv.read_bytes()).hexdigest(), fingerprint)


if __name__ == "__main__":
    unittest.main()
