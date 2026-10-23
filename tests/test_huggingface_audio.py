from __future__ import annotations

import json
import io
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from app.huggingface_audio import confirmed_reference_metadata, resolve_targets, download_resolved_audio, validated_incremental_files


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
        wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\0" * 32
        plan = {"targets": {"voice_001.wav": {"row_idx": 0}}}
        payload = {"rows": [{"row_idx": 0, "row": {"audio": {"src": "https://huggingface.co/voice.wav"}}}]}
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td)
            (destination / "voice_001.wav.part").write_bytes(b"interrupted download")
            with patch("app.huggingface_audio._json_get", return_value=payload), \
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
            with patch("app.huggingface_audio._json_get", return_value=payload), \
                 patch("app.huggingface_audio.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(b"invalid response")
                result = download_resolved_audio(plan, destination)
            self.assertEqual(result["completed"], [])
            self.assertEqual(len(result["failed"]), 1)
            self.assertEqual((destination / "voice_001.wav").read_bytes(), b"corrupt")

    def test_audio_type_cannot_silently_mismatch_index_filename(self) -> None:
        plan = {"targets": {"voice_001.wav": {"row_idx": 0}}}
        payload = {"rows": [{"row_idx": 0, "row": {"audio": {"src": "https://huggingface.co/voice.wav"}}}]}
        with tempfile.TemporaryDirectory() as td:
            with patch("app.huggingface_audio._json_get", return_value=payload), \
                 patch("app.huggingface_audio.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(b"fLaC" + b"1234567890123456")
                result = download_resolved_audio(plan, Path(td))
            self.assertEqual(result["completed"], [])
            self.assertIn("indexed member suffix", result["failed"][0]["reason"])
            self.assertFalse((Path(td) / "voice_001.flac").exists())

    def test_audio_bytes_are_bounded_and_private_urls_refused(self) -> None:
        plan = {"targets": {"voice_001.wav": {"row_idx": 0}}}
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td)
            payload = {"rows": [{"row_idx": 0, "row": {"audio": {"src": "https://huggingface.co/voice.wav"}}}]}
            with patch("app.huggingface_audio._json_get", return_value=payload), \
                 patch("app.huggingface_audio.MAX_AUDIO_DOWNLOAD_BYTES", 16), \
                 patch("app.huggingface_audio.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(b"RIFF" + b"\0" * 8 + b"too-big" * 8)
                bounded = download_resolved_audio(plan, destination)
            self.assertEqual(bounded["completed"], [])
            self.assertIn("128 MiB budget", bounded["failed"][0]["reason"])
            self.assertFalse(list(destination.glob(".hsr-audio-*.part")))
            payload["rows"][0]["row"]["audio"]["src"] = "https://127.0.0.1/private"
            with patch("app.huggingface_audio._json_get", return_value=payload):
                blocked = download_resolved_audio(plan, destination)
            self.assertEqual(blocked["completed"], [])
            self.assertIn("Private", blocked["failed"][0]["reason"])


    def test_japanese_augmentation_uses_japanese_wav_not_matching_english(self) -> None:
        records = {
            "English/en.wav": {
                "filename": "en", "inGameFilename": "English/voice/chapter5_1_hero_101.wem",
                "transcription": "Hello",
            },
            "Japanese/ja.wav": {
                "filename": "ja", "inGameFilename": "Japanese/voice/chapter5_1_hero_101.wem",
                "transcription": "こんにちは",
            },
            "Chinese(PRC)/zh.wav": {
                "filename": "zh", "inGameFilename": "Chinese(PRC)/voice/chapter5_1_hero_101.wem",
                "transcription": "你好",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
            result = resolve_targets(
                path, [{"filename": "chapter5_1_hero_101.wav", "english": "こんにちは"}],
                source_language="ja",
            )
        selected = result["targets"]["chapter5_1_hero_101.wav"]
        self.assertEqual(result["audio_language"], "ja")
        self.assertEqual(selected["key"], "Japanese/ja.wav")
        self.assertEqual(selected["audio_language"], "ja")
        self.assertEqual(result["reference_targets"]["chapter5_1_hero_101.wav"]["key"], "Chinese(PRC)/zh.wav")

    def test_japanese_augmentation_never_falls_back_to_english(self) -> None:
        records = {
            "English/en.wav": {
                "filename": "en", "inGameFilename": "English/voice/chapter5_1_hero_101.wem",
                "transcription": "Hello",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
            result = resolve_targets(
                path, [{"filename": "chapter5_1_hero_101.wav", "english": "Hello"}],
                source_language="ja",
            )
        self.assertEqual(result["targets"], {})
        self.assertEqual(result["unresolved"], ["chapter5_1_hero_101.wav"])

    def test_chinese_and_korean_are_primary_audio_locales(self) -> None:
        records = {
            "Chinese(PRC)/zh.wav": {
                "filename": "zh", "inGameFilename": "Chinese(PRC)/voice/line_1.wem",
                "transcription": "你好",
            },
            "Korean/ko.wav": {
                "filename": "ko", "inGameFilename": "Korean/voice/line_1.wem",
                "transcription": "안녕하세요",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
            for language, key in (("zh-CN", "Chinese(PRC)/zh.wav"), ("ko", "Korean/ko.wav")):
                result = resolve_targets(
                    path, [{"filename": "line_1.wav"}], source_language=language,
                )
                self.assertEqual(result["targets"]["line_1.wav"]["key"], key)
            self.assertEqual(resolve_targets(
                path, [{"filename": "line_1.wav"}], source_language="zh-CN",
            )["reference_targets"], {})

    def test_download_plan_rejects_mixed_language_rows(self) -> None:
        plan = {
            "audio_language": "ja",
            "targets": {"line.wav": {"audio_language": "en", "row_idx": 0}},
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Audio locale mismatch"):
                download_resolved_audio(plan, Path(directory))



    def test_dataset_row_rechecked_before_downloading_japanese_audio(self) -> None:
        plan = {
            "audio_language": "ja",
            "targets": {"line.wav": {
                "audio_language": "ja", "row_idx": 0,
                "ingame_filename": "Japanese/voice/line.wem",
            }},
        }
        wrong_row = {"rows": [{"row_idx": 0, "row": {
            "language": "English",
            "ingame_filename": "English/voice/line.wem",
            "audio": {"src": "https://huggingface.co/english.wav"},
        }}]}
        with tempfile.TemporaryDirectory() as td:
            with patch("app.huggingface_audio._json_get", return_value=wrong_row), \
                 patch("app.huggingface_audio.urllib.request.build_opener") as opener:
                result = download_resolved_audio(plan, Path(td))
            self.assertEqual(result["completed"], [])
            self.assertIn("locale mismatch", result["failed"][0]["reason"])
            opener.return_value.open.assert_not_called()

    def test_dataset_row_identity_change_fails_even_with_japanese_locale(self) -> None:
        plan = {
            "audio_language": "ja",
            "targets": {"line.wav": {
                "audio_language": "ja", "row_idx": 0,
                "ingame_filename": "Japanese/voice/line.wem",
            }},
        }
        moved_row = {"rows": [{"row_idx": 0, "row": {
            "language": "Japanese",
            "ingame_filename": "Japanese/voice/someone_else.wem",
            "audio": {"src": "https://huggingface.co/japanese.wav"},
        }}]}
        with tempfile.TemporaryDirectory() as td:
            with patch("app.huggingface_audio._json_get", return_value=moved_row):
                result = download_resolved_audio(plan, Path(td))
            self.assertEqual(result["completed"], [])
            self.assertIn("identity changed", result["failed"][0]["reason"])

    def test_stale_japanese_cache_is_not_reused_and_is_not_stage_input(self) -> None:
        plan = {
            "audio_language": "ja",
            "targets": {"line.wav": {
                "audio_language": "ja", "row_idx": 0,
                "ingame_filename": "Japanese/voice/line.wem",
            }},
        }
        wav = b"RIFF" + (16).to_bytes(4, "little") + b"WAVEfmt " + b"\0" * 12
        valid_row = {"rows": [{"row_idx": 0, "row": {
            "language": "Japanese",
            "ingame_filename": "Japanese/voice/line.wem",
            "audio": {"src": "https://huggingface.co/japanese.wav"},
        }}]}
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "line.wav").write_bytes(b"RIFF" + b"\0" * 4 + b"WAVE" + b"old bytes")
            (folder / "stale.wav").write_bytes(wav)
            with patch("app.huggingface_audio._json_get", return_value=valid_row), \
                 patch("app.huggingface_audio.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(wav)
                result = download_resolved_audio(plan, folder)
            self.assertEqual(result["failed"], [])
            self.assertEqual(result["completed"][0]["status"], "ok")
            self.assertEqual(result["completed"][0]["sha256"], __import__("hashlib").sha256(wav).hexdigest())
            self.assertEqual([p.name for p in validated_incremental_files(result, folder)], ["line.wav"])
            self.assertTrue((folder / "stale.wav").is_file())
            (folder / "line.wav").write_bytes(wav + b"changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                validated_incremental_files(result, folder)


if __name__ == "__main__":
    unittest.main()
