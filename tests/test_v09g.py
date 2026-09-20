from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.project import create_project, project_summary, update_project
from app.quick import relink_project_source, source_inventory


def write_voice_zip(path: Path, payload: bytes) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("archive_test_1.wav", payload)


class V09GSourceRelinkTests(unittest.TestCase):
    def test_verified_primary_relink_preserves_embedded_reference_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "state.json"
            original = root / "original.zip"
            moved = root / "moved.zip"
            write_voice_zip(original, b"same-audio")
            shutil.copy2(original, moved)
            digest = source_inventory(original)["fingerprint"]["digest"]

            with patch("app.project.STATE_FILE", state_file):
                config = create_project(
                    root / "project",
                    name="Relink",
                    index_csv="index.csv",
                    wav_source=str(original),
                    wav_source_fingerprint=digest,
                    reference_source=str(root / "reference.zip"),
                    reference_text_embedded=True,
                )
                config, result = relink_project_source(config, "primary", moved)

            self.assertTrue(result["verified"])
            self.assertEqual(Path(config.wav_source), moved.resolve())
            self.assertEqual(config.wav_source_fingerprint, digest)
            self.assertTrue(config.reference_text_embedded)

    def test_relink_rejects_different_package_and_keeps_old_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "state.json"
            original = root / "original.zip"
            other = root / "other.zip"
            write_voice_zip(original, b"one")
            write_voice_zip(other, b"two")
            digest = source_inventory(original)["fingerprint"]["digest"]

            with patch("app.project.STATE_FILE", state_file):
                config = create_project(
                    root / "project",
                    name="Relink",
                    index_csv="index.csv",
                    wav_source=str(original),
                    wav_source_fingerprint=digest,
                )
                with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
                    relink_project_source(config, "primary", other)

            self.assertEqual(Path(config.wav_source), original.resolve())
            self.assertEqual(config.wav_source_fingerprint, digest)

    def test_legacy_quick_scan_fingerprint_can_verify_relink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "state.json"
            original = root / "original.zip"
            moved = root / "moved.zip"
            write_voice_zip(original, b"legacy")
            shutil.copy2(original, moved)
            digest = source_inventory(original)["fingerprint"]["digest"]

            with patch("app.project.STATE_FILE", state_file):
                config = create_project(
                    root / "project",
                    name="Legacy",
                    index_csv=".generated/quick_index.csv",
                    wav_source=str(original),
                    managed_project_root=True,
                )
                generated = Path(config.root) / ".generated"
                generated.mkdir(exist_ok=True)
                (generated / "quick_scan.json").write_text(
                    json.dumps({
                        "english": {
                            "fingerprint": {
                                "algorithm": "sha256",
                                "digest": digest,
                            }
                        }
                    }),
                    encoding="utf-8",
                )
                config, result = relink_project_source(config, "primary", moved)

            self.assertTrue(result["verified"])
            self.assertEqual(config.wav_source_fingerprint, digest)
            self.assertEqual(Path(config.wav_source), moved.resolve())

    def test_manual_source_change_clears_old_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "state.json"
            first = root / "first.zip"
            second = root / "second.zip"
            write_voice_zip(first, b"one")
            write_voice_zip(second, b"two")
            digest = source_inventory(first)["fingerprint"]["digest"]

            with patch("app.project.STATE_FILE", state_file):
                config = create_project(
                    root / "project",
                    name="Manual change",
                    index_csv="index.csv",
                    wav_source=str(first),
                    wav_source_fingerprint=digest,
                )
                update_project(config, wav_source=str(second))

            self.assertEqual(config.wav_source_fingerprint, "")

    def test_project_summary_reports_missing_and_available_sources(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_file = root / "state.json"
            primary = root / "voice.zip"
            write_voice_zip(primary, b"voice")
            project_root = root / "project"
            (project_root / ".generated").mkdir(parents=True)
            (project_root / ".generated" / "quick_index.csv").write_text(
                "index,group,filename,source,source_detail,english,sha256\n",
                encoding="utf-8",
            )

            with patch("app.project.STATE_FILE", state_file):
                config = create_project(
                    project_root,
                    name="Health",
                    index_csv=".generated/quick_index.csv",
                    wav_source=str(primary),
                    chs_source=str(root / "missing-target.zip"),
                )
                summary = project_summary(config)

            self.assertTrue(summary["source_status"]["primary"]["exists"])
            self.assertFalse(summary["source_status"]["target"]["exists"])
            self.assertTrue(summary["source_status"]["target"]["configured"])
            self.assertTrue(summary["source_status"]["index"]["exists"])


if __name__ == "__main__":
    unittest.main()
