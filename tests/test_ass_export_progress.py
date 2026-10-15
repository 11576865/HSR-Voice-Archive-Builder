"""Progress from the actual ASS writer must reach the job's cue counter."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.timeline import write_ass
from subtitle_layout.ass_writer import render_ass
from subtitle_layout.config import SubtitleRenderConfig


def entries():
    for index in range(2):
        yield SimpleNamespace(
            id=index + 1, english=f"Subtitle {index + 1}.",
            chinese=f"字幕{index + 1}。",
            start_seconds=1.0 + index * 3.0,
            display_end_seconds=3.0 + index * 3.0,
        )


class AssExportProgressTests(unittest.TestCase):
    def test_render_ass_reports_real_cue_count(self):
        events = []
        ass = render_ass(
            entries(), config=SubtitleRenderConfig.plain_text_preset(),
            progress_callback=lambda current, total: events.append((current, total)),
        )
        self.assertTrue(ass.startswith("[Script Info]"))
        self.assertEqual(events[0], (0, 2))
        self.assertEqual(events[-1], (2, 2))
        self.assertTrue(all(total == 2 for _, total in events))

    def test_timeline_writer_forwards_progress_to_ass_writer(self):
        events = []
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "archive.ass"
            write_ass(
                entries(), path,
                config=SubtitleRenderConfig.plain_text_preset(),
                progress_callback=lambda current, total: events.append((current, total)),
            )
            self.assertTrue(path.is_file())
            self.assertIn("Subtitle 1.", path.read_text(encoding="utf-8-sig"))
        self.assertEqual(events[0], (0, 2))
        self.assertEqual(events[-1], (2, 2))


if __name__ == "__main__":
    unittest.main()
