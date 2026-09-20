from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.huggingface_audio import resolve_targets


class HuggingFaceAudioTests(unittest.TestCase):
    def test_resolves_filename_hash_media_id_and_unique_text(self) -> None:
        records = {
            "English/111.wav": {"filename": "a", "inGameFilename": "English/voice/chapter5_1_hero_101.wem", "transcription": "First", "speaker": "Hero"},
            "English/deadbeef.wav": {"filename": "b", "transcription": "Second", "speaker": "Hero"},
            "English/333.wav": {"filename": "c", "voiceID": "98765", "transcription": "Third", "speaker": "Hero"},
            "English/444.wav": {"filename": "d", "transcription": "Unique line", "speaker": "Hero"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
            plan = resolve_targets(path, [
                {"filename": "chapter5_1_hero_101.wav", "hash": "", "english": "First"},
                {"filename": "chapter5_1_hero_102.wav", "hash": "deadbeef", "english": "Second"},
                {"filename": "Ev_archive_vo_avatar_cast_hero_98765.wav", "hash": "", "english": "Third"},
                {"filename": "chapter5_1_hero_104.wav", "hash": "", "english": "Unique line"},
            ])
        self.assertEqual(plan["total_rows"], 4)
        self.assertEqual(len(plan["targets"]), 4)
        self.assertEqual(plan["targets"]["chapter5_1_hero_101.wav"]["method"], "inGameFilename")
        self.assertEqual(plan["targets"]["chapter5_1_hero_102.wav"]["method"], "wav_hash")
        self.assertEqual(plan["targets"]["Ev_archive_vo_avatar_cast_hero_98765.wav"]["method"], "media_id")
        self.assertEqual(plan["targets"]["chapter5_1_hero_104.wav"]["method"], "unique_transcription")

    def test_does_not_guess_duplicate_transcription(self) -> None:
        records = {
            "English/1.wav": {"filename": "a", "transcription": "Again"},
            "English/2.wav": {"filename": "b", "transcription": "Again"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
            plan = resolve_targets(path, [{"filename": "chapter5_1_hero_1.wav", "english": "Again"}])
        self.assertFalse(plan["targets"])
        self.assertEqual(plan["unresolved"], ["chapter5_1_hero_1.wav"])


if __name__ == "__main__":
    unittest.main()
