from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.builder import (
    build_entries,
    collect_member_labs,
    collect_wav_members,
    resolve_member_wav,
)
from app.quick import create_quick_project, quick_scan, source_inventory
from app.schema import normalize_index
from app.subtitles import _apply_derived_subtitle_fields, _override_for_entry


CREDENTIALS_OK = {
    "provider": "vapi",
    "base_url": "https://api.gpt.ge/v1",
    "configured": False,
    "source": "test",
}


def make_zip(path: Path, members: dict[str, bytes | str]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, payload in members.items():
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            z.writestr(name, data)


def write_wav(path: Path, frames: int, sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * frames)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class InventoryMemberTests(unittest.TestCase):
    def test_zip_inventory_keeps_member_paths_and_duplicate_groups(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "English.zip"
            make_zip(archive, {
                "one/dup.wav": b"a",
                "one/dup.lab": "Line A",
                "two/dup.wav": b"b",
                "two/dup.lab": "Line B",
                "three/solo.wav": b"c",
                "three/solo.lab": "Line C",
            })
            inv = source_inventory(archive)
            self.assertEqual(inv["wav_count"], 3)
            self.assertEqual(
                sorted(inv["wav_members"]),
                ["one/dup.wav", "three/solo.wav", "two/dup.wav"],
            )
            self.assertEqual(inv["duplicate_wav_names"], ["dup.wav"])
            self.assertEqual(
                inv["duplicate_wav_groups"],
                {"dup.wav": ["one/dup.wav", "two/dup.wav"]},
            )
            # Member-level pairing sees each sibling LAB next to its WAV.
            self.assertEqual(inv["wav_lab_member_pairs"], 3)

    def test_directory_inventory_member_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "pkg"
            (root / "a").mkdir(parents=True)
            (root / "b").mkdir(parents=True)
            (root / "a" / "x.wav").write_bytes(b"a")
            (root / "a" / "x.lab").write_text("A", encoding="utf-8")
            (root / "b" / "x.wav").write_bytes(b"b")
            (root / "b" / "x.lab").write_text("B", encoding="utf-8")
            inv = source_inventory(root)
            self.assertEqual(sorted(inv["wav_members"]), ["a/x.wav", "b/x.wav"])
            self.assertEqual(inv["duplicate_wav_groups"], {"x.wav": ["a/x.wav", "b/x.wav"]})
            self.assertEqual(inv["wav_lab_member_pairs"], 2)


class PackageLabIndexTests(unittest.TestCase):
    """Layer 3: complete same-stem LAB coverage makes any index optional."""

    def test_en_package_with_complete_labs_builds_without_any_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            make_zip(archive, {
                # Natural member ordering must place chapter2 before chapter10.
                "chapter10/trailblazer_2.wav": b"wav-2",
                "chapter10/trailblazer_2.lab": "Second chapter line.",
                "chapter2/trailblazer_1.wav": b"wav-1",
                "chapter2/trailblazer_1.lab": "First chapter line.",
                # A same-basename group is fully resolved by member identity.
                "chapter2/female/dup.wav": b"dup-f",
                "chapter2/female/dup.lab": "Female line.",
                "chapter2/male/dup.wav": b"dup-m",
                "chapter2/male/dup.lab": "Male line.",
            })
            with patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                return_value=([], {"cache_hit": False, "stale": False}),
            ), patch("app.quick.credentials_status", return_value=CREDENTIALS_OK):
                plan = quick_scan(archive)
                self.assertTrue(plan["ready"], plan["blockers"])
                self.assertEqual(plan["index"]["source"], "primary-package-lab")
                self.assertEqual(plan["index"]["matched_wavs"], 4)
                self.assertEqual(plan["duplicates"]["groups"], 1)
                self.assertEqual(plan["duplicates"]["auto_resolved"], 2)
                self.assertEqual(plan["duplicates"]["pending"], 0)

                config, plan = create_quick_project(archive, root=root / "project")

            rows = read_csv(Path(config.root) / ".generated" / "quick_index.csv")
            self.assertEqual(len(rows), 4)
            member_ids = [row["source_member_id"] for row in rows]
            self.assertEqual(
                member_ids,
                [
                    "chapter2/female/dup.wav",
                    "chapter2/male/dup.wav",
                    "chapter2/trailblazer_1.wav",
                    "chapter10/trailblazer_2.wav",
                ],
            )
            by_member = {row["source_member_id"]: row for row in rows}
            self.assertEqual(by_member["chapter2/female/dup.wav"]["english"], "Female line.")
            self.assertEqual(by_member["chapter2/male/dup.wav"]["english"], "Male line.")
            self.assertEqual(
                by_member["chapter10/trailblazer_2.wav"]["english"],
                "Second chapter line.",
            )
            self.assertTrue(all(row["source"] == "primary-package-lab" for row in rows))
            indexes = [row["index"] for row in rows]
            self.assertEqual(len(set(indexes)), len(indexes))

    def test_non_en_package_with_complete_labs_also_bypasses_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "Japanese.zip"
            make_zip(archive, {
                "vo/vo_1.wav": b"1",
                "vo/vo_1.lab": "日本語のセリフ",
            })
            with patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                return_value=([], {"cache_hit": False, "stale": False}),
            ), patch("app.quick.credentials_status", return_value=CREDENTIALS_OK):
                plan = quick_scan(archive, source_text_language="ja")
            self.assertTrue(plan["ready"], plan["blockers"])
            self.assertEqual(plan["index"]["source"], "primary-package-lab")


