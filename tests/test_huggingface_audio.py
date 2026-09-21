from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.huggingface_audio import confirmed_reference_metadata, resolve_targets


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

    def test_pairs_chinese_audio_and_official_text_by_ingame_path(self) -> None:
        records = {
            "Chinese(PRC)/101.wav": {
                "filename": "zh",
                "inGameFilename": "Chinese(PRC)/voice/chapter5_1_hero_101.wem",
                "transcription": "中文官方文本。",
                "speaker": "Hero",
            },
            "English/202.wav": {
                "filename": "en",
                "inGameFilename": "English/voice/chapter5_1_hero_101.wem",
                "transcription": "Official English text.",
                "speaker": "Hero",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
            plan = resolve_targets(path, [{
                "filename": "chapter5_1_hero_101.wav",
                "english": "Official English text.",
            }])
        reference = plan["reference_targets"]["chapter5_1_hero_101.wav"]
        self.assertEqual(reference["transcription"], "中文官方文本。")
        self.assertEqual(reference["reference_language"], "zh-CN")
        self.assertEqual(reference["method"], "same_ingame_filename")

    def test_does_not_pair_chinese_audio_by_translated_text(self) -> None:
        records = {
            "Chinese(PRC)/101.wav": {
                "filename": "zh",
                "inGameFilename": "Chinese(PRC)/voice/another_file.wem",
                "transcription": "Same idea.",
            },
            "English/202.wav": {
                "filename": "en",
                "inGameFilename": "English/voice/target_file.wem",
                "transcription": "Same idea.",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
            plan = resolve_targets(path, [{"filename": "target_file.wav", "english": "Same idea."}])
        self.assertFalse(plan["reference_targets"])

    def test_reference_text_requires_successfully_obtained_chinese_audio(self) -> None:
        plan = {"targets": {"line.wav": {
            "transcription": "官方中文文本",
            "reference_language": "zh-CN",
        }}}
        failed = confirmed_reference_metadata(plan, {
            "completed": [],
            "failed": [{"filename": "line.wav", "reason": "audio.src 不可用"}],
        })
        self.assertEqual(failed, {})
        completed = confirmed_reference_metadata(plan, {
            "completed": [{"filename": "line.wav", "status": "ok"}],
            "failed": [],
        })
        self.assertEqual(completed["line.wav"]["reference_text"], "官方中文文本")


if __name__ == "__main__":
    unittest.main()
