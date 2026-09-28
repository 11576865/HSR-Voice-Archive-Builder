from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.game_profiles import game_audio_dataset, game_index_kind, game_index_url
from app.provider_index import (
    GENSHIN_INDEX_LOCAL_FILE_ENV,
    fetch_provider_index,
    fetch_provider_index_for_filenames_cached,
    provider_index_label,
    read_genshin_json_for_character,
    read_genshin_json_for_filenames,
)


def write_genshin_index(path: Path) -> None:
    payload = {
        "0123456789abcdef": {
            "sourceFileName": "English(US)\\VO_anecdote\\vo_anecdote_106701_hutao_01.wem",
            "voiceContent": "One client helped me out, so I bought some perfume.",
            "talkName": "Hu Tao",
            "avatarName": "Hutao",
        },
        "1111111111111111": {
            "sourceFileName": "English(US)\\VO_anecdote\\vo_anecdote_106701_hutao_02.wem",
            "voiceContent": "Boiled fish with perfume?",
            "talkName": "Hu Tao",
            "avatarName": "Hutao",
        },
        "2222222222222222": {
            "sourceFileName": "English(US)\\VO_friendship\\vo_xingqiu_dialog_greetingNight.wem",
            "voiceContent": "What say you we snatch a few fireflies?",
            "talkName": "Xingqiu",
            "avatarName": "Xingqiu",
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class ProviderIndexTests(unittest.TestCase):
    def test_genshin_provider_configuration_is_concrete(self) -> None:
        self.assertEqual(game_index_kind("genshin-impact"), "json")
        self.assertIn("Genshin_Voice_Sorting_Scripts", game_index_url("genshin-impact", "en"))
        self.assertEqual(game_audio_dataset("genshin-impact"), "simon3000/genshin-voice")

    def test_genshin_json_maps_source_filename_to_wav_basename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "EN.json"
            write_genshin_index(path)
            rows = read_genshin_json_for_filenames(
                path,
                {
                    "vo_anecdote_106701_hutao_01.wav",
                    "vo_anecdote_106701_hutao_02.wav",
                },
            )
        self.assertEqual([row["filename"] for row in rows], [
            "vo_anecdote_106701_hutao_01.wav",
            "vo_anecdote_106701_hutao_02.wav",
        ])
        self.assertEqual(rows[0]["character"], "Hu Tao")
        self.assertEqual(rows[0]["english"], "One client helped me out, so I bought some perfume.")
        self.assertEqual(rows[0]["hash"], "0123456789abcdef")

    def test_genshin_character_filter_matches_filename_and_character_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "EN.json"
            write_genshin_index(path)
            rows = read_genshin_json_for_character(path, "hutao")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("hutao" in row["filename"] for row in rows))

    def test_local_genshin_index_override_skips_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "EN.json"
            write_genshin_index(path)
            with patch.dict(os.environ, {GENSHIN_INDEX_LOCAL_FILE_ENV: str(path)}), patch(
                "app.provider_index._download_json_index",
                side_effect=AssertionError("network should not be used"),
            ):
                rows, meta = fetch_provider_index_for_filenames_cached(
                    "genshin-impact",
                    {"vo_anecdote_106701_hutao_01.wav"},
                    language="en",
                    cache_dir=root / "cache",
                )
                character_rows = fetch_provider_index(
                    "genshin-impact",
                    "hutao",
                    language="en",
                    cache_dir=root / "cache",
                )
        self.assertEqual(len(rows), 1)
        self.assertTrue(meta.get("local_file"))
        self.assertEqual(len(character_rows), 2)

    def test_provider_label_names_game_and_index(self) -> None:
        label = provider_index_label(
            "genshin-impact",
            "https://example.invalid/Indexs/all/EN.json",
        )
        self.assertIn("Genshin Impact", label)
        self.assertIn("EN.json", label)


if __name__ == "__main__":
    unittest.main()
