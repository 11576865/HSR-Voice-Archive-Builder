from __future__ import annotations

import csv
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.pipeline import _migrate_legacy_state, build_project_v02
from app.project import create_project, delete_project, project_summary


def write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * 80)


def write_index(path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "序号", "分组", "文件名", "来源", "来源细分", "英文文本", "SHA-256",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "序号": "1",
            "分组": "archive",
            "文件名": "a.wav",
            "来源": "test",
            "来源细分": "",
            "英文文本": "Test line.",
            "SHA-256": "",
        })


class V09FStateLayoutTests(unittest.TestCase):
    def test_project_build_separates_state_from_user_outputs_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            write_wav(wavs / "a.wav")
            index = root / "index.csv"
            write_index(index)
            out = root / "project" / "output"
            state = root / "project" / ".state"

            first = build_project_v02(
                index,
                wavs,
                out,
                make_flac=False,
                state_dir=state,
            )

            for name in (
                "manifest.json",
                "manifest.csv",
                "bilingual_index_corrected.csv",
                "bilingual.srt",
                "build_report.json",
            ):
                self.assertTrue((out / name).is_file(), name)

            for legacy_internal in (
                "translation_qa.json",
                "semantic_qa.json",
                "translation_usage.json",
                ".translation_checkpoint.json",
                "stages",
            ):
                self.assertFalse((out / legacy_internal).exists(), legacy_internal)

            self.assertTrue((state / "stages" / "01_scan.json").is_file())
            self.assertTrue((state / "stages" / "02_metadata.json").is_file())
            self.assertTrue((state / "stages" / "03_translation.json").is_file())
            self.assertTrue((state / "stages" / "04_translation_qa.json").is_file())
            self.assertTrue((state / "stages" / "05_manifest.json").is_file())
            self.assertTrue((state / "stages" / "06_audio_state.json").is_file())
            self.assertTrue((state / "stages" / "final_report.json").is_file())
            self.assertTrue(first["state_layout"]["separate_from_output"])

            second = build_project_v02(
                index,
                wavs,
                out,
                make_flac=False,
                state_dir=state,
            )
            resumed = set(second["stage_resume"]["resumed"])
            self.assertTrue(
                {"scan", "metadata", "translation", "translation_qa", "manifest", "audio"}
                <= resumed
            )

    def test_legacy_internal_files_are_moved_without_overwriting_new_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "output"
            state = root / ".state"
            (out / "stages").mkdir(parents=True)
            (out / "stages" / "01_scan.json").write_text("{}", encoding="utf-8")
            (out / "translation_qa.json").write_text('{"legacy": true}', encoding="utf-8")
            (out / ".translation_checkpoint.json").write_text("{}", encoding="utf-8")

            migrated = _migrate_legacy_state(out, state)

            self.assertIn("stages", migrated)
            self.assertIn("translation_qa.json", migrated)
            self.assertIn(".translation_checkpoint.json", migrated)
            self.assertFalse((out / "stages").exists())
            self.assertFalse((out / "translation_qa.json").exists())
            self.assertTrue((state / "stages" / "01_scan.json").is_file())
            self.assertTrue((state / "translation_qa.json").is_file())

            (out / "semantic_qa.json").write_text("old", encoding="utf-8")
            (state / "semantic_qa.json").write_text("new", encoding="utf-8")
            migrated_again = _migrate_legacy_state(out, state)
            self.assertNotIn("semantic_qa.json", migrated_again)
            self.assertEqual((state / "semantic_qa.json").read_text(encoding="utf-8"), "new")
            self.assertTrue((out / "semantic_qa.json").is_file())

    def test_project_summary_uses_separate_state_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            project_root = root / "project"
            wavs = root / "wavs"
            write_wav(wavs / "a.wav")
            index = root / "index.csv"
            write_index(index)

            with patch("app.project.STATE_FILE", state_file):
                config = create_project(
                    project_root,
                    name="State layout",
                    index_csv=str(index),
                    wav_source=str(wavs),
                )
                out = project_root / "output"
                state = project_root / ".state"
                build_project_v02(
                    index,
                    wavs,
                    out,
                    make_flac=False,
                    state_dir=state,
                )
                summary = project_summary(config)

            self.assertTrue(summary["build_complete"])
            self.assertTrue(summary["outputs"]["manifest.json"]["exists"])
            self.assertTrue(summary["state_outputs"]["stages/final_report.json"]["exists"])
            self.assertFalse((out / "stages").exists())

    def test_manual_project_delete_removes_state_but_keeps_unrelated_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            project_root = root / "manual"
            with patch("app.project.STATE_FILE", state_file):
                create_project(
                    project_root,
                    name="Manual",
                    index_csv="index.csv",
                    wav_source=str(root / "voice.zip"),
                )
                (project_root / "output").mkdir()
                (project_root / "output" / "manifest.json").write_text("{}", encoding="utf-8")
                (project_root / ".state").mkdir()
                (project_root / ".state" / "translation_usage.json").write_text(
                    "{}", encoding="utf-8"
                )
                keep = project_root / "notes.txt"
                keep.write_text("keep", encoding="utf-8")

                delete_project(project_root)

            self.assertTrue(keep.is_file())
            self.assertFalse((project_root / "output").exists())
            self.assertFalse((project_root / ".state").exists())


if __name__ == "__main__":
    unittest.main()
