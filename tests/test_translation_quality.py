from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.pipeline import _target_records, _translate_missing
from app.translation_quality import (
    formatting_signatures,
    has_hard_issue,
    translation_qa,
)


class TranslationQualityTests(unittest.TestCase):
    def test_formatting_signatures_preserve_structure_not_payload_text(self) -> None:
        source = "<color=#fff>{M#Sir}{F#Madam}{NICKNAME}</color>"
        target = "<color=#fff>{M#先生}{F#女士}{NICKNAME}</color>"
        self.assertEqual(formatting_signatures(source), formatting_signatures(target))

    def test_qa_detects_missing_glossary_and_tag(self) -> None:
        issues = translation_qa(
            "<color=#fff>Evanescia</color>",
            "Evanescia",
            {"Evanescia": "绯英"},
        )
        codes = {x["code"] for x in issues}
        self.assertIn("format_tokens", codes)
        self.assertIn("terminology", codes)
        self.assertTrue(has_hard_issue(issues))

    def test_target_records_include_neighbor_context(self) -> None:
        entries = [
            SimpleNamespace(
                filename="a.wav", english="Before.", chinese="已有",
                group="scene-1", source_detail="archive-1",
            ),
            SimpleNamespace(
                filename="b.wav", english="Target?", chinese="",
                group="scene-1", source_detail="archive-1",
            ),
            SimpleNamespace(
                filename="c.wav", english="After.", chinese="已有",
                group="scene-1", source_detail="archive-1",
            ),
        ]
        rows = _target_records(entries)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["context_before"], "Before.")
        self.assertEqual(rows[0]["context_after"], "After.")

    def test_translation_qa_retries_only_problematic_row(self) -> None:
        entries = [
            SimpleNamespace(
                filename="a.wav",
                english="Evanescia is here.",
                chinese="",
                chinese_source="missing",
            )
        ]
        calls = []

        def fake_translate(records, model, glossary, client):
            calls.append(records)
            if len(calls) == 1:
                return [{"id": "a.wav", "chinese": "Evanescia来了。"}]
            self.assertIn("previous_chinese", records[0])
            self.assertIn("qa_issues", records[0])
            return [{"id": "a.wav", "chinese": "绯英来了。"}]

        with tempfile.TemporaryDirectory() as td:
            checkpoint = Path(td) / ".translation_checkpoint.json"
            with (
                patch(
                    "app.credentials.translation_identity",
                    return_value=("vapi", "https://api.gpt.ge/v1"),
                ),
                patch("app.translator.make_client", return_value=object()),
                patch("app.translator.translate_records", side_effect=fake_translate),
            ):
                report = _translate_missing(
                    entries,
                    "gpt-5.6-sol",
                    80,
                    checkpoint,
                )

            self.assertEqual(len(calls), 2)
            self.assertEqual(entries[0].chinese, "绯英来了。")
            self.assertEqual(report["count_translation_qa_retried"], 1)
            self.assertEqual(report["count_translation_qa_hard_failed"], 0)
            self.assertEqual(report["count_translation_qa_api_retries"], 1)
            self.assertTrue((Path(td) / "translation_qa.json").is_file())

    def test_translation_qa_blocks_persistent_hard_failure(self) -> None:
        entries = [
            SimpleNamespace(
                filename="a.wav",
                english="<color=#fff>Hello.</color>",
                chinese="",
                chinese_source="missing",
            )
        ]

        def bad_translate(records, model, glossary, client):
            return [{"id": "a.wav", "chinese": "你好。"}]

        with tempfile.TemporaryDirectory() as td:
            checkpoint = Path(td) / ".translation_checkpoint.json"
            with (
                patch(
                    "app.credentials.translation_identity",
                    return_value=("vapi", "https://api.gpt.ge/v1"),
                ),
                patch("app.translator.make_client", return_value=object()),
                patch("app.translator.translate_records", side_effect=bad_translate),
            ):
                with self.assertRaisesRegex(RuntimeError, "hard failure"):
                    _translate_missing(
                        entries,
                        "gpt-5.6-sol",
                        80,
                        checkpoint,
                    )

            qa_path = Path(td) / "translation_qa.json"
            self.assertTrue(qa_path.is_file())
            payload = json.loads(qa_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["count_translation_qa_hard_failed"], 1)


if __name__ == "__main__":
    unittest.main()
