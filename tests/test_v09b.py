from __future__ import annotations

import csv
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.pipeline import build_project_v02


def write_wav(path: Path, frames: int = 80) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * frames)


def write_index(path: Path, filename: str, english: str = "Hello") -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=["index", "group", "filename", "english", "sha256"],
        )
        writer.writeheader()
        writer.writerow({
            "index": "1",
            "group": "archive",
            "filename": filename,
            "english": english,
            "sha256": "",
        })


class V09BStageRecoveryTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        wavs = root / "wavs"
        wavs.mkdir()
        write_wav(wavs / "voice.wav")
        index = root / "index.csv"
        write_index(index, "voice.wav")
        output = root / "output"
        return index, wavs, output

    def test_completed_metadata_and_manifest_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            index, wavs, output = self._fixture(Path(td))
            first = build_project_v02(index, wavs, output, make_flac=False)

            expected = {
                "01_scan.json",
                "02_metadata.json",
                "03_translation.json",
                "04_translation_qa.json",
                "05_manifest.json",
                "06_audio_state.json",
                "final_report.json",
            }
            self.assertEqual({path.name for path in (output / "stages").iterdir()}, expected)
            self.assertIn("metadata", first["stage_resume"]["rebuilt"])

            with patch(
                "app.pipeline.build_entries",
                side_effect=AssertionError("metadata must not be rebuilt"),
            ), patch(
                "app.pipeline.ensure_dir_or_extract",
                side_effect=AssertionError("reused stages must not be extracted again"),
            ), patch(
                "app.pipeline.write_manifest",
                side_effect=AssertionError("manifest must not be rebuilt"),
            ):
                second = build_project_v02(index, wavs, output, make_flac=False)

            self.assertIn("metadata", second["stage_resume"]["resumed"])
            self.assertIn("translation", second["stage_resume"]["resumed"])
            self.assertIn("manifest", second["stage_resume"]["resumed"])

    def test_changed_input_invalidates_previous_stages(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            index, wavs, output = self._fixture(Path(td))
            first = build_project_v02(index, wavs, output, make_flac=False)
            old_fingerprint = first["stage_resume"]["input_fingerprint"]

            write_index(index, "voice.wav", english="Changed")
            second = build_project_v02(index, wavs, output, make_flac=False)

            self.assertNotEqual(
                old_fingerprint,
                second["stage_resume"]["input_fingerprint"],
            )
            self.assertIn("metadata", second["stage_resume"]["rebuilt"])
            self.assertEqual(second["stage_resume"]["resumed"], [])

    def test_tampered_manifest_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            index, wavs, output = self._fixture(Path(td))
            build_project_v02(index, wavs, output, make_flac=False)
            (output / "manifest.json").write_text("{}", encoding="utf-8")

            report = build_project_v02(index, wavs, output, make_flac=False)

            self.assertIn("metadata", report["stage_resume"]["resumed"])
            self.assertIn("manifest", report["stage_resume"]["rebuilt"])

    def test_missing_skipped_qa_stage_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            index, wavs, output = self._fixture(Path(td))
            build_project_v02(index, wavs, output, make_flac=False)
            (output / "stages" / "04_translation_qa.json").unlink()

            report = build_project_v02(index, wavs, output, make_flac=False)

            self.assertIn("translation", report["stage_resume"]["resumed"])
            self.assertIn("translation_qa", report["stage_resume"]["rebuilt"])
            self.assertTrue((output / "stages" / "04_translation_qa.json").is_file())

    def test_verified_audio_is_reused_but_tampering_forces_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            index, wavs, output = self._fixture(Path(td))

            def fake_encode(entries, wav_root, path):
                path.write_bytes(b"verified-flac")
                return {"flac_size_bytes": path.stat().st_size, "lossless_pcm_verified": True}

            with patch("app.pipeline.build_continuous_flac", side_effect=fake_encode) as encode:
                build_project_v02(index, wavs, output, make_flac=True)
                self.assertEqual(encode.call_count, 1)

            with patch(
                "app.pipeline.build_continuous_flac",
                side_effect=AssertionError("verified audio must be reused"),
            ):
                resumed = build_project_v02(index, wavs, output, make_flac=True)
            self.assertIn("audio", resumed["stage_resume"]["resumed"])

            (output / "continuous.flac").write_bytes(b"tampered")
            with patch("app.pipeline.build_continuous_flac", side_effect=fake_encode) as encode:
                rebuilt = build_project_v02(index, wavs, output, make_flac=True)
                self.assertEqual(encode.call_count, 1)
            self.assertIn("audio", rebuilt["stage_resume"]["rebuilt"])


if __name__ == "__main__":
    unittest.main()
