from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from app.builder import Entry, build_continuous_flac, extract_archive


def write_pcm_wav(path: Path, samples: list[int], sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join(int(x).to_bytes(2, "little", signed=True) for x in samples)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw)


def entry(index: int, filename: str, frames: int, start: int, next_start: int) -> Entry:
    sr = 8000
    audio_end = start + frames
    return Entry(
        index=index,
        group="g",
        filename=filename,
        source="test",
        source_detail="",
        english=f"line {index}",
        chinese=f"行 {index}",
        chinese_source="test",
        sample_rate=sr,
        channels=1,
        sample_width_bits=16,
        source_frames=frames,
        source_duration_seconds=frames / sr,
        start_sample=start,
        audio_end_sample=audio_end,
        next_start_sample=next_start,
        start_seconds=start / sr,
        audio_end_seconds=audio_end / sr,
        display_end_seconds=audio_end / sr,
        sha256="",
    )


class V04ReliabilityTests(unittest.TestCase):
    def test_zip_traversal_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("../escape.txt", "no")
            with self.assertRaises(ValueError):
                extract_archive(archive, root / "out")
            self.assertFalse((root / "escape.txt").exists())

    def test_zip_uncompressed_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "large.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("safe.txt", "0123456789")
            old = os.environ.get("HSR_MAX_EXTRACT_BYTES")
            os.environ["HSR_MAX_EXTRACT_BYTES"] = "5"
            try:
                with self.assertRaises(ValueError):
                    extract_archive(archive, root / "out")
            finally:
                if old is None:
                    os.environ.pop("HSR_MAX_EXTRACT_BYTES", None)
                else:
                    os.environ["HSR_MAX_EXTRACT_BYTES"] = old

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_flac_is_streamed_verified_and_atomically_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            wavs.mkdir()
            write_pcm_wav(wavs / "a.wav", [100, -100, 200, -200] * 100)
            write_pcm_wav(wavs / "b.wav", [300, -300] * 100)
            gap = 80
            a_frames = 400
            b_frames = 200
            entries = [
                entry(1, "a.wav", a_frames, 0, a_frames + gap),
                entry(2, "b.wav", b_frames, a_frames + gap, a_frames + gap + b_frames),
            ]
            out = root / "continuous.flac"
            report = build_continuous_flac(entries, wavs, out)
            self.assertTrue(out.is_file())
            self.assertTrue(report["lossless_pcm_verified"])
            self.assertTrue(report["pcm_streamed_directly"])
            self.assertEqual(report["pcm_frames_written"], a_frames + gap + b_frames)
            self.assertEqual(
                report["assembled_pcm_sha256"],
                report["decoded_flac_pcm_sha256"],
            )
            self.assertFalse((root / "continuous.partial.flac").exists())


if __name__ == "__main__":
    unittest.main()
