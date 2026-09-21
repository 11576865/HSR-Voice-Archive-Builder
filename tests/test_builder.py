from __future__ import annotations

import csv
import tempfile
import unittest
import wave
from pathlib import Path

from app.builder import build_entries, cross_language_voice_key, map_labs_to_voice_filenames, write_manifest


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


class BuilderTests(unittest.TestCase):
    def test_cross_language_voice_key_patterns(self) -> None:
        test_cases = [
            ("chapter5_13_evanescia_103.wav", "chapter5_13::103"),
            ("chapter5_13_绯英_103.lab", "chapter5_13::103"),
            ("vo_chapter5_13_evanescia_104.wav", "chapter5_13::104"),
            ("vo_chapter5_13_绯英_104.lab", "chapter5_13::104"),
            ("vo_100101_01.wav", "id::100101_01"),
            ("100101_01.lab", "id::100101_01"),
            ("archive_evanescia_01.wav", "archive::01"),
            ("archive_绯英_01.lab", "archive::01"),
        ]
        for name, expected in test_cases:
            self.assertEqual(cross_language_voice_key(name), expected)

    def test_map_labs_to_voice_filenames_classification(self) -> None:
        wavs = [
            "archive_evanescia_01.wav",
            "chapter5_13_evanescia_103.wav",
            "vo_chapter5_13_evanescia_104.wav",
            "vo_100101_01.wav",
            "unmatched_voice_999.wav",
        ]
        labs = {
            "archive_evanescia_01": "Exact Match",
            "chapter5_13_绯英_103": "Cross Language Match 103",
            "vo_chapter5_13_绯英_104": "Cross Language Match 104",
            "100101_01": "Numeric ID Match",
            "unrelated_lab_888": "Unused LAB",
        }
        mapped, stats = map_labs_to_voice_filenames(wavs, labs)
        self.assertEqual(stats["exact"], 1)
        self.assertEqual(stats["structural"], 3)
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["unmatched"], 1)
        self.assertEqual(stats["conflicts"], 0)
        self.assertEqual(mapped["archive_evanescia_01.wav"], "Exact Match")
        self.assertEqual(mapped["chapter5_13_evanescia_103.wav"], "Cross Language Match 103")
        self.assertEqual(mapped["vo_chapter5_13_evanescia_104.wav"], "Cross Language Match 104")
        self.assertEqual(mapped["vo_100101_01.wav"], "Numeric ID Match")

    def test_cross_language_official_labs_are_mapped_by_unique_voice_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            labs = root / "official_chinese"
            labs.mkdir()
            english_name = "chapter5_13_evanescia_103.wav"
            write_wav(wavs / english_name, 8000)
            (labs / "chapter5_13_绯英_103.lab").write_text("官方中文", encoding="utf-8")
            index = root / "index.csv"
            bilingual = root / "bilingual.csv"
            write_csv(index, ["序号", "分组", "文件名", "来源", "来源细分", "英文文本", "SHA-256"], [{"序号": "1", "分组": "g", "文件名": english_name, "来源": "", "来源细分": "", "英文文本": "English", "SHA-256": ""}])
            write_csv(bilingual, ["文件名", "中文", "ENGLISH"], [{"文件名": english_name, "中文": "", "ENGLISH": "English"}])

            entries, report = build_entries(index, bilingual, labs, wavs)
            self.assertEqual(entries[0].chinese, "官方中文")
            self.assertEqual(entries[0].chinese_source, "official_chs_lab")
            self.assertEqual(report["count_official_chs_exact"], 0)
            self.assertEqual(report["count_official_chs_structural"], 1)

    def test_chinese_primary_labs_are_used_as_chinese_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            labs = root / "official_chinese"
            labs.mkdir()
            write_wav(wavs / "a.wav", 8000)
            (wavs / "a.lab").write_text("主音频包中文台词", encoding="utf-8")

            index = root / "index.csv"
            bilingual = root / "bilingual.csv"
            write_csv(
                index,
                ["序号", "分组", "文件名", "来源", "来源细分", "英文文本", "SHA-256"],
                [{"序号": "1", "分组": "g1", "文件名": "a.wav", "来源": "", "来源细分": "", "英文文本": "主音频包中文台词", "SHA-256": ""}],
            )
            write_csv(
                bilingual,
                ["文件名", "中文", "ENGLISH"],
                [{"文件名": "a.wav", "中文": "", "ENGLISH": "主音频包中文台词"}],
            )

            entries, report = build_entries(
                index,
                bilingual,
                labs,
                wavs,
                source_text_language="zh-CN",
                target_language="zh-CN",
            )
            self.assertEqual(entries[0].chinese, "主音频包中文台词")
            self.assertEqual(entries[0].chinese_source, "official_chs_lab")
            self.assertEqual(report["count_official_chs_lab"], 1)
            self.assertEqual(report["count_missing_chinese"], 0)

    def test_official_lab_precedence_and_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            labs = root / "labs"
            out = root / "out"
            labs.mkdir()

            write_wav(wavs / "a.wav", 8000)
            write_wav(wavs / "b.wav", 4000)
            (labs / "a.lab").write_text("官方中文 A", encoding="utf-8")

            index = root / "index.csv"
            bilingual = root / "bilingual.csv"
            write_csv(
                index,
                ["序号", "分组", "文件名", "来源", "来源细分", "英文文本", "SHA-256"],
                [
                    {"序号": "1", "分组": "g1", "文件名": "a.wav", "来源": "", "来源细分": "", "英文文本": "English A", "SHA-256": ""},
                    {"序号": "2", "分组": "g2", "文件名": "b.wav", "来源": "", "来源细分": "", "英文文本": "English B", "SHA-256": ""},
                ],
            )
            write_csv(
                bilingual,
                ["文件名", "中文", "ENGLISH"],
                [
                    {"文件名": "a.wav", "中文": "旧译 A", "ENGLISH": "English A"},
                    {"文件名": "b.wav", "中文": "译文 B", "ENGLISH": "English B"},
                ],
            )

            entries, report = build_entries(index, bilingual, labs, wavs, same_group_gap=0.4, group_gap=1.2)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].chinese, "官方中文 A")
            self.assertEqual(entries[0].chinese_source, "official_chs_lab")
            self.assertEqual(entries[1].chinese, "译文 B")
            self.assertEqual(entries[1].chinese_source, "translated_existing")
            self.assertEqual(entries[0].start_sample, round(5.0 * 8000))
            self.assertEqual(
                entries[1].start_sample,
                round(5.0 * 8000) + 8000 + round(1.2 * 8000),
            )
            self.assertEqual(report["count_missing_chinese"], 0)

            write_manifest(entries, report, out)
            self.assertTrue((out / "manifest.json").is_file())
            self.assertTrue((out / "timeline_resolved.json").is_file())
            self.assertTrue((out / "HSR_Voice_Archive.ass").is_file())
            self.assertTrue((out / "HSR_Voice_Archive.srt").is_file())
            self.assertFalse((out / "bilingual.srt").exists())


if __name__ == "__main__":
    unittest.main()
