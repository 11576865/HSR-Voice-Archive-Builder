from __future__ import annotations

import csv
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

from app.jobs import create_job, get_job
from app.pipeline import build_project_v02


def write_wav(path: Path, frames: int = 80) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * frames)


def write_index(path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["index", "group", "filename", "english", "sha256"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "index": "1",
                "group": "scene",
                "filename": "a.wav",
                "english": "Hello.",
                "sha256": "",
            }
        )


class QuickBuildUiTests(unittest.TestCase):
    def test_common_timing_settings_are_visible_before_first_build(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="quickIntroGap"', html)
        self.assertIn('id="quickSameGroupGap"', html)
        self.assertIn('id="quickGroupGap"', html)
        self.assertIn('id="quickAiBudget" class="hidden"', html)
        self.assertIn("d.set('intro_gap'", html)
        self.assertIn("d.set('same_group_gap'", html)
        self.assertIn("d.set('group_gap'", html)

    def test_finished_files_have_user_facing_descriptions(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("const outputDescriptions=", html)
        self.assertIn("'continuous.flac':'连续播放的无损语音音频'", html)
        self.assertIn("'manifest.json':'完整的机器可读档案", html)
        self.assertIn("'build_report.json':'本次构建的统计", html)


class BuildProgressTests(unittest.TestCase):
    def test_build_jobs_report_progress_and_reject_concurrent_build(self) -> None:
        release = threading.Event()

        def run(report_progress):
            report_progress("translation", "3/6 AI 翻译：批次 1/2", 3, 6)
            release.wait(2)
            return {"ok": True}

        job = create_job("build", run, with_progress=True)
        deadline = time.time() + 2
        state = None
        while time.time() < deadline:
            state = get_job(job.id)
            if state and state["phase"] == "translation":
                break
            time.sleep(0.01)

        self.assertIsNotNone(state)
        self.assertEqual(state["progress_current"], 3)
        self.assertEqual(state["progress_total"], 6)
        self.assertIn("批次 1/2", state["message"])

        with self.assertRaisesRegex(RuntimeError, "已有构建任务正在运行"):
            create_job("quick-build", lambda: None)

        release.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            state = get_job(job.id)
            if state and state["state"] == "succeeded":
                break
            time.sleep(0.01)
        self.assertEqual(state["state"], "succeeded")

    def test_pipeline_reports_all_major_stages(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            wavs.mkdir()
            write_wav(wavs / "a.wav")
            index = root / "index.csv"
            write_index(index)
            events: list[tuple[str, str, int, int]] = []

            report = build_project_v02(
                index,
                wavs,
                root / "output",
                make_flac=False,
                progress_callback=lambda phase, message, current, total: events.append(
                    (phase, message, current, total)
                ),
            )

            phases = [event[0] for event in events]
            self.assertEqual(
                phases,
                ["prepare", "metadata", "translation", "manifest", "audio", "final"],
            )
            self.assertEqual(events[-1][2:], (6, 6))
            self.assertEqual(report["count_total"], 1)


if __name__ == "__main__":
    unittest.main()
