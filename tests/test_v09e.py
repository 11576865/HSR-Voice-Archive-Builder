from __future__ import annotations

import csv
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.builder import build_entries
from app.jobs import create_job, get_job
from app.pipeline import _load_translation_checkpoint, _write_translation_checkpoint
from app.project import create_project, recent_projects
from app.quick import _map_reference_records, create_quick_project, quick_scan
from app.remote_index import ai_hobbyist_index_url
from app.semantic_quality import semantic_risk_tags
from app.translator import _translation_prompt


def write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * 80)


class V09EProjectAndLanguageRoleTests(unittest.TestCase):
    def test_recent_projects_keeps_multiple_projects(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state.json"
            with patch("app.project.STATE_FILE", state):
                a = create_project(
                    root / "A",
                    name="A",
                    index_csv="index.csv",
                    wav_source="voice.zip",
                )
                b = create_project(
                    root / "B",
                    name="B",
                    index_csv="index.csv",
                    wav_source="voice.zip",
                )
                rows = recent_projects(10)
            self.assertEqual([row["name"] for row in rows[:2]], ["B", "A"])
            self.assertEqual(Path(rows[0]["root"]), Path(b.root))
            self.assertEqual(Path(rows[1]["root"]), Path(a.root))

    def test_job_records_project_identity(self) -> None:
        job = create_job(
            "unit-project",
            lambda: {"ok": True},
            project_root="/tmp/project-a",
            project_name="Project A",
        )
        state = get_job(job.id)
        self.assertIsNotNone(state)
        self.assertEqual(state["project_root"], "/tmp/project-a")
        self.assertEqual(state["project_name"], "Project A")

    def test_translation_prompt_uses_language_roles_and_reference_text(self) -> None:
        prompt = _translation_prompt(
            [
                {
                    "id": "a.wav",
                    "english": "行くよ。",
                    "reference_text": "I'm going.",
                    "reference_language": "en",
                }
            ],
            None,
            source_language="ja",
            target_language="ko",
        )
        self.assertIn("from ja", prompt)
        self.assertIn("into ko", prompt)
        self.assertIn("reference_text", prompt)
        self.assertIn("I'm going.", prompt)

    def test_builtin_remote_indexes_cover_supported_voice_languages(self) -> None:
        self.assertTrue(ai_hobbyist_index_url("en").endswith("/EN.xlsx"))
        self.assertTrue(ai_hobbyist_index_url("zh-CN").endswith("/CHS.xlsx"))
        self.assertTrue(ai_hobbyist_index_url("ja").endswith("/JP.xlsx"))
        self.assertTrue(ai_hobbyist_index_url("ko").endswith("/KR.xlsx"))
        with self.assertRaisesRegex(ValueError, "No built-in"):
            ai_hobbyist_index_url("ru")

    def test_non_english_quick_scan_uses_remote_language_index_without_local_lab(self) -> None:
        inventory = {
            "source": "/fake/voice.7z",
            "kind": "7z",
            "file_count": 1,
            "wav_count": 1,
            "lab_count": 0,
            "wav_names": ["archive_evanescia_1.wav"],
            "lab_names": [],
            "duplicate_wav_names": [],
            "wav_lab_pairs": 0,
            "wav_without_lab": 1,
            "declared_bytes": 123,
            "fingerprint": {
                "algorithm": "sha256",
                "digest": "abc",
                "source_size_bytes": 123,
                "source_modified_ns": 1,
            },
        }
        records = [
            {
                "filename": "archive_evanescia_1.wav",
                "hash": "",
                "character": "Evanescia",
                "english": "行くよ。",
                "battle": "",
            }
        ]
        with (
            patch("app.quick.source_inventory", return_value=inventory),
            patch("app.quick._index_candidates", return_value=[]),
            patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                return_value=(records, {"cache_hit": False, "stale": False}),
            ) as fetched,
            patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "custom",
                    "base_url": "https://example.invalid/v1",
                    "configured": True,
                },
            ),
        ):
            plan = quick_scan(Path("/fake/voice.7z"), source_text_language="ja")

        self.assertTrue(plan["ready"])
        self.assertEqual(plan["source_text_language"], "ja")
        self.assertTrue(plan["translation"]["remote_index_url"].endswith("/JP.xlsx"))
        self.assertEqual(plan["index"]["english_matched"], 1)
        called_url = fetched.call_args.args[1]
        self.assertTrue(called_url.endswith("/JP.xlsx"))

    def test_reference_mapping_can_bridge_localized_character_token(self) -> None:
        mapped, detail = _map_reference_records(
            {
                "chapter5_13_evanescia_103.wav",
                "archive_evanescia_1.wav",
            },
            [
                {
                    "filename": "chapter5_13_绯英_103.wav",
                    "english": "参考剧情台词",
                },
                {
                    "filename": "archive_绯英_1.wav",
                    "english": "参考档案台词",
                },
            ],
        )
        self.assertEqual(mapped["chapter5_13_evanescia_103.wav"], "参考剧情台词")
        self.assertEqual(mapped["archive_evanescia_1.wav"], "参考档案台词")
        self.assertEqual(detail["structural"], 2)

    def test_audio_only_reference_package_is_materialized_into_quick_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary = root / "English.zip"
            reference = root / "Japanese.zip"
            primary_names = [
                "chapter5_13_evanescia_103.wav",
                "archive_evanescia_1.wav",
            ]
            reference_names = [
                "chapter5_13_绯英_103.wav",
                "archive_绯英_1.wav",
            ]
            with zipfile.ZipFile(primary, "w") as z:
                for name in primary_names:
                    z.writestr(name, b"RIFF")
            with zipfile.ZipFile(reference, "w") as z:
                for name in reference_names:
                    z.writestr(name, b"RIFF")

            source_records = [
                {
                    "filename": primary_names[0],
                    "english": "Story source.",
                    "hash": "",
                    "character": "Evanescia",
                },
                {
                    "filename": primary_names[1],
                    "english": "Archive source.",
                    "hash": "",
                    "character": "Evanescia",
                },
            ]
            reference_records = [
                {
                    "filename": reference_names[0],
                    "english": "物語の参考。",
                    "hash": "",
                    "character": "Evanescia",
                },
                {
                    "filename": reference_names[1],
                    "english": "アーカイブの参考。",
                    "hash": "",
                    "character": "Evanescia",
                },
            ]

            def fake_fetch(names, url):
                if url.endswith("/JP.xlsx"):
                    return reference_records, {"cache_hit": True, "stale": False}
                return source_records, {"cache_hit": True, "stale": False}

            with (
                patch(
                    "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                    side_effect=fake_fetch,
                ),
                patch(
                    "app.quick.credentials_status",
                    return_value={
                        "provider": "custom",
                        "base_url": "https://example.invalid/v1",
                        "configured": False,
                    },
                ),
            ):
                config, plan = create_quick_project(
                    primary,
                    reference_source=reference,
                    reference_language="ja",
                    root=root / "project",
                )

            self.assertTrue(config.reference_text_embedded)
            self.assertEqual(plan["reference_text_match"]["total"], 2)
            generated = Path(config.root) / ".generated" / "quick_index.csv"
            with generated.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            by_name = {row["filename"]: row for row in rows}
            self.assertEqual(
                by_name[primary_names[0]]["reference_text"],
                "物語の参考。",
            )
            self.assertEqual(by_name[primary_names[0]]["reference_language"], "ja")
            self.assertEqual(
                by_name[primary_names[1]]["reference_text"],
                "アーカイブの参考。",
            )

    def test_multilingual_semantic_risk_heuristics(self) -> None:
        self.assertIn("negation", semantic_risk_tags("私は行かない。", "ja"))
        self.assertIn("condition", semantic_risk_tags("如果你来，我就走。", "zh-CN"))
        self.assertIn("negation", semantic_risk_tags("나는 가지 않을 거야.", "ko"))
        self.assertIn("quantity", semantic_risk_tags("あと 2 回。", "ja"))

    def test_reference_lab_is_attached_but_not_used_as_audio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            wavs.mkdir()
            write_wav(wavs / "a.wav")
            chs = root / "target"
            chs.mkdir()
            ref = root / "reference"
            ref.mkdir()
            (ref / "a.lab").write_text("参考文本", encoding="utf-8")

            index = root / "index.csv"
            with index.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=["序号", "分组", "文件名", "来源", "来源细分", "英文文本", "SHA-256"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "序号": "1",
                        "分组": "g",
                        "文件名": "a.wav",
                        "来源": "",
                        "来源细分": "",
                        "英文文本": "Source text",
                        "SHA-256": "",
                    }
                )

            bilingual = root / "bilingual.csv"
            with bilingual.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["文件名", "中文", "ENGLISH"])
                writer.writeheader()
                writer.writerow({"文件名": "a.wav", "中文": "", "ENGLISH": "Source text"})

            entries, report = build_entries(
                index,
                bilingual,
                chs,
                wavs,
                reference_lab_root=ref,
                reference_language="zh-CN",
            )
            self.assertEqual(entries[0].reference_text, "参考文本")
            self.assertEqual(entries[0].reference_language, "zh-CN")
            self.assertEqual(report["count_reference_lab"], 1)
            self.assertEqual(entries[0].filename, "a.wav")

    def test_non_english_source_text_can_come_from_primary_lab(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            wavs.mkdir()
            write_wav(wavs / "a.wav")
            (wavs / "a.lab").write_text("行くよ。", encoding="utf-8")
            chs = root / "target"
            chs.mkdir()

            index = root / "index.csv"
            with index.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=["序号", "分组", "文件名", "来源", "来源细分", "英文文本", "SHA-256"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "序号": "1",
                        "分组": "g",
                        "文件名": "a.wav",
                        "来源": "",
                        "来源细分": "",
                        "英文文本": "I am going.",
                        "SHA-256": "",
                    }
                )

            bilingual = root / "bilingual.csv"
            with bilingual.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["文件名", "中文", "ENGLISH"])
                writer.writeheader()
                writer.writerow({"文件名": "a.wav", "中文": "", "ENGLISH": "I am going."})

            entries, report = build_entries(
                index,
                bilingual,
                chs,
                wavs,
                source_text_language="ja",
            )
            self.assertEqual(entries[0].english, "行くよ。")
            self.assertEqual(report["count_source_lab"], 1)

    def test_checkpoint_is_invalidated_when_target_language_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "checkpoint.json"
            records = {
                "a.wav": {
                    "english_sha256": "x",
                    "chinese": "旧目标文本",
                }
            }
            _write_translation_checkpoint(
                path,
                "model",
                "custom",
                "https://example.invalid/v1",
                records,
                "en",
                "zh-CN",
            )
            reused = _load_translation_checkpoint(
                path,
                "model",
                "custom",
                "https://example.invalid/v1",
                "en",
                "zh-CN",
            )
            changed = _load_translation_checkpoint(
                path,
                "model",
                "custom",
                "https://example.invalid/v1",
                "en",
                "ja",
            )
            self.assertIn("a.wav", reused)
            self.assertEqual(changed, {})


if __name__ == "__main__":
    unittest.main()
