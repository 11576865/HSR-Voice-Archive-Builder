from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.builder import extract_major_group
from app.identity import classify_major_group, infer_group
from app.quick import _chapter_classification_summary


class ChapterClassificationTests(unittest.TestCase):
    def test_known_hsr_prefixes_are_classified_without_fake_vo_or_ev_groups(self) -> None:
        chapter = classify_major_group("vo_chapter5_13_evanescia_104.wav")
        self.assertEqual(chapter.group, "chapter5_13")
        self.assertEqual(chapter.major_group, "chapter5")
        self.assertEqual(chapter.confidence, "confirmed")

        archive = classify_major_group("Ev_archive_vo_avatar_cast_hero_98765.wav")
        self.assertEqual(archive.group, "archive")
        self.assertEqual(archive.major_group, "archive")
        self.assertEqual(archive.confidence, "confirmed")

    def test_opaque_numeric_voice_name_is_unknown_for_chapter_sorting(self) -> None:
        classification = classify_major_group("vo_100101_01.wav", "vo")
        self.assertEqual(classification.confidence, "unknown")
        self.assertEqual(classification.major_group, "")
        self.assertEqual(infer_group("vo_100101_01"), "vo")

    def test_package_path_can_infer_group_when_basename_is_opaque(self) -> None:
        classification = classify_major_group(
            "chapter3_7/vo_100101_01.wav",
            "vo",
        )
        self.assertEqual(classification.group, "chapter3_7")
        self.assertEqual(classification.major_group, "chapter3")
        self.assertEqual(classification.confidence, "inferred")
        self.assertEqual(classification.method, "package_path")

    def test_explicit_custom_index_group_is_inferred(self) -> None:
        classification = classify_major_group("mystery_001.wav", "event_special")
        self.assertEqual(classification.major_group, "event_special")
        self.assertEqual(classification.confidence, "inferred")
        self.assertEqual(classification.method, "explicit_index_group")

    def test_genshin_anecdote_schema_is_confirmed(self) -> None:
        classification = classify_major_group("vo_anecdote_106701_hutao_01.wav")
        self.assertEqual(classification.group, "anecdote_106701")
        self.assertEqual(classification.major_group, "anecdote_106701")
        self.assertEqual(classification.confidence, "confirmed")

    def test_builder_uses_confidence_aware_major_group(self) -> None:
        chapter = SimpleNamespace(
            group="vo",
            source_member_id="vo_chapter5_13_evanescia_104.wav",
            filename="vo_chapter5_13_evanescia_104.wav",
        )
        unknown = SimpleNamespace(
            group="vo",
            source_member_id="vo_100101_01.wav",
            filename="vo_100101_01.wav",
        )
        self.assertEqual(extract_major_group(chapter), "chapter5")
        self.assertIsNone(extract_major_group(unknown))

    def test_preflight_summary_reports_confirmed_inferred_and_unknown(self) -> None:
        summary = _chapter_classification_summary(
            [
                "vo_chapter5_13_evanescia_104.wav",
                "chapter3_7/vo_100101_01.wav",
                "vo_999999_01.wav",
            ],
            [
                {
                    "filename": "vo_chapter5_13_evanescia_104.wav",
                    "group": "chapter5_13",
                },
                {
                    "filename": "vo_100101_01.wav",
                    "group": "vo",
                },
                {
                    "filename": "vo_999999_01.wav",
                    "group": "vo",
                },
            ],
        )
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["inferred"], 1)
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["classified"], 2)
        self.assertEqual(summary["coverage"], 0.6667)
        groups = {row["group_id"]: row["count"] for row in summary["major_groups"]}
        self.assertEqual(groups["chapter5"], 1)
        self.assertEqual(groups["chapter3"], 1)
        self.assertEqual(summary["unknown_examples"], ["vo_999999_01.wav"])


if __name__ == "__main__":
    unittest.main()
