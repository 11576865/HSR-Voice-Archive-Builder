"""Real FFmpeg acceptance for FLAC as original voice clips in Quick Create.

Exercises production source inventory, selected package-LAB ordering, decoded
PCM staging, actual continuous FLAC and manifest source provenance.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.builder import ensure_dir_or_extract
from app.flac_source import load_flac_provenance
from app.pipeline import build_project_v02
from app.quick import create_quick_project, quick_scan, source_inventory
from app.wavpcm import parse_wav_pcm


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Real FFmpeg and FFprobe required")
class FlacQuickSourceTests(unittest.TestCase):
    def _voice(self, root: Path, stem: str, *, freq: int, bits: int = 16) -> Path:
        folder = root / "chapter1"
        folder.mkdir(parents=True, exist_ok=True)
        wav = folder / (stem + "-seed.wav")
        flac = folder / (stem + ".flac")
        codec = "pcm_s16le" if bits == 16 else "pcm_s24le"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"sine=frequency={freq}:sample_rate=48000:duration=0.16",
             "-ac", "1", "-c:a", codec, str(wav)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(wav),
             "-map", "0:a:0", "-c:a", "flac", str(flac)],
            check=True, capture_output=True,
        )
        wav.unlink()
        (folder / (stem + ".lab")).write_text(
            f"Voice line {stem}", encoding="utf-8",
        )
        return flac

    def _ready_scan(self, source: Path):
        with patch(
            "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
            side_effect=AssertionError("same-stem LAB must not trigger remote WAV-index lookup"),
        ), patch(
            "app.quick.credentials_status",
            return_value={"configured": True, "provider": "test",
                          "base_url": "https://example.invalid"},
        ):
            plan = quick_scan(source)
            self.assertTrue(plan["ready"], plan["blockers"])
            self.assertEqual(plan["index"]["source"], "primary-package-lab")
            return plan

    def test_extracted_flac_folder_scans_and_builds_real_verified_flac(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "flac-only"
            first = self._voice(source, "archive_mar7th_1", freq=440)
            second = self._voice(source, "archive_mar7th_2", freq=660)
            hashes = {_sha(p) for p in (first, second)}
            inventory = source_inventory(source)
            self.assertEqual(inventory["wav_count"], 2)
            self.assertEqual(inventory["native_wav_count"], 0)
            self.assertEqual(inventory["flac_count"], 2)
            self.assertEqual(inventory["lab_count"], 2)
            self.assertEqual(inventory["wav_lab_member_pairs"], 2)
            self.assertEqual(
                set(inventory["wav_members"]),
                {"chapter1/archive_mar7th_1.wav", "chapter1/archive_mar7th_2.wav"},
            )
            self._ready_scan(source)
            with patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                side_effect=AssertionError("no remote lookup should be necessary"),
            ):
                config, plan = create_quick_project(source, root=root / "project")
            self.assertEqual(plan["index"]["source"], "primary-package-lab")
            from app.project import resolve_project_path
            index = resolve_project_path(config, config.index_csv)
            report = build_project_v02(
                index, source, root / "project" / "output",
                state_dir=root / "project" / ".state",
                make_flac=True, translate_missing=False,
                delivery_mode="flac_only",
            )
            self.assertTrue(report["lossless_pcm_verified"])
            self.assertEqual({_sha(p) for p in (first, second)}, hashes)
            manifest = json.loads(
                (root / "project" / "output" / "manifest.json").read_text(encoding="utf-8")
            )
            entries = manifest["entries"]
            self.assertEqual(len(entries), 2)
            original_by_stem = {p.stem: p for p in (first, second)}
            for e in entries:
                self.assertTrue(e["source_member_id"].endswith(".wav"))
                self.assertTrue(e["original_source_member_id"].endswith(".flac"))
                self.assertEqual(
                    e["original_source_sha256"],
                    _sha(original_by_stem[Path(e["filename"]).stem]),
                )
                self.assertEqual(len(e["sha256"]), 64)
            self.assertTrue((root / "project" / "output" / "continuous.flac").is_file())

            # Resuming the same source must retain exact identity and audio.
            rerun = build_project_v02(
                index, source, root / "project" / "output",
                state_dir=root / "project" / ".state",
                make_flac=True, translate_missing=False,
                delivery_mode="flac_only",
            )
            self.assertTrue(rerun["lossless_pcm_verified"])

    def test_mixed_zip_flac_and_wav_stages_without_mutating_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tree = root / "inputs"
            flac = self._voice(tree, "archive_mar7th_1", freq=440, bits=24)
            wav = tree / "chapter1" / "archive_mar7th_2.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "sine=frequency=660:sample_rate=48000:duration=0.16",
                 "-c:a", "pcm_s24le", str(wav)],
                check=True, capture_output=True,
            )
            (wav.with_suffix(".lab")).write_text("Voice line two", encoding="utf-8")
            archive = root / "voices.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                for p in sorted(tree.rglob("*")):
                    if p.is_file():
                        zipped.write(p, p.relative_to(tree).as_posix())
            archive_hash = _sha(archive)
            inventory = source_inventory(archive)
            self.assertEqual(inventory["wav_count"], 2)
            self.assertEqual(inventory["flac_count"], 1)
            self.assertEqual(inventory["native_wav_count"], 1)
            self._ready_scan(archive)
            with tempfile.TemporaryDirectory() as work:
                view = ensure_dir_or_extract(archive, Path(work), "wavs")
                original = view / "chapter1" / "archive_mar7th_1.wav"
                self.assertTrue(original.is_file())
                self.assertEqual(parse_wav_pcm(original).bits_per_sample, 24)
                self.assertEqual(parse_wav_pcm(view / "chapter1" / wav.name).bits_per_sample, 24)
                provenance = load_flac_provenance(view)
                self.assertEqual(
                    provenance["chapter1/archive_mar7th_1.wav"]["original_sha256"],
                    _sha(flac),
                )
            self.assertEqual(_sha(archive), archive_hash)

    def test_same_stem_wav_flac_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._voice(root, "voice", freq=440)
            other = root / "chapter1" / "voice.wav"
            other.write_bytes(b"other")
            with self.assertRaisesRegex(ValueError, "WAV/FLAC"):
                source_inventory(root)
            with tempfile.TemporaryDirectory() as work:
                with self.assertRaisesRegex(ValueError, "WAV/FLAC"):
                    ensure_dir_or_extract(root, Path(work), "wavs")
            self.assertEqual(other.read_bytes(), b"other")

    def test_corrupt_flac_fails_closed_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "voice"
            source.mkdir()
            corrupted = source / "line.flac"
            corrupted.write_bytes(b"not a FLAC stream")
            (source / "line.lab").write_text("Fake input", encoding="utf-8")
            self.assertEqual(source_inventory(source)["wav_count"], 1)
            with tempfile.TemporaryDirectory() as work:
                with self.assertRaisesRegex(RuntimeError, "FLAC"):
                    ensure_dir_or_extract(source, Path(work), "wavs")
            self.assertEqual(corrupted.read_bytes(), b"not a FLAC stream")


if __name__ == "__main__":
    unittest.main()
