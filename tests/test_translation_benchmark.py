from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.translation_benchmark import (
    classify_sample,
    discover_index,
    run_benchmark,
    select_sample,
)


def write_index(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["filename", "group", "english"],
        )
        writer.writeheader()
        writer.writerows(rows)


class TranslationBenchmarkTests(unittest.TestCase):
    def test_categories_cover_failure_oriented_cases(self) -> None:
        self.assertEqual(classify_sample("<color=#fff>Hello</color>"), "tags_placeholders")
        self.assertEqual(classify_sample("The Aeon has never answered us."), "hsr_terminology")
        self.assertEqual(classify_sample("Short line."), "short_context_sensitive")
        self.assertEqual(classify_sample("Really? You don't believe me!"), "short_context_sensitive")
        self.assertEqual(
            classify_sample("This sentence is deliberately long " * 8),
            "long_complex",
        )

    def test_sample_selection_is_deterministic_and_unique(self) -> None:
        rows = []
        for i in range(30):
            rows.append(
                {
                    "filename": f"line_{i}.wav",
                    "group": "g",
                    "english": (
                        "The Aeon speaks." if i % 5 == 0
                        else ("Really? This can't be right!" if i % 5 == 1
                        else ("x" * 160 if i % 5 == 2 else f"Plain sample number {i}."))
                    ),
                }
            )
        first = select_sample(rows, 20, seed="same")
        second = select_sample(rows, 20, seed="same")
        self.assertEqual(
            [x["filename"] for x in first],
            [x["filename"] for x in second],
        )
        self.assertEqual(len({x["filename"] for x in first}), 20)
        categories = {x["benchmark_category"] for x in first}
        self.assertIn("hsr_terminology", categories)
        self.assertIn("long_complex", categories)

    def test_discover_index_prefers_expected_hsr_filename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = root / "绯英_379条_完整索引.csv"
            other = root / "voice_index.csv"
            rows = [
                {"filename": "a.wav", "group": "g", "english": "Hello."},
                {"filename": "b.wav", "group": "g", "english": "World."},
            ]
            write_index(expected, rows)
            write_index(other, rows)

            found, inspected = discover_index([root])

            self.assertEqual(found, expected.resolve())
            self.assertEqual(sum(bool(x.get("valid")) for x in inspected), 2)

    def test_discover_index_skips_invalid_named_csv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = root / "379_index.csv"
            good = root / "绯英_完整索引.csv"
            bad.write_text("not,a,voice,index\n1,2,3,4\n", encoding="utf-8")
            write_index(
                good,
                [{"filename": "a.wav", "group": "g", "english": "Hello."}],
            )

            found, inspected = discover_index([root])

            self.assertEqual(found, good.resolve())
            invalid = [x for x in inspected if not x.get("valid")]
            self.assertTrue(invalid)

    def test_discover_index_refuses_equal_score_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [{"filename": "a.wav", "group": "g", "english": "Hello."}]
            write_index(root / "voice_index_a.csv", rows)
            write_index(root / "voice_index_b.csv", rows)

            with self.assertRaisesRegex(RuntimeError, "Multiple equally likely"):
                discover_index([root])

    def test_dry_run_writes_sample_without_api(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "index.csv"
            out = root / "benchmark"
            write_index(
                index,
                [
                    {"filename": "a.wav", "group": "archive", "english": "The story's not finished."},
                    {"filename": "b.wav", "group": "archive", "english": "The Aeon has never answered us."},
                ],
            )
            with patch(
                "app.translation_benchmark.credentials_status",
                return_value={
                    "provider": "vapi",
                    "base_url": "https://api.gpt.ge/v1",
                    "configured": True,
                    "source": "test",
                },
            ):
                report = run_benchmark(
                    index,
                    out,
                    sample_size=2,
                    dry_run=True,
                    model="gpt-5.6-sol",
                )

            self.assertTrue(report["dry_run"])
            self.assertTrue(report["integrity"]["complete"])
            self.assertTrue((out / "benchmark_results.csv").is_file())
            self.assertTrue((out / "benchmark_report.json").is_file())
            text = (out / "benchmark_results.csv").read_text(encoding="utf-8-sig")
            self.assertIn("semantic_fidelity", text)
            self.assertNotIn("官方中文", text)

    def test_live_path_validates_and_writes_all_mocked_translations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "index.csv"
            out = root / "benchmark"
            write_index(
                index,
                [
                    {"filename": f"{i}.wav", "group": "g", "english": f"English line {i}."}
                    for i in range(5)
                ],
            )

            def fake_translate(records, model, client):
                return [
                    {"id": row["id"], "chinese": f"译文：{row['english']}"}
                    for row in records
                ]

            with (
                patch(
                    "app.translation_benchmark.credentials_status",
                    return_value={
                        "provider": "vapi",
                        "base_url": "https://api.gpt.ge/v1",
                        "configured": True,
                        "source": "test",
                    },
                ),
                patch("app.translation_benchmark.make_client", return_value=object()),
                patch("app.translation_benchmark.translate_records", side_effect=fake_translate),
            ):
                report = run_benchmark(
                    index,
                    out,
                    sample_size=5,
                    batch_size=5,
                    model="gpt-5.6-sol",
                )

            self.assertEqual(report["integrity"]["expected_ids"], 5)
            self.assertEqual(report["integrity"]["returned_ids"], 5)
            self.assertTrue(report["integrity"]["complete"])
            text = (out / "benchmark_results.csv").read_text(encoding="utf-8-sig")
            self.assertIn("译文：English line", text)
            self.assertEqual(len(report["batch_timings"]), 1)


if __name__ == "__main__":
    unittest.main()
