"""Locale safety tests for remote audio update check/apply."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.project import ProjectConfig
from app.remote_update_language import (
    require_incremental_audio_language, select_incremental_index_url,
    require_matching_update_plan, require_safe_prior_incremental_apply,
)


def config(audio: str = "ja", text: str = "ja", url: str = "") -> ProjectConfig:
    return ProjectConfig(
        schema_version=1,
        name="Japanese voice archive",
        root="/tmp/voice-project",
        index_csv="index.csv",
        wav_source="original-jp.7z",
        game_id="honkai-star-rail",
        audio_language=audio,
        source_text_language=text,
        remote_index_url=url,
    )


class RemoteUpdateLanguageTests(unittest.TestCase):
    def test_japanese_audio_chooses_japanese_index_not_saved_english_default(self) -> None:
        saved = "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/EN.xlsx"
        self.assertEqual(require_incremental_audio_language(config(url=saved)), "ja")
        self.assertTrue(select_incremental_index_url(config(url=saved)).endswith("/JP.xlsx"))
        self.assertTrue(select_incremental_index_url(config(url=saved), requested_url=saved).endswith("/JP.xlsx"))

    def test_automatic_or_mismatched_language_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "auto"):
            require_incremental_audio_language(config(audio="auto"))
        with self.assertRaisesRegex(ValueError, "一致"):
            require_incremental_audio_language(config(audio="ja", text="en"))

    def test_old_or_wrong_language_plan_requires_new_remote_check(self) -> None:
        for plan in ({}, {"audio_language": "en"}):
            with self.subTest(plan=plan), self.assertRaisesRegex(ValueError, "重新检查"):
                require_matching_update_plan(plan, "ja")
        require_matching_update_plan({"audio_language": "ja"}, "ja")

    def test_legacy_applied_english_audio_blocks_new_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            receipt = Path(td) / "update_apply_report.json"
            receipt.write_text(json.dumps({
                "project_updated": True, "applied_filenames": ["line.wav"],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "历史增补"):
                require_safe_prior_incremental_apply(receipt, "ja")
            receipt.write_text(json.dumps({
                "project_updated": True, "audio_language": "en",
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "历史增补"):
                require_safe_prior_incremental_apply(receipt, "ja")
            receipt.write_text(json.dumps({
                "project_updated": True, "audio_language": "ja",
            }), encoding="utf-8")
            require_safe_prior_incremental_apply(receipt, "ja")


if __name__ == "__main__":
    unittest.main()
