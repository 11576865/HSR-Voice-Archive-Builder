from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.quick import (
    _default_project_base,
    _default_project_root,
    _remote_candidate,
    _safe_project_dir_name,
    create_quick_project,
    infer_character,
    quick_scan,
    remote_character_candidates,
    source_inventory,
)
from app.project import create_project, load_project


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
    def test_termux_default_project_base_uses_shared_download_folder(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            downloads = home / "storage" / "downloads"
            downloads.mkdir(parents=True)
            with (
                patch.dict(
                    os.environ,
                    {"TERMUX_VERSION": "0.119", "HSR_VOICE_PROJECTS_DIR": ""},
                    clear=False,
                ),
                patch("app.quick.Path.home", return_value=home),
            ):
                base = _default_project_base()
            self.assertEqual(base, downloads / "HSR_Voice_Test")

    def test_default_project_root_preserves_project_name_and_avoids_collision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "HSR_Voice_Test"
            base.mkdir()
            plan = {"character": {"value": "绯英"}}
            source = Path(td) / "English.zip"
            with patch("app.quick._default_project_base", return_value=base):
                first = _default_project_root(plan, source)
                self.assertEqual(first, base / "绯英")
                first.mkdir()
                second = _default_project_root(plan, source)
            self.assertEqual(second, base / "绯英-2")

    def test_project_directory_name_keeps_unicode_but_removes_unsafe_characters(self) -> None:
        self.assertEqual(_safe_project_dir_name("绯英 / Evanescia: test"), "绯英 - Evanescia- test")

    def test_remote_character_dominance_allows_story_aliases(self) -> None:
        names = {f"chapter1_evanescia_{i}.wav" for i in range(4)}
        records = [
            {
                "filename": name,
                "english": f"Line {i}",
                "hash": "",
                "character": "绯英" if i < 3 else "苏醒的少女",
            }
            for i, name in enumerate(sorted(names))
        ]
        candidate = _remote_candidate(
            records, names, url="https://example.test/EN.xlsx", cache={}
        )
        self.assertEqual(candidate["primary_character"], "绯英")
        self.assertEqual(candidate["primary_character_share"], 0.75)

    def test_legacy_project_prefers_remote_role_from_saved_quick_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            config = create_project(
                root,
                name="legacy",
                index_csv="index.csv",
                wav_source="voice.zip",
                remote_character="evanescia",
            )
            generated = root / ".generated"
            generated.mkdir(parents=True)
            (generated / "quick_scan.json").write_text(
                json.dumps({
                    "index": {
                        "source": "remote",
                        "primary_character": "绯英",
                    }
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            self.assertEqual(
                remote_character_candidates(config, "evanescia"),
                ["绯英", "evanescia"],
            )
            self.assertEqual(
                remote_character_candidates(config, "花火"),
                ["花火", "绯英", "evanescia"],
            )

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
            ), patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                side_effect=OSError("offline test"),
            ):
                plan = quick_scan(archive)

            self.assertTrue(plan["ready"])
            self.assertEqual(plan["index"]["matched_wavs"], 2)
            self.assertEqual(plan["index"]["english_matched"], 2)
            self.assertEqual(len(plan["index"]["file_sha256"]), 64)
            self.assertEqual(plan["character"]["value"], "evanescia")
            self.assertTrue(plan["translation"]["configured"])

    def test_chinese_primary_labs_need_no_second_package_or_translation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "Chinese.zip"
            names = ["archive_evanescia_1.wav", "archive_evanescia_2.wav"]
            make_voice_zip(archive, names)
            records = [
                {"filename": name, "english": f"中文台词 {i}", "hash": "", "character": "绯英"}
                for i, name in enumerate(names, 1)
            ]
            with patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                return_value=(records, {"cache_hit": False, "stale": False}),
            ), patch(
                "app.quick.credentials_status",
                return_value={"provider": "vapi", "base_url": "https://api.gpt.ge/v1", "configured": True},
            ):
                plan = quick_scan(
                    archive,
                    source_text_language="zh-CN",
                    target_language="zh-CN",
                )

            self.assertTrue(plan["ready"])
            self.assertEqual(plan["translation"]["official_chinese_matches"], 2)
            self.assertEqual(plan["translation"]["pending_translation_count"], 0)
            self.assertEqual(plan["translation"]["target_language"], "zh-CN")

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
            ), patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                side_effect=OSError("offline test"),
            ):
                plan = quick_scan(archive)
            self.assertFalse(plan["ready"])
            self.assertTrue(any("1 / 2" in x for x in plan["blockers"]))

    def test_quick_scan_uses_complete_remote_index_when_local_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            names = [
                "chapter5_27_evanescia_101.wav",
                "chapter5_27_evanescia_102.wav",
            ]
            make_voice_zip(archive, names)
            records = [
                {"filename": names[0], "english": "First.", "hash": "", "character": "Evanescia"},
                {"filename": names[1], "english": "Second.", "hash": "", "character": "Evanescia"},
            ]
            with patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                return_value=(records, {"cache_hit": False, "stale": False}),
            ), patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "vapi", "base_url": "https://api.gpt.ge/v1",
                    "configured": False, "source": "test",
                },
            ):
                plan = quick_scan(archive)

            self.assertTrue(plan["ready"])
            self.assertEqual(plan["index"]["source"], "remote")
            self.assertEqual(plan["index"]["matched_wavs"], 2)
            self.assertEqual(plan["english"]["fingerprint"]["algorithm"], "sha256")
            self.assertEqual(len(plan["english"]["fingerprint"]["digest"]), 64)

    def test_quick_scan_blocks_incomplete_remote_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            names = [
                "chapter5_27_evanescia_101.wav",
                "chapter5_27_evanescia_102.wav",
            ]
            make_voice_zip(archive, names)
            records = [
                {"filename": names[0], "english": "First.", "hash": "", "character": "Evanescia"},
            ]
            with patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                return_value=(records, {"cache_hit": True, "stale": False}),
            ), patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "vapi", "base_url": "https://api.gpt.ge/v1",
                    "configured": False, "source": "test",
                },
            ):
                plan = quick_scan(archive)

            self.assertFalse(plan["ready"])
            self.assertEqual(plan["remote_index_attempt"]["matched_wavs"], 1)
            self.assertTrue(any("reliable local or remote index" in x for x in plan["blockers"]))

    def test_quick_create_materializes_remote_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            names = [
                "chapter5_27_evanescia_101.wav",
                "archive_vo_avatar_evanescia_02.wav",
            ]
            make_voice_zip(archive, names)
            records = [
                {"filename": names[1], "english": "Archive.", "hash": "", "character": "绯英"},
                {"filename": names[0], "english": "Story.", "hash": "", "character": "绯英"},
            ]
            with patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                return_value=(records, {"cache_hit": True, "stale": False}),
            ), patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "vapi", "base_url": "https://api.gpt.ge/v1",
                    "configured": False, "source": "test",
                },
            ):
                config, plan = create_quick_project(archive, root=root / "project")

            self.assertEqual(plan["index"]["source"], "remote")
            self.assertEqual(config.remote_character, "绯英")
            generated = Path(config.root) / ".generated" / "quick_index.csv"
            with generated.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([row["filename"] for row in rows], [names[1], names[0]])
            self.assertEqual([row["index"] for row in rows], ["1", "2"])
            self.assertEqual(rows[0]["source"], "AI-Hobbyist EN.xlsx")

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
