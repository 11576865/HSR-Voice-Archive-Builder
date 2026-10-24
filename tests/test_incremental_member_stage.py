"""Integration acceptance for member-safe FastAPI/Termux incremental WAV staging."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.incremental_stage import prepare_incremental_audio_stage
from app.remote_apply_generation import publish_audio_generation


WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 32


def download_file(incoming: Path, filename: str = "incoming.wav"):
    incoming.mkdir(parents=True, exist_ok=True)
    path = incoming / filename
    path.write_bytes(WAV)
    return {
        "completed": [{
            "filename": filename,
            "status": "ok",
            "path": str(path),
            "sha256": hashlib.sha256(WAV).hexdigest(),
        }],
        "failed": [],
    }


class IncrementalMemberSafeStageTests(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        original = root / "original"
        (original / "chapter1").mkdir(parents=True)
        (original / "chapter2").mkdir()
        (original / "chapter1" / "same.wav").write_bytes(b"first original")
        (original / "chapter2" / "same.wav").write_bytes(b"second original")
        (original / "chapter1" / "same.lab").write_text("日本語一", encoding="utf-8")
        (original / "chapter2" / "same.lab").write_text("日本語二", encoding="utf-8")
        return original

    def test_preserves_same_basename_members_in_distinct_chapters_and_new_root_voice(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = self._source(base)
            downloaded = download_file(base / "incoming")
            generated = base / ".generated"
            working = generated / "combined-audio-000001"
            working.mkdir(parents=True)
            staged = prepare_incremental_audio_stage(
                source, working, downloaded, base / "incoming"
            )
            result = publish_audio_generation(generated, staged, working.name)
            self.assertEqual(
                (result / "chapter1" / "same.wav").read_bytes(),
                b"first original",
            )
            self.assertEqual(
                (result / "chapter2" / "same.wav").read_bytes(),
                b"second original",
            )
            self.assertEqual((result / "incoming.wav").read_bytes(), WAV)
            self.assertEqual((result / "chapter1" / "same.lab").read_text(encoding="utf-8"), "日本語一")
            self.assertEqual((result / "chapter2" / "same.lab").read_text(encoding="utf-8"), "日本語二")
            self.assertFalse((result / "same.wav").exists())
            self.assertEqual((source / "chapter1" / "same.wav").read_bytes(), b"first original")
            self.assertEqual((source / "chapter2" / "same.wav").read_bytes(), b"second original")

    def test_zip_source_preserves_both_member_names_not_flattened(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "source.zip"
            with zipfile.ZipFile(archive, "w") as pack:
                pack.writestr("chapter1/same.wav", b"first")
                pack.writestr("chapter2/same.wav", b"second")
                pack.writestr("chapter1/same.lab", "日本語一")
                pack.writestr("chapter2/same.lab", "日本語二")
            incoming = base / "incoming"
            receipt = download_file(incoming)
            working = base / ".generated" / "combined-audio-zip"
            working.mkdir(parents=True)
            staged = prepare_incremental_audio_stage(archive, working, receipt, incoming)
            self.assertEqual((staged / "chapter1" / "same.wav").read_bytes(), b"first")
            self.assertEqual((staged / "chapter2" / "same.wav").read_bytes(), b"second")
            self.assertEqual((staged / "incoming.wav").read_bytes(), WAV)
            self.assertEqual((staged / "chapter1" / "same.lab").read_text(encoding="utf-8"), "日本語一")
            self.assertEqual((staged / "chapter2" / "same.lab").read_text(encoding="utf-8"), "日本語二")
            self.assertEqual((archive.stat().st_size), len(archive.read_bytes()))

    def test_preexisting_nested_basename_collision_rejected_before_publishing(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = self._source(base)
            incoming = base / "incoming"
            receipt = download_file(incoming, "same.wav")
            generated = base / ".generated"
            working = generated / "combined-audio-collision"
            working.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "already exists"):
                prepare_incremental_audio_stage(source, working, receipt, incoming)
            self.assertFalse((generated / "combined_audio_versions").exists())
            self.assertEqual((source / "chapter1" / "same.wav").read_bytes(), b"first original")

    def test_unverified_download_is_refused_before_copying_any_source(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = self._source(base)
            incoming = base / "incoming"
            receipt = download_file(incoming)
            receipt["completed"][0]["sha256"] = "0" * 64
            working = base / ".generated" / "combined-audio-bad"
            working.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "changed after"):
                prepare_incremental_audio_stage(source, working, receipt, incoming)
            self.assertFalse((working / "combined").exists())
            self.assertEqual((source / "chapter1" / "same.wav").read_bytes(), b"first original")

    def test_symlink_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = self._source(base)
            link = source / "chapter1" / "link.wav"
            try:
                link.symlink_to(base / "outside.wav")
            except (NotImplementedError, OSError):
                self.skipTest("Symlinks unavailable")
            incoming = base / "incoming"
            receipt = download_file(incoming)
            working = base / ".generated" / "combined-audio-sym"
            working.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                prepare_incremental_audio_stage(source, working, receipt, incoming)
            self.assertFalse((base / ".generated" / "combined_audio_versions").exists())

    def test_both_http_adapters_use_same_stage_and_member_aware_index(self):
        root = Path(__file__).resolve().parents[1]
        for adapter in ("server.py", "lite_server.py"):
            with self.subTest(adapter=adapter):
                content = (root / "app" / adapter).read_text(encoding="utf-8")
                self.assertIn("prepare_incremental_audio_stage(", content)
                self.assertIn("publish_audio_generation(generated, staging, temporary_root.name)", content)
                self.assertIn('"source_member_id": filename', content)
                self.assertIn('"source_member_id",', content)
                self.assertNotIn("target = temporary_root / wav.name", content)
                self.assertNotIn("publish_audio_generation(generated, temporary_root,", content)


if __name__ == "__main__":
    unittest.main()
