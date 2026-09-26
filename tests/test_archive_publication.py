from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.pipeline import build_project_v02
from app.project import ProjectConfig, project_summary
from app.publishing import BUILD_MARKER
from app.subtitles import update_project_subtitles


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class ArchivePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wavs = self.root / "wavs"
        self.wavs.mkdir()
        with wave.open(str(self.wavs / "voice.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x01\x00" * 8000)
        self.index = self.root / "index.csv"
        with self.index.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=["index", "group", "filename", "english", "sha256"]
            )
            writer.writeheader()
            writer.writerow({
                "index": 1, "group": "archive", "filename": "voice.wav",
                "english": "Hello", "sha256": "",
            })
        self.output = self.root / "output"
        self.state = self.root / ".state"
        self.config = ProjectConfig(1, "test", str(self.root), "index.csv", "wavs")

    def build(self, intro_gap: float = 1.0) -> dict[str, object]:
        return build_project_v02(
            self.index, self.wavs, self.output,
            intro_gap=intro_gap, same_group_gap=0.0, group_gap=0.0,
            state_dir=self.state,
        )

    def test_human_subtitle_remains_in_every_output_after_full_rebuild(self) -> None:
        self.build()
        update_project_subtitles(
            self.config, self.output, [{"id": 1, "final_chs": "人工校订文本"}]
        )
        self.build()

        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["entries"][0]["final_chs"], "人工校订文本")
        self.assertIn("人工校订文本", (self.output / "HSR_Voice_Archive.srt").read_text(encoding="utf-8-sig"))
        with (self.output / "bilingual_index_corrected.csv").open(encoding="utf-8-sig") as stream:
            self.assertEqual(list(csv.DictReader(stream))[0]["final_chs"], "人工校订文本")

    def test_failed_encode_restores_complete_old_archive(self) -> None:
        self.build()
        before = {
            name: (self.output / name).read_bytes()
            for name in ("manifest.json", "HSR_Voice_Archive.srt", "continuous.flac", "build_report.json")
        }
        with patch("app.pipeline.build_continuous_flac", side_effect=RuntimeError("encoder failed")):
            with self.assertRaisesRegex(RuntimeError, "encoder failed"):
                self.build(intro_gap=3.0)

        for name, original in before.items():
            self.assertEqual((self.output / name).read_bytes(), original, name)
        self.assertTrue(project_summary(self.config)["build_complete"])
        self.assertFalse((self.output / BUILD_MARKER).exists())

    def test_failed_final_stage_restores_old_audio_and_timeline(self) -> None:
        self.build()
        old_audio = hashlib.sha256((self.output / "continuous.flac").read_bytes()).hexdigest()
        original_save_stage = __import__("app.pipeline", fromlist=["save_stage"]).save_stage

        def fail_final(*args, **kwargs):
            if args[2] == "final_report":
                raise RuntimeError("report failed")
            return original_save_stage(*args, **kwargs)

        with patch("app.pipeline.save_stage", side_effect=fail_final):
            with self.assertRaisesRegex(RuntimeError, "report failed"):
                self.build(intro_gap=3.0)

        self.assertEqual(
            hashlib.sha256((self.output / "continuous.flac").read_bytes()).hexdigest(), old_audio
        )
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["entries"][0]["start_seconds"], 1.0)
        self.assertTrue(project_summary(self.config)["build_complete"])

    def test_failed_rebuild_restores_audio_when_hard_links_are_unavailable(self) -> None:
        self.build()
        old_audio = (self.output / "continuous.flac").read_bytes()
        with patch("app.publishing.os.link", side_effect=OSError("unsupported")), patch(
            "app.pipeline.build_continuous_flac", side_effect=RuntimeError("encoder failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "encoder failed"):
                self.build(intro_gap=3.0)
        self.assertEqual((self.output / "continuous.flac").read_bytes(), old_audio)

    def test_interrupted_marker_does_not_report_completed_archive(self) -> None:
        self.build()
        (self.output / BUILD_MARKER).touch()
        self.assertFalse(project_summary(self.config)["build_complete"])
        self.assertTrue(project_summary(self.config)["interrupted_build"])
        self.build()
        self.assertTrue(project_summary(self.config)["build_complete"])
        self.assertFalse((self.output / BUILD_MARKER).exists())
