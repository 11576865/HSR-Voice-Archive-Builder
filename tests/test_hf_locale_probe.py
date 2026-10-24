"""Offline acceptance of the opt-in, read-only Japanese HF parity probe."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.hf_locale_probe import probe_japanese_row_identity


class JapaneseHfMetadataProbeTests(unittest.TestCase):
    def _index(self, folder: Path) -> Path:
        path = folder / "result.json"
        path.write_text(json.dumps({
            "English/en.wav": {
                "filename": "en",
                "inGameFilename": "English/voice/en.wem",
            },
            "Japanese/ja1.wav": {
                "filename": "ja1",
                "inGameFilename": "Japanese/voice/one.wem",
            },
            "Japanese/ja2.wav": {
                "filename": "ja2",
                "inGameFilename": "Japanese/voice/two.wem",
            },
            "Japanese/orphan.wav": {"filename": "orphan", "inGameFilename": ""},
        }, separators=(",", ":")), encoding="utf-8")
        return path

    def _fetch(self, wrong_second: bool = False):
        def fetch(url, params):
            self.assertIn("/rows", url)
            self.assertEqual(params["dataset"], "simon3000/starrail-voice")
            self.assertEqual(params["offset"], 0)
            return {"rows": [
                {"row_idx": 0, "row": {
                    "language": "English",
                    "ingame_filename": "English/voice/en.wem",
                }},
                {"row_idx": 1, "row": {
                    "language": "Japanese",
                    "ingame_filename": "Japanese/voice/one.wem",
                }},
                {"row_idx": 2, "row": {
                    "language": "Japanese",
                    "ingame_filename": (
                        "Japanese/voice/drifted.wem"
                        if wrong_second else "Japanese/voice/two.wem"
                    ),
                }},
            ]}
        return fetch

    def test_metadata_identity_probe_reports_two_matching_japanese_rows(self):
        with tempfile.TemporaryDirectory() as td:
            report = probe_japanese_row_identity(
                self._index(Path(td)), samples=2, fetch=self._fetch()
            )
            self.assertTrue(report["passed"])
            self.assertEqual(report["checked"], 2)
            self.assertEqual(report["matched"], 2)
            self.assertEqual([row["row_idx"] for row in report["evidence"]], [1, 2])
            self.assertFalse(report["audio_downloaded"])
            self.assertFalse(report["spoken_language_verified"])

    def test_moved_japanese_row_is_not_claimed_as_verified(self):
        with tempfile.TemporaryDirectory() as td:
            report = probe_japanese_row_identity(
                self._index(Path(td)), samples=2,
                fetch=self._fetch(wrong_second=True),
            )
            self.assertFalse(report["passed"])
            self.assertEqual(report["matched"], 1)
            self.assertEqual(report["evidence"][-1]["status"], "mismatch")
            self.assertIn("identity changed", report["evidence"][-1]["reason"])

    def test_missing_identity_is_not_sampled(self):
        with tempfile.TemporaryDirectory() as td:
            report = probe_japanese_row_identity(
                self._index(Path(td)), samples=3, fetch=self._fetch(),
            )
            self.assertFalse(report["passed"])
            self.assertEqual(report["checked"], 2)

    def test_rejects_missing_input_and_unbounded_sample_count(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                probe_japanese_row_identity(Path(td) / "does-not-exist.json")
            with self.assertRaisesRegex(ValueError, "between 1 and 20"):
                probe_japanese_row_identity(Path(td) / "does-not-exist.json", samples=21)


if __name__ == "__main__":
    unittest.main()
