from __future__ import annotations

import csv
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.official_alignment import (
    ZONE_GREEN,
    ZONE_RED,
    ZONE_YELLOW,
    alignment_signals,
    alignment_zone,
    official_candidate,
)
from app.pipeline import _review_official_targets, build_project_v02
from app.timeline import render_srt
from app.translator import HTTPResponse, OpenAIResponsesHTTPClient


def write_wav(path: Path, frames: int = 80) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * frames)


def write_index(path: Path, english: str) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["index", "group", "filename", "english", "sha256"],
        )
        writer.writeheader()
        writer.writerow({
            "index": "1",
            "group": "scene",
            "filename": "a.wav",
            "english": english,
            "sha256": "",
        })


def official_entry(filename: str, english: str, chinese: str) -> SimpleNamespace:
    return SimpleNamespace(
        filename=filename,
        english=english,
        chinese=chinese,
        chinese_source="official_chs_lab",
        group="scene",
        source_detail="archive",
    )


class ScriptedReviewClient(OpenAIResponsesHTTPClient):
    def __init__(self) -> None:
        super().__init__(
            "test-key",
            base_url="https://example.invalid/v1",
            provider="custom",
            sleeper=lambda _: None,
            max_retries=0,
        )
        self.review_calls = 0
        self.reviewed_ids: list[str] = []

    def create(self, **payload):
        schema_name = payload["text"]["format"]["name"]
        if schema_name != "official_target_review":
            raise AssertionError(f"unexpected schema: {schema_name}")
        requested = json.loads(payload["input"].split("\n\nInput JSON:\n", 1)[1])
        self.review_calls += 1
        reviews = []
        for row in requested:
            self.reviewed_ids.append(row["id"])
            if row["zone"] == "red":
                reviews.append({
                    "id": row["id"],
                    "decision": "revise",
                    "reason": "numeric_mismatch",
                    "translation": "我找到了3把钥匙。",
                })
            else:
                reviews.append({
                    "id": row["id"],
                    "decision": "accept_official",
                    "reason": "acceptable_localization",
                    "translation": "",
                })
        return HTTPResponse(
            output_text=json.dumps({"reviews": reviews}, ensure_ascii=False),
            raw={
                "status": "completed",
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 8,
                    "total_tokens": 28,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )


class OfficialAlignmentGateTests(unittest.TestCase):
    def test_natural_localization_stays_green(self) -> None:
        signals = alignment_signals(
            "Well, I guess we should get going.",
            "走吧。",
        )
        self.assertEqual([item["code"] for item in signals], ["length_anomaly"])

    def test_reordered_line_without_deviation_needs_no_api(self) -> None:
        candidate = official_candidate(
            row_id="a.wav",
            source_text="Take this, and be careful out there.",
            official_text="路上小心，这个拿着。",
        )
        self.assertEqual(candidate["zone"], ZONE_GREEN)

    def test_number_and_negation_conflicts_are_red(self) -> None:
        numeric = alignment_signals("I found 3 keys.", "我找到了5把钥匙。")
        self.assertEqual(alignment_zone(numeric), ZONE_RED)
        self.assertEqual(numeric[0]["code"], "numeric_conflict")

        negation = alignment_signals("I will not go.", "我会去。")
        self.assertEqual(alignment_zone(negation), ZONE_RED)
        self.assertEqual(negation[0]["code"], "negation_dropped")

    def test_negation_added_by_localization_is_only_yellow(self) -> None:
        signals = alignment_signals("Stay away from the reactor.", "别靠近反应堆。")
        self.assertEqual(alignment_zone(signals), ZONE_YELLOW)
        self.assertEqual([item["code"] for item in signals], ["negation_added"])

    def test_cjk_numerals_count_as_the_same_number(self) -> None:
        self.assertEqual(alignment_signals("Only 2 left.", "只剩两个。"), [])

    def test_question_turned_into_statement_is_yellow(self) -> None:
        signals = alignment_signals("Are you coming?", "你来。")
        self.assertEqual(alignment_zone(signals), ZONE_YELLOW)
        self.assertEqual([item["code"] for item in signals], ["sentence_type_shift"])


class OfficialReviewPipelineTests(unittest.TestCase):
    def _entries(self) -> list[SimpleNamespace]:
        return [
            official_entry("green.wav", "Let's go.", "我们走吧。"),
            official_entry("red.wav", "I found 3 keys.", "我找到了5把钥匙。"),
            official_entry("yellow.wav", "Are you coming?", "你来。"),
        ]

    def test_only_deviating_rows_reach_the_api_and_revisions_apply(self) -> None:
        client = ScriptedReviewClient()
        entries = self._entries()
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            with (
                patch(
                    "app.credentials.translation_identity",
                    return_value=("custom", "https://example.invalid/v1"),
                ),
                patch("app.translator.make_client", return_value=client),
            ):
                report = _review_official_targets(
                    entries,
                    model="test-model",
                    batch_size=20,
                    state_dir=state,
                    translation_glossary={},
                )

            self.assertEqual(sorted(client.reviewed_ids), ["red.wav", "yellow.wav"])
            self.assertEqual(report["count_official_zone_green"], 1)
            self.assertEqual(report["count_official_zone_yellow"], 1)
            self.assertEqual(report["count_official_zone_red"], 1)
            self.assertEqual(report["count_official_revised"], 1)
            self.assertEqual(report["count_official_revision_rejected"], 0)
            self.assertEqual(entries[0].chinese, "我们走吧。")
            self.assertEqual(entries[1].chinese, "我找到了3把钥匙。")
            self.assertTrue(entries[1].chinese_source.startswith("official-review:"))
            self.assertEqual(entries[2].chinese, "你来。")
            self.assertEqual(entries[2].chinese_source, "official_chs_lab")

            saved = json.loads(
                (state / "official_review.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["summary"]["count_official_api_reviewed"], 2)

            fresh = self._entries()
            with (
                patch(
                    "app.credentials.translation_identity",
                    return_value=("custom", "https://example.invalid/v1"),
                ),
                patch(
                    "app.translator.make_client",
                    side_effect=AssertionError(
                        "reviewed rows must not trigger another API call"
                    ),
                ),
            ):
                reused = _review_official_targets(
                    fresh,
                    model="test-model",
                    batch_size=20,
                    state_dir=state,
                    translation_glossary={},
                )

            self.assertEqual(reused["count_official_checkpoint_reused"], 2)
            self.assertEqual(reused["count_official_api_reviewed"], 0)
            self.assertEqual(fresh[1].chinese, "我找到了3把钥匙。")

    def test_same_language_archives_skip_the_review_entirely(self) -> None:
        entries = self._entries()
        with tempfile.TemporaryDirectory() as td:
            report = _review_official_targets(
                entries,
                model="test-model",
                batch_size=20,
                state_dir=Path(td),
                source_language="zh-CN",
                target_language="zh-CN",
            )
        self.assertEqual(report["count_official_reviewed"], 0)
        self.assertEqual(entries[1].chinese, "我找到了5把钥匙。")


class OfficialReviewBuildTests(unittest.TestCase):
    def test_build_reports_review_counts_and_writes_srt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            wavs.mkdir()
            write_wav(wavs / "a.wav")
            index = root / "index.csv"
            write_index(index, "Let's go.")
            output = root / "output"

            with patch(
                "app.translator.make_client",
                side_effect=AssertionError("no official row needs an API call"),
            ):
                report = build_project_v02(
                    index,
                    wavs,
                    output,
                    make_flac=False,
                    review_official_target=True,
                    translation_model="test-model",
                )

            self.assertEqual(report["count_official_reviewed"], 0)
            self.assertTrue((output / "HSR_Voice_Archive.srt").is_file())


class SubtitleSrtTests(unittest.TestCase):
    def test_srt_pairs_source_and_target_on_timeline_positions(self) -> None:
        entries = [
            SimpleNamespace(
                english="Hello there.",
                chinese="你好。",
                start_seconds=5.0,
                display_end_seconds=6.5,
            ),
            SimpleNamespace(
                english="Goodbye.",
                chinese="再见。",
                start_seconds=6.9,
                display_end_seconds=7.25,
            ),
        ]
        self.assertEqual(
            render_srt(entries),
            "1\n00:00:05,000 --> 00:00:06,500\nHello there.\n你好。\n"
            "\n2\n00:00:06,900 --> 00:00:07,250\nGoodbye.\n再见。\n",
        )

    def test_chinese_primary_archive_renders_one_line(self) -> None:
        entries = [
            SimpleNamespace(
                english="你好。",
                chinese="你好。",
                start_seconds=0.0,
                display_end_seconds=1.0,
            )
        ]
        rendered = render_srt(entries, source_language="zh-CN", target_language="zh-CN")
        self.assertEqual(rendered, "1\n00:00:00,000 --> 00:00:01,000\n你好。\n")


if __name__ == "__main__":
    unittest.main()
