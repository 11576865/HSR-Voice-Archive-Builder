from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.project import clone_project, create_project


class V09HProjectCloneTests(unittest.TestCase):
    def test_clone_copies_generated_inputs_but_not_outputs_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            voice = root / "voice.zip"
            voice.write_bytes(b"voice")
            source_root = root / "Original"

            with patch("app.project.STATE_FILE", state_file):
                source = create_project(
                    source_root,
                    name="Original",
                    index_csv=".generated/quick_index.csv",
                    wav_source=str(voice),
                    wav_source_fingerprint="abc123",
                    reference_source=str(root / "reference.zip"),
                    reference_source_fingerprint="ref123",
                    reference_text_embedded=True,
                    managed_project_root=True,
                )
                generated = source_root / ".generated"
                generated.mkdir(exist_ok=True)
                (generated / "quick_index.csv").write_text("index\n", encoding="utf-8")
                (generated / "quick_scan.json").write_text("{}", encoding="utf-8")
                (source_root / "output").mkdir()
                (source_root / "output" / "continuous.flac").write_bytes(b"flac")
                (source_root / ".state").mkdir()
                (source_root / ".state" / "translation_usage.json").write_text(
                    "{}", encoding="utf-8"
                )

                clone = clone_project(source, name="Japanese variant")

            clone_root = Path(clone.root)
            self.assertNotEqual(clone_root, source_root)
            self.assertEqual(clone_root.parent, source_root.parent)
            self.assertEqual(clone.name, "Japanese variant")
            self.assertTrue(clone.managed_project_root)
            self.assertEqual(clone.output_dir, "output")
            self.assertEqual(clone.state_dir, ".state")
            self.assertEqual(clone.index_csv, ".generated/quick_index.csv")
            self.assertTrue((clone_root / ".generated" / "quick_index.csv").is_file())
            self.assertTrue((clone_root / ".generated" / "quick_scan.json").is_file())
            self.assertFalse((clone_root / "output").exists())
            self.assertFalse((clone_root / ".state").exists())
            self.assertEqual(Path(clone.wav_source), voice.resolve())
            self.assertEqual(clone.wav_source_fingerprint, "abc123")
            self.assertEqual(clone.reference_source_fingerprint, "ref123")
            self.assertTrue(clone.reference_text_embedded)

    def test_clone_rebinds_manual_relative_inputs_to_original_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            source_root = root / "Manual"
            source_root.mkdir()
            index = source_root / "index.csv"
            voice = source_root / "voice.zip"
            glossary = source_root / "glossary.csv"
            index.write_text("index\n", encoding="utf-8")
            voice.write_bytes(b"voice")
            glossary.write_text("source,target\n", encoding="utf-8")

            with patch("app.project.STATE_FILE", state_file):
                source = create_project(
                    source_root,
                    name="Manual",
                    index_csv="index.csv",
                    wav_source="voice.zip",
                    glossary_path="glossary.csv",
                )
                clone = clone_project(source, name="Manual copy")

            self.assertEqual(Path(clone.index_csv), index.resolve())
            self.assertEqual(Path(clone.wav_source), voice.resolve())
            self.assertEqual(Path(clone.glossary_path), glossary.resolve())
            self.assertFalse((Path(clone.root) / "voice.zip").exists())
            self.assertFalse((Path(clone.root) / "index.csv").exists())

    def test_clone_allocates_unique_sibling_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            source_root = root / "Original"

            with patch("app.project.STATE_FILE", state_file):
                source = create_project(
                    source_root,
                    name="Original",
                    index_csv="index.csv",
                    wav_source=str(root / "voice.zip"),
                )
                (root / "Variant").mkdir()
                clone = clone_project(source, name="Variant")

            self.assertEqual(Path(clone.root).name, "Variant-2")

    def test_clone_refuses_nonempty_explicit_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            source_root = root / "Original"
            destination = root / "Existing"
            destination.mkdir()
            (destination / "keep.txt").write_text("keep", encoding="utf-8")

            with patch("app.project.STATE_FILE", state_file):
                source = create_project(
                    source_root,
                    name="Original",
                    index_csv="index.csv",
                    wav_source=str(root / "voice.zip"),
                )
                with self.assertRaises(FileExistsError):
                    clone_project(source, name="Copy", root=destination)

            self.assertEqual(
                (destination / "keep.txt").read_text(encoding="utf-8"),
                "keep",
            )


if __name__ == "__main__":
    unittest.main()
