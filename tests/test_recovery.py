from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.project import create_project, update_project
from app.recovery import build_text_recovery, import_text_recovery


def write_index(path: Path, english: str = "Hello.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "index", "group", "filename", "english", "reference_text",
                "reference_language", "sha256",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "index": "1",
            "group": "scene",
            "filename": "a.wav",
            "english": english,
            "reference_text": "",
            "reference_language": "",
            "sha256": "",
        })


def input_sha(english: str = "Hello.") -> str:
    import hashlib
    payload = {
        "english": english,
        "reference_text": "",
        "reference_language": "",
        "source_language": "en",
        "target_language": "zh-CN",
    }
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def checkpoint(model: str = "gpt-5.6-terra", english: str = "Hello.") -> dict:
    return {
        "schema_version": 3,
        "provider": "vapi",
        "base_url": "https://api.gpt.ge/v1",
        "model": model,
        "source_language": "en",
        "target_language": "zh-CN",
        "records": {
            "a.wav": {
                "english_sha256": "",
                "input_sha256": input_sha(english),
                "chinese": "你好。",
                "qa_version": 1,
                "qa_issues": [],
                "glossary_fingerprint": "",
            }
        },
    }


class TextRecoveryTests(unittest.TestCase):
    def test_export_contains_text_state_but_no_audio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            project_root = root / "project"
            write_index(project_root / ".generated" / "quick_index.csv")
            (project_root / ".generated" / "incremental_audio").mkdir(parents=True)
            (project_root / ".generated" / "incremental_audio" / "large.wav").write_bytes(b"x" * 100)
            (project_root / ".state").mkdir()
            (project_root / ".state" / ".translation_checkpoint.json").write_text(
                json.dumps(checkpoint(), ensure_ascii=False), encoding="utf-8"
            )

            with patch("app.project.STATE_FILE", state_file):
                config = create_project(
                    project_root,
                    name="Recovery",
                    index_csv=".generated/quick_index.csv",
                    wav_source=str(root / "voice.zip")
                )
                data, filename, summary = build_text_recovery(config)

            package = json.loads(data.decode("utf-8"))
            self.assertTrue(filename.endswith(".hsrbackup"))
            self.assertFalse(summary["contains_audio"])
            self.assertEqual(summary["translation_records"], 1)
            self.assertIn("state/.translation_checkpoint.json", package["files"])
            self.assertIn("generated/quick_index.csv", package["files"])
            self.assertFalse(any("wav" in name.lower() for name in package["files"]))
            self.assertFalse(package["policy"]["contains_audio"])

    def test_import_restores_checkpoint_and_reports_current_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            source_root = root / "source"
            target_root = root / "target"
            write_index(source_root / ".generated" / "quick_index.csv")
            (source_root / ".state").mkdir(parents=True)
            (source_root / ".state" / ".translation_checkpoint.json").write_text(
                json.dumps(checkpoint(), ensure_ascii=False), encoding="utf-8"
            )
            write_index(target_root / ".generated" / "quick_index.csv")

            with patch("app.project.STATE_FILE", state_file):
                source = create_project(
                    source_root,
                    name="Source",
                    index_csv=".generated/quick_index.csv",
                    wav_source=str(root / "voice.zip")
                )
                target = create_project(
                    target_root,
                    name="Target",
                    index_csv=".generated/quick_index.csv",
                    wav_source=str(root / "voice.zip")
                )
                data, _, _ = build_text_recovery(source)
                result = import_text_recovery(target, data.decode("utf-8"))

            restored = json.loads(
                (target_root / ".state" / ".translation_checkpoint.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(restored["records"]["a.wav"]["chinese"], "你好。")
            self.assertEqual(result["matching_now"], 1)
            self.assertEqual(result["changed_now"], 0)
            self.assertEqual(result["imported_records"], 1)
            self.assertFalse(result["checkpoint_conflict"])
            self.assertTrue(Path(result["preserved_package"]).is_file())

    def test_import_does_not_overwrite_different_checkpoint_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            source_root = root / "source"
            target_root = root / "target"
            write_index(source_root / ".generated" / "quick_index.csv")
            write_index(target_root / ".generated" / "quick_index.csv")
            (source_root / ".state").mkdir(parents=True)
            (source_root / ".state" / ".translation_checkpoint.json").write_text(
                json.dumps(checkpoint("old-model"), ensure_ascii=False), encoding="utf-8"
            )
            (target_root / ".state").mkdir(parents=True)
            existing = checkpoint("new-model")
            existing["records"]["a.wav"]["chinese"] = "现有译文。"
            (target_root / ".state" / ".translation_checkpoint.json").write_text(
                json.dumps(existing, ensure_ascii=False), encoding="utf-8"
            )

            with patch("app.project.STATE_FILE", state_file):
                source = create_project(
                    source_root,
                    name="Source",
                    index_csv=".generated/quick_index.csv",
                    wav_source=str(root / "voice.zip")
                )
                target = create_project(
                    target_root,
                    name="Target",
                    index_csv=".generated/quick_index.csv",
                    wav_source=str(root / "voice.zip")
                )
                update_project(source, translation_model="old-model")
                update_project(target, translation_model="new-model")
                data, _, _ = build_text_recovery(source)
                result = import_text_recovery(target, data.decode("utf-8"))

            after = json.loads(
                (target_root / ".state" / ".translation_checkpoint.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(result["checkpoint_conflict"])
            self.assertEqual(result["imported_records"], 0)
            self.assertEqual(after["model"], "new-model")
            self.assertEqual(after["records"]["a.wav"]["chinese"], "现有译文。")

    def test_import_rejects_modified_package(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "app-state.json"
            project_root = root / "project"
            write_index(project_root / ".generated" / "quick_index.csv")
            (project_root / ".state").mkdir(parents=True)
            (project_root / ".state" / ".translation_checkpoint.json").write_text(
                json.dumps(checkpoint(), ensure_ascii=False), encoding="utf-8"
            )
            with patch("app.project.STATE_FILE", state_file):
                config = create_project(
                    project_root,
                    name="Recovery",
                    index_csv=".generated/quick_index.csv",
                    wav_source=str(root / "voice.zip"),
                )
                data, _, _ = build_text_recovery(config)

            package = json.loads(data.decode("utf-8"))
            package["files"]["state/.translation_checkpoint.json"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                import_text_recovery(config, json.dumps(package, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
