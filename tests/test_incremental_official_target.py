from __future__ import annotations

import csv
import tempfile
import unittest
import wave
from pathlib import Path

from app.builder import build_entries
from app.schema import write_legacy_inputs


def write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 80)


def write_index(path: Path, row: dict[str, str], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)


class IncrementalOfficialTargetTests(unittest.TestCase):
    def test_confirmed_incremental_chinese_is_target_text_not_api_reference(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            write_wav(wavs / "chapter5_1_evanescia_101.wav")
            index = root / "index.csv"
            write_index(
                index,
                {
                    "index": "1",
                    "group": "chapter5_1",
                    "filename": "chapter5_1_evanescia_101.wav",
                    "source": "huggingface",
                    "source_detail": "simon3000/starrail-voice",
                    "english": "Official English line.",
                    "reference_text": "官方中文台词。",
                    "reference_language": "zh-CN",
                    "official_target_text": "官方中文台词。",
                    "official_target_language": "zh-CN",
                    "official_target_source": "huggingface:Chinese(PRC):same_ingame_filename",
                    "sha256": "",
                },
                [
                    "index", "group", "filename", "source", "source_detail", "english",
                    "reference_text", "reference_language",
                    "official_target_text", "official_target_language", "official_target_source",
                    "sha256",
                ],
            )

            legacy_index, legacy_bilingual = write_legacy_inputs(index, None, root / "legacy")
            empty_labs = root / "labs"
            empty_labs.mkdir()
            entries, report = build_entries(
                legacy_index,
                legacy_bilingual,
                empty_labs,
                wavs,
                target_language="zh-CN",
            )

            self.assertEqual(entries[0].chinese, "官方中文台词。")
            self.assertEqual(
                entries[0].chinese_source,
                "huggingface:Chinese(PRC):same_ingame_filename",
            )
            self.assertEqual(report["count_incremental_official_reference"], 1)
            self.assertEqual(report["count_missing_chinese"], 0)

    def test_legacy_reference_only_incremental_index_is_migrated_without_redownload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            write_wav(wavs / "chapter5_1_evanescia_101.wav")
            index = root / "index.csv"
            write_index(
                index,
                {
                    "index": "1",
                    "group": "chapter5_1",
                    "filename": "chapter5_1_evanescia_101.wav",
                    "source": "huggingface",
                    "source_detail": "simon3000/starrail-voice",
                    "english": "Official English line.",
                    "reference_text": "旧索引中的官方中文台词。",
                    "reference_language": "zh-CN",
                    "sha256": "",
                },
                [
                    "index", "group", "filename", "source", "source_detail", "english",
                    "reference_text", "reference_language", "sha256",
                ],
            )

            legacy_index, legacy_bilingual = write_legacy_inputs(index, None, root / "legacy")
            empty_labs = root / "labs"
            empty_labs.mkdir()
            entries, report = build_entries(
                legacy_index,
                legacy_bilingual,
                empty_labs,
                wavs,
                target_language="zh-CN",
            )

            self.assertEqual(entries[0].chinese, "旧索引中的官方中文台词。")
            self.assertTrue(entries[0].chinese_source.startswith("huggingface:Chinese(PRC):"))
            self.assertEqual(report["count_incremental_official_reference"], 1)
            self.assertEqual(report["count_missing_chinese"], 0)


if __name__ == "__main__":
    unittest.main()
