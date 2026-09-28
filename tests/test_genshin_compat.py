from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.game_profiles import detect_game_profile, genshin_voice_parts
from app.identity import infer_group
from app.quick import create_quick_project, infer_character, quick_scan


class GenshinCompatibilityTests(unittest.TestCase):
    def _make_hutao_zip(self, path: Path, count: int = 3, *, complete_labs: bool = True) -> list[str]:
        names = [f"vo_anecdote_106701_hutao_{i:02d}.wav" for i in range(1, count + 1)]
        with zipfile.ZipFile(path, "w") as archive:
            for i, name in enumerate(names, 1):
                archive.writestr(name, b"synthetic-wav")
                if complete_labs or i < count:
                    archive.writestr(Path(name).with_suffix(".lab").name, f"Hu Tao line {i}.")
        return names

    def test_genshin_filename_schema_extracts_identity(self) -> None:
        parts = genshin_voice_parts("vo_anecdote_106701_hutao_01.wav")
        self.assertIsNotNone(parts)
        assert parts is not None
        self.assertEqual(parts["family"], "anecdote")
        self.assertEqual(parts["section"], "106701")
        self.assertEqual(parts["character"], "hutao")
        self.assertEqual(parts["sequence"], "01")
        self.assertEqual(parts["group"], "anecdote_106701")

    def test_genshin_group_and_character_are_not_reduced_to_vo_or_anecdote(self) -> None:
        names = [
            "vo_anecdote_106701_hutao_01.wav",
            "vo_anecdote_106701_hutao_02.wav",
            "vo_anecdote_106701_hutao_03.wav",
        ]
        self.assertEqual(infer_group(Path(names[0]).stem), "anecdote_106701")
        character = infer_character(names)
        self.assertEqual(character["value"], "hutao")
        self.assertEqual(character["method"], "genshin_filename_schema")
        self.assertEqual(character["confidence"], "high")

    def test_detects_genshin_package_profile(self) -> None:
        profile = detect_game_profile([
            "vo_anecdote_106701_hutao_01.wav",
            "vo_anecdote_106701_hutao_02.wav",
        ])
        self.assertEqual(profile["game_id"], "genshin-impact")
        self.assertEqual(profile["label"], "Genshin Impact")
        self.assertTrue(profile["remote_updates_supported"])

    def test_quick_scan_uses_complete_genshin_labs_without_hsr_remote_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "Hutao.zip"
            self._make_hutao_zip(archive)
            with patch(
                "app.quick.fetch_provider_index_for_filenames_cached",
                side_effect=OSError("offline provider index"),
            ), patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "custom",
                    "base_url": "https://example.invalid/v1",
                    "configured": False,
                },
            ):
                plan = quick_scan(archive)

            self.assertTrue(plan["ready"], plan["blockers"])
            self.assertEqual(plan["game_profile"]["game_id"], "genshin-impact")
            self.assertEqual(plan["character"]["value"], "hutao")
            self.assertEqual(plan["index"]["source"], "primary-package-lab")
            self.assertEqual(plan["index"]["order_basis"], "package_member_path")
            self.assertIn("Genshin_Voice_Sorting_Scripts", plan["translation"]["remote_index_url"])

    def test_genshin_without_complete_labs_uses_genshin_remote_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "Hutao.zip"
            names = self._make_hutao_zip(archive, complete_labs=False)
            records = [
                {
                    "filename": name,
                    "english": f"Remote Hu Tao line {i}.",
                    "hash": f"{i:016x}",
                    "character": "Hu Tao",
                }
                for i, name in enumerate(names, 1)
            ]
            with patch(
                "app.quick.fetch_provider_index_for_filenames_cached",
                return_value=(records, {"cache_hit": False, "stale": False}),
            ), patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "custom",
                    "base_url": "https://example.invalid/v1",
                    "configured": False,
                },
            ):
                plan = quick_scan(archive)

            self.assertTrue(plan["ready"], plan["blockers"])
            self.assertEqual(plan["index"]["source"], "remote")
            self.assertEqual(plan["index"]["matched_wavs"], len(names))
            self.assertIn("Genshin_Voice_Sorting_Scripts", plan["index"]["url"])

    def test_created_genshin_project_persists_game_identity_and_groups(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "Hutao.zip"
            self._make_hutao_zip(archive)
            with patch(
                "app.quick.fetch_provider_index_for_filenames_cached",
                side_effect=OSError("offline provider index"),
            ), patch(
                "app.quick.credentials_status",
                return_value={
                    "provider": "custom",
                    "base_url": "https://example.invalid/v1",
                    "configured": False,
                },
            ):
                config, plan = create_quick_project(archive, root=root / "project")

            self.assertEqual(config.game_id, "genshin-impact")
            self.assertIn("Genshin_Voice_Sorting_Scripts", config.remote_index_url)
            index_path = Path(config.root) / config.index_csv
            rows = index_path.read_text(encoding="utf-8-sig")
            self.assertIn("anecdote_106701", rows)
            self.assertEqual(plan["character"]["value"], "hutao")


if __name__ == "__main__":
    unittest.main()