class DuplicateDisambiguationTests(unittest.TestCase):
    def _scan_and_create(self, root: Path, records: list[dict[str, str]]):
        archive = root / "English.zip"
        with patch(
            "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
            return_value=(records, {"cache_hit": False, "stale": False}),
        ), patch("app.quick.credentials_status", return_value=CREDENTIALS_OK):
            plan = quick_scan(archive)
            self.assertTrue(plan["ready"], plan["blockers"])
            config, _ = create_quick_project(archive, root=root / "project")
        rows = read_csv(Path(config.root) / ".generated" / "quick_index.csv")
        return plan, rows

    def test_index_hash_binds_row_to_matching_member(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            digest = hashlib.sha256(b"dup-f").hexdigest()
            make_zip(root / "English.zip", {
                "female/dup.wav": b"dup-f",
                "male/dup.wav": b"dup-m",
            })
            records = [
                {
                    "filename": "dup.wav",
                    "english": "Indexed line.",
                    "hash": digest,
                    "character": "开拓者",
                }
            ]
            plan, rows = self._scan_and_create(root, records)
            self.assertEqual(plan["duplicates"]["auto_resolved"], 1)
            self.assertEqual(plan["duplicates"]["pending"], 1)
            self.assertTrue(any("待确认 1 条" in w for w in plan["warnings"]))
            self.assertEqual(len(rows), 2)
            by_member = {row["source_member_id"]: row for row in rows}
            self.assertEqual(by_member["female/dup.wav"]["english"], "Indexed line.")
            self.assertEqual(by_member["female/dup.wav"]["sha256"], digest)
            loser = by_member["male/dup.wav"]
            self.assertEqual(loser["source"], "unindexed-package-file")
            self.assertEqual(loser["english"], "")
            indexes = [row["index"] for row in rows]
            self.assertEqual(len(set(indexes)), 2)

    def test_index_lab_text_binds_row_to_matching_member(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_zip(root / "English.zip", {
                "female/dup.wav": b"dup-f",
                "female/dup.lab": "Female line.",
                "male/dup.wav": b"dup-m",
                "male/dup.lab": "Male line.",
            })
            records = [
                {
                    "filename": "dup.wav",
                    "english": "Male line.",
                    "hash": "",
                    "character": "开拓者",
                }
            ]
            plan, rows = self._scan_and_create(root, records)
            self.assertEqual(plan["duplicates"]["auto_resolved"], 1)
            self.assertEqual(plan["duplicates"]["pending"], 1)
            by_member = {row["source_member_id"]: row for row in rows}
            self.assertEqual(by_member["male/dup.wav"]["english"], "Male line.")
            self.assertEqual(by_member["male/dup.wav"]["source"], "AI-Hobbyist EN.xlsx")
            self.assertEqual(
                by_member["female/dup.wav"]["source"], "unindexed-package-file"
            )

    def test_unresolvable_group_is_demoted_to_appendix_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_zip(root / "English.zip", {
                "female/dup.wav": b"dup-f",
                "female/dup.lab": "Female line.",
                "male/dup.wav": b"dup-m",
                "male/dup.lab": "Male line.",
                "solo.wav": b"solo",
                "solo.lab": "Solo line.",
            })
            records = [
                {"filename": "dup.wav", "english": "Nobody said this.", "hash": "", "character": "开拓者"},
                {"filename": "solo.wav", "english": "Solo line.", "hash": "", "character": "开拓者"},
            ]
            plan, rows = self._scan_and_create(root, records)
            self.assertEqual(plan["duplicates"]["groups"], 1)
            self.assertEqual(plan["duplicates"]["auto_resolved"], 0)
            self.assertEqual(plan["duplicates"]["pending"], 2)
            self.assertTrue(any("待确认 2 条" in w for w in plan["warnings"]))
            self.assertEqual(len(rows), 3)
            dup_rows = [row for row in rows if row["filename"] == "dup.wav"]
            self.assertEqual(len(dup_rows), 2)
            self.assertTrue(
                all(row["source"] == "unindexed-package-file" for row in dup_rows)
            )
            self.assertEqual(
                sorted(row["source_member_id"] for row in dup_rows),
                ["female/dup.wav", "male/dup.wav"],
            )
            solo = [row for row in rows if row["filename"] == "solo.wav"][0]
            self.assertEqual(solo["english"], "Solo line.")
            self.assertEqual(solo["source_member_id"], "solo.wav")


class SchemaMemberIdTests(unittest.TestCase):
    def test_duplicate_filenames_allowed_with_distinct_member_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            index = Path(td) / "index.csv"
            write_csv(
                index,
                ["index", "group", "filename", "english", "source_member_id"],
                [
                    {"index": "1", "group": "g", "filename": "dup.wav", "english": "A", "source_member_id": "a/dup.wav"},
                    {"index": "2", "group": "g", "filename": "dup.wav", "english": "B", "source_member_id": "b/dup.wav"},
                ],
            )
            rows = normalize_index(index)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["source_member_id"], "a/dup.wav")
            self.assertEqual(rows[1]["source_member_id"], "b/dup.wav")

    def test_duplicate_member_id_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            index = Path(td) / "index.csv"
            write_csv(
                index,
                ["index", "group", "filename", "english", "source_member_id"],
                [
                    {"index": "1", "group": "g", "filename": "dup.wav", "english": "A", "source_member_id": "a/dup.wav"},
                    {"index": "2", "group": "g", "filename": "dup.wav", "english": "B", "source_member_id": "a/dup.wav"},
                ],
            )
            with self.assertRaises(ValueError):
                normalize_index(index)

    def test_duplicate_basename_without_member_id_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            index = Path(td) / "index.csv"
            write_csv(
                index,
                ["index", "group", "filename", "english"],
                [
                    {"index": "1", "group": "g", "filename": "dup.wav", "english": "A"},
                    {"index": "2", "group": "g", "filename": "dup.wav", "english": "B"},
                ],
            )
            with self.assertRaises(ValueError):
                normalize_index(index)


class BuilderMemberResolutionTests(unittest.TestCase):
    def test_missing_index_wav_is_skipped_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            write_wav(wavs / "present.wav", 800)
            labs = root / "labs"
            labs.mkdir()
            index = root / "index.csv"
            bilingual = root / "bilingual.csv"
            write_csv(
                index,
                ["序号", "分组", "文件名", "来源", "来源细分", "来源成员路径", "英文文本", "SHA-256"],
                [
                    {"序号": "1", "分组": "g", "文件名": "present.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "present.wav", "英文文本": "Present.", "SHA-256": ""},
                    {"序号": "2", "分组": "带变量语音 - Placeholder", "文件名": "missing.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "带变量语音 - Placeholder/missing.wav", "英文文本": "Missing.", "SHA-256": ""},
                ],
            )
            write_csv(
                bilingual,
                ["文件名", "来源成员路径", "中文", "ENGLISH"],
                [
                    {"文件名": "present.wav", "来源成员路径": "present.wav", "中文": "", "ENGLISH": "Present."},
                    {"文件名": "missing.wav", "来源成员路径": "带变量语音 - Placeholder/missing.wav", "中文": "", "ENGLISH": "Missing."},
                ],
            )

            entries, report = build_entries(index, bilingual, labs, wavs)

            self.assertEqual([entry.filename for entry in entries], ["present.wav"])
            self.assertEqual(report["count_index_rows"], 2)
            self.assertEqual(report["count_total"], 1)
            self.assertEqual(report["count_unavailable_wavs"], 1)
            self.assertFalse(report["all_index_wavs_available"])
            self.assertEqual(
                report["unavailable_wavs"],
                [{
                    "filename": "missing.wav",
                    "source_member_id": "带变量语音 - Placeholder/missing.wav",
                    "group": "带变量语音 - Placeholder",
                    "reason": "wav_not_present",
                }],
            )

    def test_all_missing_index_wavs_still_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            wavs.mkdir()
            labs = root / "labs"
            labs.mkdir()
            index = root / "index.csv"
            bilingual = root / "bilingual.csv"
            write_csv(
                index,
                ["序号", "分组", "文件名", "来源", "来源细分", "来源成员路径", "英文文本", "SHA-256"],
                [
                    {"序号": "1", "分组": "g", "文件名": "missing.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "g/missing.wav", "英文文本": "Missing.", "SHA-256": ""},
                ],
            )
            write_csv(
                bilingual,
                ["文件名", "来源成员路径", "中文", "ENGLISH"],
                [
                    {"文件名": "missing.wav", "来源成员路径": "g/missing.wav", "中文": "", "ENGLISH": "Missing."},
                ],
            )

            with self.assertRaisesRegex(ValueError, "No usable WAV entries"):
                build_entries(index, bilingual, labs, wavs)

    def test_build_entries_binds_rows_to_their_own_members(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            write_wav(wavs / "female" / "dup.wav", 8000)
            write_wav(wavs / "male" / "dup.wav", 4000)
            labs = root / "labs"
            labs.mkdir()
            index = root / "index.csv"
            bilingual = root / "bilingual.csv"
            write_csv(
                index,
                ["序号", "分组", "文件名", "来源", "来源细分", "来源成员路径", "英文文本", "SHA-256"],
                [
                    {"序号": "1", "分组": "g", "文件名": "dup.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "female/dup.wav", "英文文本": "Female.", "SHA-256": ""},
                    {"序号": "2", "分组": "g", "文件名": "dup.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "male/dup.wav", "英文文本": "Male.", "SHA-256": ""},
                ],
            )
            write_csv(
                bilingual,
                ["文件名", "来源成员路径", "中文", "ENGLISH"],
                [
                    {"文件名": "dup.wav", "来源成员路径": "female/dup.wav", "中文": "", "ENGLISH": "Female."},
                    {"文件名": "dup.wav", "来源成员路径": "male/dup.wav", "中文": "", "ENGLISH": "Male."},
                ],
            )
            entries, report = build_entries(index, bilingual, labs, wavs)
            self.assertEqual(len(entries), 2)
            by_member = {e.source_member_id: e for e in entries}
            self.assertEqual(by_member["female/dup.wav"].source_frames, 8000)
            self.assertEqual(by_member["male/dup.wav"].source_frames, 4000)
            self.assertEqual(by_member["female/dup.wav"].english, "Female.")
            self.assertEqual(by_member["male/dup.wav"].english, "Male.")

    def test_ambiguous_basename_without_member_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_wav(root / "a" / "dup.wav", 800)
            write_wav(root / "b" / "dup.wav", 800)
            members = collect_wav_members(root)
            with self.assertRaises(ValueError):
                resolve_member_wav(members, "dup.wav")
            path, resolved = resolve_member_wav(members, "dup.wav", "a/dup.wav")
            self.assertIsNotNone(path)
            self.assertEqual(resolved, "a/dup.wav")

    def test_collect_member_labs_prefers_sibling_and_keeps_unique_stems(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir(parents=True)
            (root / "b").mkdir(parents=True)
            (root / "a" / "x.lab").write_text("A text", encoding="utf-8")
            (root / "b" / "x.lab").write_text("B text", encoding="utf-8")
            (root / "b" / "solo.lab").write_text("Solo", encoding="utf-8")
            by_member, by_stem = collect_member_labs(root)
            self.assertEqual(by_member["a/x.lab"], "A text")
            self.assertEqual(by_member["b/x.lab"], "B text")
            # Conflicting same-stem LABs are excluded from the stem fallback.
            self.assertNotIn("x", by_stem)
            self.assertEqual(by_stem["solo"], "Solo")


class SubtitleOverrideMemberTests(unittest.TestCase):
    def _entries(self) -> list[dict[str, object]]:
        return [
            {
                "index": 1,
                "filename": "dup.wav",
                "source_member_id": "female/dup.wav",
                "target_text": "女",
            },
            {
                "index": 2,
                "filename": "dup.wav",
                "source_member_id": "male/dup.wav",
                "target_text": "男",
            },
        ]

    def test_member_aware_override_applies_to_the_right_entry(self) -> None:
        entries = self._entries()
        overrides = {
            "2": {
                "final_chs": "男-改",
                "modified": True,
                "filename": "dup.wav",
                "source_member_id": "male/dup.wav",
            }
        }
        _apply_derived_subtitle_fields(entries, overrides)
        self.assertEqual(entries[0]["final_chs"], "女")
        self.assertFalse(entries[0]["modified"])
        self.assertEqual(entries[1]["final_chs"], "男-改")
        self.assertTrue(entries[1]["modified"])

    def test_bare_filename_fallback_is_suppressed_for_ambiguous_names(self) -> None:
        entries = self._entries()
        # A legacy override without member id must not guess between
        # same-basename entries.
        overrides = {
            "99": {"final_chs": "旧改动", "modified": True, "filename": "dup.wav"}
        }
        _apply_derived_subtitle_fields(entries, overrides)
        self.assertEqual(entries[0]["final_chs"], "女")
        self.assertEqual(entries[1]["final_chs"], "男")

    def test_bare_filename_fallback_still_works_for_unique_names(self) -> None:
        entry = {"index": 7, "filename": "solo.wav", "target_text": "原文"}
        override = _override_for_entry(
            entry,
            {"99": {"final_chs": "旧改动", "modified": True, "filename": "solo.wav"}},
            frozenset(),
        )
        self.assertIsNotNone(override)
        self.assertEqual(override["final_chs"], "旧改动")


class EndToEndDuplicateBuildTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_pipeline_builds_archive_with_duplicate_basenames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staging = root / "staging"
            write_wav(staging / "female" / "dup.wav", 8000)
            write_wav(staging / "male" / "dup.wav", 4000)
            write_wav(staging / "solo.wav", 2000)
            (staging / "female" / "dup.lab").write_text("Female line.", encoding="utf-8")
            (staging / "male" / "dup.lab").write_text("Male line.", encoding="utf-8")
            (staging / "solo.lab").write_text("Solo line.", encoding="utf-8")
            archive = root / "English.zip"
            with zipfile.ZipFile(archive, "w") as z:
                for path in sorted(staging.rglob("*")):
                    if path.is_file():
                        z.write(path, path.relative_to(staging).as_posix())

            project = root / "project"
            with patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                return_value=([], {"cache_hit": False, "stale": False}),
            ), patch("app.quick.credentials_status", return_value=CREDENTIALS_OK):
                config, plan = create_quick_project(archive, root=project)
            self.assertTrue(plan["ready"], plan["blockers"])

            from app.pipeline import build_project_v02
            from app.project import resolve_project_path

            out_dir = project / "output"
            report = build_project_v02(
                resolve_project_path(config, config.index_csv),
                archive,
                out_dir,
                make_flac=True,
                translate_missing=False,
            )
            manifest = json.loads(
                (out_dir / "manifest.json").read_text(encoding="utf-8")
            )
            entries = manifest["entries"]
            self.assertEqual(len(entries), 3)
            by_member = {e["source_member_id"]: e for e in entries}
            self.assertEqual(by_member["female/dup.wav"]["source_frames"], 8000)
            self.assertEqual(by_member["male/dup.wav"]["source_frames"], 4000)
            self.assertEqual(by_member["solo.wav"]["source_frames"], 2000)
            self.assertEqual(by_member["female/dup.wav"]["english"], "Female line.")
            self.assertEqual(by_member["male/dup.wav"]["english"], "Male line.")
            self.assertTrue((out_dir / "continuous.flac").is_file())
            self.assertTrue(report["lossless_pcm_verified"])
            self.assertEqual(report["count_total"], 3)
            indexes = [e["index"] for e in entries]
            self.assertEqual(len(set(indexes)), 3)


if __name__ == "__main__":
    unittest.main()
