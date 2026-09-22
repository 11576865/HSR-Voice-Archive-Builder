from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from app.builder import (
    Entry,
    build_chapter_ordered_flac,
    build_entries,
    extract_major_group,
    major_group_sort_key,
)
from app.pipeline import build_project_v02


def write_wav(path: Path, frames: int = 8000, sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * frames)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class MajorGroupExtractionTests(unittest.TestCase):
    def test_extract_major_group(self) -> None:
        def e(group: str, member_id: str = "test.wav") -> Entry:
            return Entry(
                index=1,
                group=group,
                filename=Path(member_id).name,
                source="",
                source_detail="",
                english="",
                chinese="",
                chinese_source="",
                sample_rate=8000,
                channels=1,
                sample_width_bits=16,
                source_frames=8000,
                source_duration_seconds=1.0,
                start_sample=0,
                audio_end_sample=8000,
                next_start_sample=8000,
                start_seconds=0.0,
                audio_end_seconds=1.0,
                display_end_seconds=1.0,
                sha256="",
                source_member_id=member_id,
            )

        self.assertEqual(extract_major_group(e("chapter0")), "chapter0")
        self.assertEqual(extract_major_group(e("chapter1_1")), "chapter1")
        self.assertEqual(extract_major_group(e("chapter2_15")), "chapter2")
        self.assertEqual(extract_major_group(e("chapter5_13")), "chapter5")
        self.assertEqual(extract_major_group(e("chapterfinality1")), "chapterfinality1")
        self.assertEqual(extract_major_group(e("finality")), "finality")
        self.assertEqual(extract_major_group(e("archive")), "archive")
        self.assertEqual(extract_major_group(e("side0")), "side0")
        self.assertEqual(extract_major_group(e("side1_2")), "side1")
        self.assertEqual(extract_major_group(e("companion1_1")), "companion1")
        self.assertEqual(extract_major_group(e("", "chapter3_1_hero_1.wav")), "chapter3")
        self.assertIsNone(extract_major_group(e("", "unknown_123.wav")))

    def test_major_group_sort_key(self) -> None:
        mgs = ["side1", "chapter2", "chapter0", "archive", "chapter1", "finality", "companion1"]
        sorted_mgs = sorted(mgs, key=major_group_sort_key)
        self.assertEqual(
            sorted_mgs,
            ["archive", "chapter0", "chapter1", "chapter2", "finality", "companion1", "side1"],
        )


class ChapterOrderedFlacTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_build_chapter_ordered_flac_reordering_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            # Duplicate filenames in different subfolders
            write_wav(wavs / "c2" / "dup.wav", 8000)
            write_wav(wavs / "c1" / "dup.wav", 4000)
            write_wav(wavs / "arch" / "arch.wav", 2000)

            labs = root / "labs"
            labs.mkdir()

            index = root / "index.csv"
            bilingual = root / "bilingual.csv"

            # Timeline order: chapter2, then chapter1, then archive
            write_csv(
                index,
                ["序号", "分组", "文件名", "来源", "来源细分", "来源成员路径", "英文文本", "SHA-256"],
                [
                    {"序号": "1", "分组": "chapter2_1", "文件名": "dup.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "c2/dup.wav", "英文文本": "C2 Line", "SHA-256": ""},
                    {"序号": "2", "分组": "chapter1_1", "文件名": "dup.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "c1/dup.wav", "英文文本": "C1 Line", "SHA-256": ""},
                    {"序号": "3", "分组": "archive", "文件名": "arch.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "arch/arch.wav", "英文文本": "Arch Line", "SHA-256": ""},
                ],
            )
            write_csv(
                bilingual,
                ["文件名", "来源成员路径", "中文", "ENGLISH"],
                [
                    {"文件名": "dup.wav", "来源成员路径": "c2/dup.wav", "中文": "", "ENGLISH": "C2 Line"},
                    {"文件名": "dup.wav", "来源成员路径": "c1/dup.wav", "中文": "", "ENGLISH": "C1 Line"},
                    {"文件名": "arch.wav", "来源成员路径": "arch/arch.wav", "中文": "", "ENGLISH": "Arch Line"},
                ],
            )

            entries, report = build_entries(index, bilingual, labs, wavs)
            out_flac = root / "continuous_chapter_ordered.flac"

            res = build_chapter_ordered_flac(
                entries,
                wavs,
                out_flac,
                intro_gap=1.0,
                same_group_gap=0.5,
                group_gap=1.0,
            )

            self.assertTrue(out_flac.is_file())
            self.assertEqual(res["chapter_flac_filename"], "continuous_chapter_ordered.flac")
            self.assertEqual(res["chapter_flac_total_items"], 3)
            self.assertEqual(res["chapter_flac_group_order"], ["archive", "chapter1", "chapter2"])
            self.assertEqual(res["chapter_flac_unassigned_count"], 0)
            self.assertTrue(res["chapter_flac_lossless_pcm_verified"])

            # Verify PCM frame duration calculation:
            # intro_gap = 1.0s (8000 frames)
            # Item 1: archive (arch/arch.wav, 2000 frames)
            # gap: group_gap = 1.0s (8000 frames)
            # Item 2: chapter1 (c1/dup.wav, 4000 frames)
            # gap: group_gap = 1.0s (8000 frames)
            # Item 3: chapter2 (c2/dup.wav, 8000 frames)
            # Total frames = 8000 + 2000 + 8000 + 4000 + 8000 + 8000 = 38000 frames
            # Duration = 38000 / 8000 = 4.75 seconds
            self.assertAlmostEqual(res["chapter_flac_duration_seconds"], 4.75)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_unassigned_items_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            write_wav(wavs / "c1" / "line1.wav", 8000)
            write_wav(wavs / "unk" / "unk.wav", 4000)

            labs = root / "labs"
            labs.mkdir()

            index = root / "index.csv"
            bilingual = root / "bilingual.csv"

            write_csv(
                index,
                ["序号", "分组", "文件名", "来源", "来源细分", "来源成员路径", "英文文本", "SHA-256"],
                [
                    {"序号": "1", "分组": "chapter1_1", "文件名": "line1.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "c1/line1.wav", "英文文本": "C1", "SHA-256": ""},
                    {"序号": "2", "分组": "unknown", "文件名": "unk.wav", "来源": "", "来源细分": "",
                     "来源成员路径": "unk/unk.wav", "英文文本": "Unk", "SHA-256": ""},
                ],
            )
            write_csv(
                bilingual,
                ["文件名", "来源成员路径", "中文", "ENGLISH"],
                [
                    {"文件名": "line1.wav", "来源成员路径": "c1/line1.wav", "中文": "", "ENGLISH": "C1"},
                    {"文件名": "unk.wav", "来源成员路径": "unk/unk.wav", "中文": "", "ENGLISH": "Unk"},
                ],
            )

            entries, report = build_entries(index, bilingual, labs, wavs)
            out_flac = root / "continuous_chapter_ordered.flac"

            res = build_chapter_ordered_flac(
                entries,
                wavs,
                out_flac,
                intro_gap=1.0,
                same_group_gap=0.5,
                group_gap=1.0,
            )

            self.assertEqual(res["chapter_flac_total_items"], 1)
            self.assertEqual(res["chapter_flac_unassigned_count"], 1)
            self.assertEqual(res["chapter_flac_unassigned_items"], ["unk/unk.wav"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_pipeline_build_with_make_chapter_flac(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            write_wav(wavs / "chapter1_1_mar7th_205.wav", 8000)
            write_wav(wavs / "chapter2_1_mar7th_206.wav", 4000)

            labs = root / "labs"
            labs.mkdir()

            index = root / "index.csv"
            bilingual = root / "bilingual.csv"

            write_csv(
                index,
                ["序号", "分组", "文件名", "来源", "来源细分", "英文文本", "SHA-256"],
                [
                    {"序号": "1", "分组": "chapter1_1", "文件名": "chapter1_1_mar7th_205.wav", "来源": "", "来源细分": "", "英文文本": "C1", "SHA-256": ""},
                    {"序号": "2", "分组": "chapter2_1", "文件名": "chapter2_1_mar7th_206.wav", "来源": "", "来源细分": "", "英文文本": "C2", "SHA-256": ""},
                ],
            )
            write_csv(
                bilingual,
                ["文件名", "中文", "ENGLISH"],
                [
                    {"文件名": "chapter1_1_mar7th_205.wav", "中文": "", "ENGLISH": "C1"},
                    {"文件名": "chapter2_1_mar7th_206.wav", "中文": "", "ENGLISH": "C2"},
                ],
            )

            out_dir = root / "output"
            report = build_project_v02(
                index,
                wavs,
                out_dir,
                bilingual_csv=bilingual,
                chs_source=labs,
                make_flac=True,
                make_chapter_flac=True,
            )

            self.assertTrue((out_dir / "continuous.flac").is_file())
            self.assertTrue((out_dir / "continuous_chapter_ordered.flac").is_file())
            self.assertIn("chapter_flac_path", report)
            self.assertEqual(report["chapter_flac_filename"], "continuous_chapter_ordered.flac")
            self.assertEqual(report["chapter_flac_group_order"], ["chapter1", "chapter2"])


if __name__ == "__main__":
    unittest.main()
