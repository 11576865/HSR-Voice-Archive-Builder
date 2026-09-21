from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.black_video_exporter import BlackVideoExporter


class BlackVideoExporterTests(unittest.TestCase):
    @patch("app.black_video_exporter.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
    @patch("app.black_video_exporter.subprocess.Popen")
    @patch("app.black_video_exporter.subprocess.run")
    def test_export_keeps_subtitles_external_and_reports_progress(self, run, popen, _which) -> None:
        run.side_effect = [
            Mock(stdout="12.5\n"),
            Mock(stdout=json.dumps({"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})),
        ]
        process = Mock(returncode=0)
        process.stdout = iter(["out_time_us=6250000\n", "progress=end\n"])
        process.stderr.read.return_value = ""
        process.wait.return_value = 0
        popen.return_value = process
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, target = root / "continuous.flac", root / "archive.mkv"
            source.write_bytes(b"flac")
            temporary = root / "archive.part.mkv"

            def fake_validate(path):
                self.assertEqual(Path(path), temporary)
                temporary.write_bytes(b"mkv")

            exporter = BlackVideoExporter()
            with patch.object(exporter, "validate", side_effect=fake_validate):
                result = exporter.export(source, target, lambda *args: events.append(args))

            command = popen.call_args.args[0]
            self.assertIn("-c:a", command)
            self.assertEqual(command[command.index("-c:a") + 1], "copy")
            self.assertNotIn("-vf", command)
            self.assertFalse(any(str(arg).endswith((".ass", ".srt")) for arg in command))
            self.assertFalse(result["subtitles_embedded"])
            self.assertEqual(events[-1][:2], (12500, 12500))
            self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
