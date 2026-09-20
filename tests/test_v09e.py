from __future__ import annotations

import csv
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.builder import build_entries
from app.jobs import create_job, get_job
from app.pipeline import _load_translation_checkpoint, _write_translation_checkpoint
from app.project import create_project, recent_projects
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
