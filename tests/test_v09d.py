from __future__ import annotations

import csv
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.glossary import (
    CORE_GLOSSARY,
    glossary_fingerprint,
    load_glossary_overlay,
    merge_glossary,
)
from app.pipeline import _target_records, _translate_missing, build_project_v02
from app.semantic_quality import semantic_risk_tags
from app.translator import (
    HTTPResponse,
    OpenAIResponsesHTTPClient,
    verify_semantic_records,
)


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
        writer.writerow(
            {
                "index": "1",
                "group": "scene",
                "filename": "a.wav",
                "english": english,
                "sha256": "",
            }
        )


class ScriptedSemanticClient(OpenAIResponsesHTTPClient):
    def __init__(self) -> None:
        super().__init__(
            "test-key",
            base_url="https://example.invalid/v1",
            provider="custom",
            sleeper=lambda _: None,
            max_retries=0,
        )
        self.translation_calls = 0
        self.audit_calls = 0

    @staticmethod
    def _usage() -> dict[str, object]:
        return {
            "status": "completed",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 8,
                "total_tokens": 28,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }

    def create(self, **payload):
        schema_name = payload["text"]["format"]["name"]
        requested = json.loads(payload["input"].split("\n\nInput JSON:\n", 1)[1])

        if schema_name == "voice_translation_batch":
            self.translation_calls += 1
            translations = []
            for row in requested:
                if row["id"] == "smoke-1":
                    chinese = "故事还没结束。"
                elif "previous_chinese" in row and "Semantic verifier:" in row.get("qa_issues", ""):
                    chinese = "如果你离开，我就不会去。"
                else:
                    # Deliberately preserve structure while dropping the source negation.
                    # Deterministic QA should let this through so semantic QA can catch it.
                    chinese = "如果你离开，我会去。"
                translations.append({"id": row["id"], "chinese": chinese})
            return HTTPResponse(
                output_text=json.dumps({"translations": translations}, ensure_ascii=False),
                raw=self._usage(),
            )

        if schema_name == "voice_translation_semantic_audit":
            self.audit_calls += 1
            ok = self.audit_calls >= 2
            verdicts = [
                {
                    "id": row["id"],
                    "ok": ok,
                    "issues": [] if ok else ["negation"],
                    "note": "meaning preserved" if ok else "negation polarity changed",
                }
                for row in requested
            ]
            return HTTPResponse(
                output_text=json.dumps({"verdicts": verdicts}, ensure_ascii=False),
                raw=self._usage(),
            )

        raise AssertionError(f"unexpected schema: {schema_name}")


class InvalidSemanticClient(ScriptedSemanticClient):
    def create(self, **payload):
        schema_name = payload["text"]["format"]["name"]
        if schema_name != "voice_translation_semantic_audit":
            return super().create(**payload)
        requested = json.loads(payload["input"].split("\n\nInput JSON:\n", 1)[1])
        verdicts = [
            {
                "id": row["id"],
                "ok": True,
                "issues": ["negation"],
                "note": "invalid combination",
            }
            for row in requested
        ]
        return HTTPResponse(
            output_text=json.dumps({"verdicts": verdicts}, ensure_ascii=False),
            raw=self._usage(),
        )


