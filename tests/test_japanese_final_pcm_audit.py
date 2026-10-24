"""Exact media-backed full-stream PCM verification for restored JP products."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.japanese_final_pcm_audit import verify_japanese_final_audio_pcm
from app.pipeline import build_project_v02
from app.project import ProjectConfig
from tests.test_japanese_listening_pack import JapaneseListeningPackTests


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Requires actual FFmpeg and FFprobe")
class JapaneseFinalPcmAuditTests(unittest.TestCase):
    def build(self, base: Path) -> tuple[ProjectConfig, Path, Path]:
        with patch("app.project.STATE_FILE", base / "app_recent.json"):
            recovered, _original, source = JapaneseListeningPackTests()._fixture(base)
            out = Path(recovered.root) / "output"
            build_project_v02(
                Path(recovered.root) / ".generated" / "quick_index.csv",
                source, out, make_flac=True, delivery_mode="flac_only",
                translate_missing=False, audio_language="ja",
                source_text_language="ja", target_language="ja",
                state_dir=Path(recovered.root) / ".state",
            )
        return recovered, source, out

    def test_full_flac_stream_matches_pre_encoding_pcm_and_all_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, original, output = self.build(root)
            progress: list[tuple[str, int, int]] = []
            result = verify_japanese_final_audio_pcm(
                config, report_progress=lambda phase, _message, current, total:
                    progress.append((phase, current, total)),
            )
            manifest = json.loads((output / "manifest.json").read_text("utf-8"))
            self.assertEqual(result["status"], "final-pcm-lossless-verified")
            self.assertEqual(result["expected_pcm_sha256"], manifest["report"]["assembled_pcm_sha256"])
            self.assertEqual(result["expected_pcm_sha256"], result["decoded_final_pcm_sha256"])
            self.assertEqual(result["verified_frames"], manifest["report"]["pcm_frames_written"])
            self.assertEqual(result["original_members"], 3)
            self.assertEqual(result["coverage"], "all-decoded-final-audio-frames")
            self.assertGreater(result["verified_bytes"], 0)
            self.assertFalse(result["spoken_japanese_verified"])
            self.assertFalse(result["translation_semantics_verified"])
            self.assertEqual(progress[-1][0], "complete")
            self.assertTrue(original.is_file())

    def test_modified_valid_flac_is_rejected_even_when_manifest_identity_is_same(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, original, output = self.build(root)
            original_pcm = verify_japanese_final_audio_pcm(config)
            output_flac = output / "continuous.flac"
            changed = output / "changed.flac"
            # Full-length, valid but *different* PCM still decodes successfully.
            seconds = original_pcm["verified_frames"] / original_pcm["sample_rate"]
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                 "-i", "anullsrc=r=16000:cl=mono", "-t", str(seconds),
                 "-c:a", "flac", str(changed)],
                check=True, capture_output=True,
            )
            changed.replace(output_flac)
            with self.assertRaisesRegex(RuntimeError, "PCM 与编码前来源不一致"):
                verify_japanese_final_audio_pcm(config)
            # Rejected without writing into original audio or manifest.
            self.assertTrue(original.is_file())
            self.assertTrue((output / "manifest.json").is_file())

    def test_real_matroska_flac_remux_decodes_to_original_assembled_pcm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _source, output = self.build(root)
            target = output / "remuxed.mkv"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y",
                 "-i", str(output / "continuous.flac"),
                 "-map", "0:a:0", "-c:a", "copy", str(target)],
                check=True, capture_output=True,
            )
            # The same media pipeline is exercised on actual MKV bitstreams;
            # only the pre-existing MKV publication receipt guard is bypassed
            # because this test fixture is a synthetic, unpublished remux.
            with patch("app.japanese_final_pcm_audit._media", return_value=(target, "single-flac")):
                result = verify_japanese_final_audio_pcm(config)
            self.assertEqual(result["media_file"], "remuxed.mkv")
            self.assertEqual(result["status"], "final-pcm-lossless-verified")

    def test_bad_builder_digest_and_tampered_provenance_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, source, output = self.build(root)
            manifest = output / "manifest.json"
            data = json.loads(manifest.read_text("utf-8"))
            data["report"]["assembled_pcm_sha256"] = "0" * 64
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "一致性证据"):
                verify_japanese_final_audio_pcm(config)
            with source.open("ab") as f:
                f.write(b"modified after recovery")
            with self.assertRaisesRegex(ValueError, "发生变化"):
                verify_japanese_final_audio_pcm(config)

    def test_server_adapters_and_media_ci_enforce_full_final_pcm_check(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("server.py", "lite_server.py"):
            with self.subTest(server=name):
                text = (root / "app" / name).read_text("utf-8")
                self.assertIn("/api/recovery/verify-finished-pcm", text)
                self.assertIn('verify_japanese_final_audio_pcm(', text)
                self.assertIn('"jp-full-audio-audit"', text)
        workflow = (root / ".github/workflows/tests.yml").read_text("utf-8")
        self.assertIn("tests.test_japanese_final_pcm_audit", workflow)


if __name__ == "__main__":
    unittest.main()
