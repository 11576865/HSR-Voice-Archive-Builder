from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.quick import create_quick_project, infer_character, quick_scan, source_inventory
from app.project import load_project


def write_index(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["index", "group", "filename", "english", "sha256"],
        )
        writer.writeheader()
        writer.writerows(rows)


def make_voice_zip(path: Path, names: list[str], with_labs: bool = True) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, b"not-real-wav")
            if with_labs:
                z.writestr(Path(name).with_suffix(".lab").name, "English text")


class QuickModeTests(unittest.TestCase):
    def test_zip_inventory_is_read_only_and_counts_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            make_voice_zip(
                archive,
                [
                    "chapter5_27_evanescia_101.wav",
                    "chapter5_27_evanescia_102.wav",
                ],
            )
            before = archive.read_bytes()
            inv = source_inventory(archive)
            after = archive.read_bytes()

            self.assertEqual(inv["wav_count"], 2)
            self.assertEqual(inv["lab_count"], 2)
            self.assertEqual(inv["wav_lab_pairs"], 2)
            self.assertEqual(inv["duplicate_wav_names"], [])
            self.assertEqual(before, after)

    def test_character_inference_finds_repeated_name(self) -> None:
        result = infer_character(
            [
                "chapter5_27_evanescia_101.wav",
                "chapter5_28_evanescia_102.wav",
                "archive_vo_avatar_evanescia_03.wav",
            ]
        )
        self.assertEqual(result["value"], "evanescia")
        self.assertEqual(result["confidence"], "high")

    def test_quick_scan_auto_matches_covering_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            names = [
                "chapter5_27_evanescia_101.wav",
                "chapter5_27_evanescia_102.wav",
            ]
            make_voice_zip(archive, names)
            write_index(
                root / "绯英_379条_完整索引.csv",
                [
                    {"index": "1", "group": "chapter5_27", "filename": names[0], "english": "First.", "sha256": ""},
                    {"index": "2", "group": "chapter5_27", "filename": names[1], "english": "Second.", "sha256": ""},
                    {"index": "3", "group": "archive", "filename": "other.wav", "english": "Other.", "sha256": ""},
                ],
            )
            with patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "vapi",
                    "base_url": "https://api.gpt.ge/v1",
                    "configured": True,
                    "source": "test",
                },
            ):
                plan = quick_scan(archive)

            self.assertTrue(plan["ready"])
            self.assertEqual(plan["index"]["matched_wavs"], 2)
            self.assertEqual(plan["index"]["english_matched"], 2)
            self.assertEqual(plan["character"]["value"], "evanescia")
            self.assertTrue(plan["translation"]["configured"])

    def test_quick_scan_blocks_partial_index_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            names = ["a_evanescia_1.wav", "a_evanescia_2.wav"]
            make_voice_zip(archive, names)
            write_index(
                root / "index.csv",
                [{"index": "1", "group": "a", "filename": names[0], "english": "First.", "sha256": ""}],
            )
            with patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "vapi",
                    "base_url": "https://api.gpt.ge/v1",
                    "configured": False,
                    "source": "test",
                },
            ):
                plan = quick_scan(archive)
            self.assertFalse(plan["ready"])
            self.assertTrue(any("1 / 2" in x for x in plan["blockers"]))

    def test_quick_scan_blocks_duplicate_wav_basename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("one/dup.wav", b"a")
                z.writestr("two/dup.wav", b"b")
            with patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "vapi",
                    "base_url": "https://api.gpt.ge/v1",
                    "configured": False,
                    "source": "test",
                },
            ):
                plan = quick_scan(archive)
            self.assertFalse(plan["ready"])
            self.assertTrue(any("Duplicate WAV basenames" in x for x in plan["blockers"]))

    def test_quick_create_generates_filtered_internal_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            names = [
                "chapter5_27_evanescia_101.wav",
                "chapter5_27_evanescia_102.wav",
            ]
            make_voice_zip(archive, names)
            write_index(
                root / "绯英_379条_完整索引.csv",
                [
                    {"index": "10", "group": "chapter5_27", "filename": names[0], "english": "First.", "sha256": ""},
                    {"index": "11", "group": "chapter5_27", "filename": names[1], "english": "Second.", "sha256": ""},
                    {"index": "12", "group": "archive", "filename": "not-in-package.wav", "english": "Skip.", "sha256": ""},
                ],
            )
            project_root = root / "project"
            with patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "vapi",
                    "base_url": "https://api.gpt.ge/v1",
                    "configured": True,
                    "source": "test",
                },
            ):
                config, plan = create_quick_project(archive, root=project_root)

            self.assertTrue(plan["ready"])
            loaded = load_project(project_root)
            self.assertTrue(loaded.translate_missing)
            self.assertEqual(loaded.translation_model, "gpt-5.6-sol")
            generated = project_root / ".generated" / "quick_index.csv"
            self.assertTrue(generated.is_file())
            text = generated.read_text(encoding="utf-8-sig")
            self.assertIn(names[0], text)
            self.assertIn(names[1], text)
            self.assertNotIn("not-in-package.wav", text)
            scan = json.loads((project_root / ".generated" / "quick_scan.json").read_text(encoding="utf-8"))
            self.assertTrue(scan["ready"])


if __name__ == "__main__":
    unittest.main()
