from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.quick import create_quick_project, source_inventory
from app.remote_index import fetch_ai_hobbyist_index_cached


def make_voice_zip(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, b"wav")


class V09RemoteFallbackTests(unittest.TestCase):
    def test_remote_cache_reuses_fresh_character_slice(self) -> None:
        records = [{"filename": "a.wav", "english": "A", "character": "Test", "hash": ""}]
        with tempfile.TemporaryDirectory() as td, patch(
            "app.remote_index.fetch_ai_hobbyist_index",
            return_value=records,
        ) as fetch:
            cache_dir = Path(td)
            first, first_meta = fetch_ai_hobbyist_index_cached(
                "Test", cache_dir=cache_dir
            )
            second, second_meta = fetch_ai_hobbyist_index_cached(
                "test", cache_dir=cache_dir
            )

        self.assertEqual(first, records)
        self.assertEqual(second, records)
        self.assertFalse(first_meta["cache_hit"])
        self.assertTrue(second_meta["cache_hit"])
        self.assertFalse(second_meta["stale"])
        self.assertEqual(fetch.call_count, 1)

    def test_remote_cache_can_fall_back_after_refresh_failure(self) -> None:
        records = [{"filename": "a.wav", "english": "A", "character": "Test", "hash": ""}]
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            with patch("app.remote_index.fetch_ai_hobbyist_index", return_value=records):
                fetch_ai_hobbyist_index_cached("Test", cache_dir=cache_dir)
            with patch(
                "app.remote_index.fetch_ai_hobbyist_index",
                side_effect=OSError("offline"),
            ):
                cached, meta = fetch_ai_hobbyist_index_cached(
                    "Test", cache_dir=cache_dir, max_age_seconds=-1
                )

        self.assertEqual(cached, records)
        self.assertTrue(meta["cache_hit"])
        self.assertTrue(meta["stale"])

    def test_source_fingerprint_changes_when_archive_bytes_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "English.zip"
            make_voice_zip(archive, ["chapter1_test_1.wav"])
            first = source_inventory(archive)["fingerprint"]["digest"]
            make_voice_zip(archive, ["chapter1_test_1.wav", "chapter1_test_2.wav"])
            second = source_inventory(archive)["fingerprint"]["digest"]
        self.assertNotEqual(first, second)

    def test_project_creation_rejects_remote_records_changed_after_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "English.zip"
            names = ["chapter1_evanescia_1.wav", "chapter1_evanescia_2.wav"]
            make_voice_zip(archive, names)
            first = [
                {"filename": names[0], "english": "One", "character": "Evanescia", "hash": ""},
                {"filename": names[1], "english": "Two", "character": "Evanescia", "hash": ""},
            ]
            changed = [dict(row) for row in first]
            changed[1]["english"] = "Changed"
            with patch(
                "app.quick.fetch_ai_hobbyist_index_for_filenames_cached",
                side_effect=[
                    (first, {"cache_hit": False, "stale": False}),
                    (changed, {"cache_hit": True, "stale": False}),
                ],
            ), patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "vapi", "base_url": "https://api.gpt.ge/v1",
                    "configured": False, "source": "test",
                },
            ):
                with self.assertRaisesRegex(RuntimeError, "Remote index changed"):
                    create_quick_project(archive, root=root / "project")


if __name__ == "__main__":
    unittest.main()