class V09DGlossaryAndSemanticQATests(unittest.TestCase):
    def test_glossary_overlay_csv_json_and_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "terms.csv"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["English", "Chinese"])
                writer.writeheader()
                writer.writerow({"English": "Evanescia", "Chinese": "自定义绯英"})
                writer.writerow({"English": "New Term", "Chinese": "新术语"})

            json_path = root / "terms.json"
            json_path.write_text(
                json.dumps(
                    [
                        {"source": "Alpha", "target": "阿尔法"},
                        {"english": "Beta", "chinese": "贝塔"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            csv_overlay = load_glossary_overlay(csv_path)
            json_overlay = load_glossary_overlay(json_path)
            merged = merge_glossary(CORE_GLOSSARY, csv_overlay)

            self.assertEqual(csv_overlay["New Term"], "新术语")
            self.assertEqual(json_overlay, {"Alpha": "阿尔法", "Beta": "贝塔"})
            self.assertEqual(merged["Evanescia"], "自定义绯英")
            self.assertEqual(merged["Stellar Jade"], CORE_GLOSSARY["Stellar Jade"])

    def test_glossary_rejects_conflicting_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.csv"
            path.write_text(
                "english,chinese\nTerm,甲\nTerm,乙\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "conflicting targets"):
                load_glossary_overlay(path)

    def test_glossary_fingerprint_is_order_independent(self) -> None:
        first = glossary_fingerprint({"B": "乙", "A": "甲"})
        second = glossary_fingerprint({"A": "甲", "B": "乙"})
        self.assertEqual(first, second)

    def test_context_is_included_only_with_structural_relation(self) -> None:
        related = [
            SimpleNamespace(
                filename="a.wav", english="Before.", chinese="已有",
                group="g1", source_detail="",
            ),
            SimpleNamespace(
                filename="b.wav", english="Target.", chinese="",
                group="g1", source_detail="",
            ),
            SimpleNamespace(
                filename="c.wav", english="After.", chinese="已有",
                group="g1", source_detail="",
            ),
        ]
        rows = _target_records(related)
        self.assertEqual(rows[0]["context_before"], "Before.")
        self.assertEqual(rows[0]["context_after"], "After.")

        unrelated = [
            SimpleNamespace(
                filename="a.wav", english="Wrong before.", chinese="已有",
                group="g1", source_detail="archive-a",
            ),
            SimpleNamespace(
                filename="b.wav", english="Target.", chinese="",
                group="g2", source_detail="archive-b",
            ),
            SimpleNamespace(
                filename="c.wav", english="Wrong after.", chinese="已有",
                group="g3", source_detail="archive-c",
            ),
        ]
        rows = _target_records(unrelated)
        self.assertNotIn("context_before", rows[0])
        self.assertNotIn("context_after", rows[0])

    def test_semantic_risk_tags_are_sparse_and_targeted(self) -> None:
        self.assertEqual(semantic_risk_tags("Hello there."), [])
        tags = set(semantic_risk_tags("If I don't give you two tickets, she will leave."))
        self.assertEqual(tags, {"negation", "quantity", "condition", "person"})

    def test_semantic_verifier_rejects_inconsistent_ok_payload(self) -> None:
        client = InvalidSemanticClient()
        with self.assertRaisesRegex(RuntimeError, "ok=true with issues"):
            verify_semantic_records(
                [
                    {
                        "id": "a.wav",
                        "english": "I will not go.",
                        "chinese": "我不会去。",
                        "risk_tags": ["negation"],
                    }
                ],
                model="test-model",
                client=client,
            )

    def test_semantic_qa_artifact_tamper_rebuilds_stage_without_paid_calls(self) -> None:
        client = ScriptedSemanticClient()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            wavs.mkdir()
            write_wav(wavs / "a.wav")
            index = root / "index.csv"
            write_index(index, "I will not go if you leave.")
            output = root / "output"

            with (
                patch(
                    "app.credentials.translation_identity",
                    return_value=("custom", "https://example.invalid/v1"),
                ),
                patch("app.translator.make_client", return_value=client),
                patch(
                    "app.translation_runtime.CAPABILITY_CACHE_FILE",
                    root / "capabilities.json",
                ),
            ):
                first = build_project_v02(
                    index,
                    wavs,
                    output,
                    make_flac=False,
                    translate_missing=True,
                    translation_model="test-model",
                )

            self.assertTrue((output / "semantic_qa.json").is_file())
            self.assertIn("translation_qa", first["stage_resume"]["rebuilt"])

            (output / "semantic_qa.json").write_text(
                "{\"tampered\": true}",
                encoding="utf-8",
            )

            with (
                patch(
                    "app.credentials.translation_identity",
                    return_value=("custom", "https://example.invalid/v1"),
                ),
                patch(
                    "app.translator.make_client",
                    side_effect=AssertionError(
                        "validated checkpoint reuse must avoid API client creation"
                    ),
                ),
            ):
                second = build_project_v02(
                    index,
                    wavs,
                    output,
                    make_flac=False,
                    translate_missing=True,
                    translation_model="test-model",
                )

            self.assertIn("translation", second["stage_resume"]["rebuilt"])
            self.assertIn("translation_qa", second["stage_resume"]["rebuilt"])
            restored = json.loads(
                (output / "semantic_qa.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                restored["summary"]["count_semantic_qa_checkpoint_reused"],
                1,
            )

    def test_semantic_failure_gets_one_targeted_repair_and_checkpoint_reuse(self) -> None:
        client = ScriptedSemanticClient()
        entry = SimpleNamespace(
            filename="a.wav",
            english="I will not go if you leave.",
            chinese="",
            chinese_source="missing",
            group="scene",
            source_detail="archive",
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            checkpoint = root / ".translation_checkpoint.json"
            with (
                patch(
                    "app.credentials.translation_identity",
                    return_value=("custom", "https://example.invalid/v1"),
                ),
                patch("app.translator.make_client", return_value=client),
                patch("app.translation_runtime.CAPABILITY_CACHE_FILE", root / "capabilities.json"),
            ):
                report = _translate_missing(
                    [entry],
                    "test-model",
                    20,
                    checkpoint,
                    translation_glossary={},
                )

            self.assertEqual(entry.chinese, "如果你离开，我就不会去。")
            self.assertEqual(report["count_semantic_qa_candidates"], 1)
            self.assertEqual(report["count_semantic_qa_failed"], 1)
            self.assertEqual(report["count_semantic_qa_repaired"], 1)
            self.assertEqual(report["count_semantic_qa_hard_failed"], 0)
            self.assertEqual(client.audit_calls, 2)
            self.assertTrue((root / "semantic_qa.json").is_file())

            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(saved["records"]["a.wav"]["semantic_qa_version"], 1)

            reused_entry = SimpleNamespace(
                filename="a.wav",
                english="I will not go if you leave.",
                chinese="",
                chinese_source="missing",
                group="scene",
                source_detail="archive",
            )
            with (
                patch(
                    "app.credentials.translation_identity",
                    return_value=("custom", "https://example.invalid/v1"),
                ),
                patch(
                    "app.translator.make_client",
                    side_effect=AssertionError("verified checkpoint must avoid API client creation"),
                ),
            ):
                reused_report = _translate_missing(
                    [reused_entry],
                    "test-model",
                    20,
                    checkpoint,
                    translation_glossary={},
                )

            self.assertEqual(reused_entry.chinese, "如果你离开，我就不会去。")
            self.assertEqual(reused_report["count_gpt_checkpoint_reused"], 1)
            self.assertEqual(reused_report["count_semantic_qa_checkpoint_reused"], 1)


if __name__ == "__main__":
    unittest.main()
