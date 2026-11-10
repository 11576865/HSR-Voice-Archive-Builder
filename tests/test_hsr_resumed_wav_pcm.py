"""Real-FFmpeg regression for resuming a mixed-rate WAV archive.

A metadata stage may contain 44.1 kHz normalized entry metadata even though
a new extraction of the unchanged source ZIP still contains the original
48 kHz member. Resuming must reconstruct and verify the normalized PCM without
modifying the original input or trusting duration alone.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.builder import (
    Entry, _normalize_wav_pcm, build_continuous_flac, sha256_file,
)
from app.wavpcm import parse_wav_pcm


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Requires real FFmpeg")
class ResumedMixedRateWavTests(unittest.TestCase):
    def test_exact_48k_to_44100_reconstruction_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wav_root = root / "extracted_again"
            wav_root.mkdir()
            original = wav_root / "archive_mar7th_20.wav"

            def make_source(frequency: int) -> None:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error", "-nostdin",
                        "-f", "lavfi",
                        "-i", f"sine=frequency={frequency}:sample_rate=48000",
                        "-t", "5.778395833333333", "-ac", "1", "-c:a", "pcm_s16le",
                        str(original),
                    ],
                    check=True, capture_output=True,
                )

            make_source(440)
            original_before = original.read_bytes()
            first = parse_wav_pcm(original)
            self.assertEqual((first.sample_rate, first.channels,
                              first.bits_per_sample, first.frames),
                             (48000, 1, 16, 277363))

            # Mirrors the metadata stage's private, already-normalized copy.
            normalized = root / "metadata_normalized.wav"
            shutil.copyfile(original, normalized)
            _normalize_wav_pcm(normalized, 44100, 1, 2)
            expected = parse_wav_pcm(normalized)
            self.assertEqual((expected.sample_rate, expected.channels,
                              expected.bits_per_sample, expected.frames),
                             (44100, 1, 16, 254828))

            saved = Entry(
                index=1, group="g", filename=original.name, source="test",
                source_detail="", english="voice", chinese="语音",
                chinese_source="test", sample_rate=44100, channels=1,
                sample_width_bits=16, source_frames=expected.frames,
                source_duration_seconds=expected.frames / 44100,
                start_sample=0, audio_end_sample=expected.frames,
                next_start_sample=expected.frames, start_seconds=0,
                audio_end_seconds=expected.frames / 44100,
                display_end_seconds=expected.frames / 44100,
                sha256=sha256_file(normalized),
                source_member_id=original.name,
            )
            output = root / "continuous.flac"
            report = build_continuous_flac([saved], wav_root, output)
            self.assertTrue(report["lossless_pcm_verified"])
            self.assertEqual(report["pcm_frames_written"], expected.frames)
            self.assertEqual(original.read_bytes(), original_before,
                             "Input WAV must not be resampled in place on resume")
            self.assertEqual(list(root.glob(".hsr-pcm-stream-*")), [],
                             "Normalized working files must be cleaned after mux")

            published = output.read_bytes()
            # The same manifest cannot authenticate a newly substituted,
            # equally long WAV. A duration-only allowance would be unsafe.
            make_source(880)
            self.assertEqual(parse_wav_pcm(original).frames, first.frames)
            with self.assertRaisesRegex(RuntimeError, "PCM identity does not match"):
                build_continuous_flac([saved], wav_root, output)
            self.assertEqual(output.read_bytes(), published,
                             "Last good FLAC must survive failed rebuild")
            self.assertFalse((root / "continuous.partial.flac").exists())


if __name__ == "__main__":
    unittest.main()
