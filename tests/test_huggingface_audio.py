from __future__ import annotations

import json
import io
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from app.huggingface_audio import confirmed_reference_metadata, resolve_targets, download_resolved_audio


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


    def test_genshin_resolves_by_ingame_basename_and_keeps_dataset_identity(self) -> None:
        records = {
            "English(US)/aaa.wav": {
                "filename": "aaa",
                "inGameFilename": "VO_anecdote\\vo_anecdote_106701_hutao_01.wem",
                "transcription": "A Genshin line.",
                "speaker": "Hu Tao",
            },
            "Chinese(PRC)/bbb.wav": {
                "filename": "bbb",
                "inGameFilename": "VO_anecdote\\vo_anecdote_106701_hutao_01.wem",
                "transcription": "原神中文台词。",
                "speaker": "胡桃",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
            plan = resolve_targets(
                path,
                [{
                    "filename": "vo_anecdote_106701_hutao_01.wav",
                    "english": "A Genshin line.",
                }],
                dataset="simon3000/genshin-voice",
            )
        self.assertEqual(plan["dataset"], "simon3000/genshin-voice")
        row = plan["targets"]["vo_anecdote_106701_hutao_01.wav"]
        self.assertEqual(row["method"], "inGameFilenameBasename")
        ref = plan["reference_targets"]["vo_anecdote_106701_hutao_01.wav"]
        self.assertEqual(ref["transcription"], "原神中文台词。")

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


    def test_interrupted_part_is_not_a_completed_audio_download(self) -> None:
        wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\\0" * 32
        plan = {"targets": {"voice_001.wav": {"row_idx": 0}}}
        payload = {"rows": [{"row_idx": 0, "row": {"audio": {"src": "https://huggingface.co/voice.wav"}}}]}
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td)
            (destination / "voice_001.wav.part").write_bytes(b"interrupted download")
            with patch("app.huggingface_audio._json_get", return_value=payload), \\
                 patch("app.huggingface_audio.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(wav)
                result = download_resolved_audio(plan, destination)
            self.assertEqual(result["failed"], [])
            self.assertEqual(result["completed"][0]["status"], "ok")
            self.assertEqual((destination / "voice_001.wav").read_bytes(), wav)
            opener.return_value.open.assert_called_once()

    def test_invalid_existing_audio_does_not_claim_success(self) -> None:
        plan = {"targets": {"voice_001.wav": {"row_idx": 0}}}
        payload = {"rows": [{"row_idx": 0, "row": {"audio": {"src": "https://huggingface.co/voice.wav"}}}]}
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td)
            (destination / "voice_001.wav").write_bytes(b"corrupt")
            (destination / "voice_001.txt").write_text("not audio")
            with patch("app.huggingface_audio._json_get", return_value=payload), \\
                 patch("app.huggingface_audio.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(b"invalid response")
                result = download_resolved_audio(plan, destination)
            self.assertEqual(result["completed"], [])
            self.assertEqual(len(result["failed"]), 1)
            self.assertEqual((destination / "voice_001.wav").read_bytes(), b"corrupt")

    def test_audio_bytes_are_bounded_and_private_urls_refused(self) -> None:
        plan = {"targets": {"voice_001.wav": {"row_idx": 0}}}
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td)
            payload = {"rows": [{"row_idx": 0, "row": {"audio": {"src": "https://huggingface.co/voice.wav"}}}]}
            with patch("app.huggingface_audio._json_get", return_value=payload), \\
                 patch("app.huggingface_audio.MAX_AUDIO_DOWNLOAD_BYTES", 16), \\
                 patch("app.huggingface_audio.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(b"RIFF" + b"\\0" * 8 + b"too-big" * 8)
                bounded = download_resolved_audio(plan, destination)
            self.assertEqual(bounded["completed"], [])
            self.assertIn("128 MiB budget", bounded["failed"][0]["reason"])
            self.assertFalse(list(destination.glob(".hsr-audio-*.part")))
            payload["rows"][0]["row"]["audio"]["src"] = "https://127.0.0.1/private"
            with patch("app.huggingface_audio._json_get", return_value=payload):
                blocked = download_resolved_audio(plan, destination)
            self.assertEqual(blocked["completed"], [])
            self.assertIn("Private", blocked["failed"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
