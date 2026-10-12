"""Strict historical-ASS audit cost and provisional-pointer safety benchmark.

All data are synthetic; no FFmpeg is executed in this scale test.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from app.ass_coverage_batches import (
    ALGORITHM, COVERAGE_FILE, VERSION, _receipt_path,
    read_exported_ass_coverage,
)
from app.ass_artifact_evidence import _export_identity
from app.project import ProjectConfig
from app.subtitle_export_status import (
    ASS_NAME, SRT_NAME, record_subtitle_export,
    export_render_policy_sha256,
)
from app.subtitles import subtitle_render_config
from tests.test_export_ass_infrastructure import ASS_FIXTURE


class AssCoverageAuditScaleTests(unittest.TestCase):
    def test_256_batch_history_strict_verified_and_provisional_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = ProjectConfig(
                schema_version=1, name="synthetic-audit-scale", root=str(root),
                index_csv="index.csv", wav_source="wavs",
            )
            output = root / "output"
            output.mkdir()
            (output / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
            (output / SRT_NAME).write_text("subtitle\n", encoding="utf-8")
            head = ASS_FIXTURE.split("Dialogue:", 1)[0]
            entries = []
            batch_count = 256
            for i in range(batch_count):
                mins, sec = divmod(i, 60)
                entries.append(
                    f"Dialogue: 0,0:{mins:02d}:{sec:02d}.00,"
                    f"0:{mins:02d}:{sec:02d}.90,Default,,0,0,0,,line-{i}"
                )
            (output / ASS_NAME).write_text(
                head + "\n".join(entries) + "\n", encoding="utf-8"
            )
            record_subtitle_export(
                output, state="current", ass_required=True,
                render_policy_sha256=export_render_policy_sha256(
                    subtitle_render_config(config), "en", "zh-CN"
                ),
            )
            identity = _export_identity(output, config)
            run_id = "a" * 32
            area = {"x_min": 0, "x_max": 1920, "y_min": 0, "y_max": 1080}
            previous = ""
            most_recent = None
            oldest = None
            for i in range(batch_count):
                row = {
                    "window_index": i,
                    "timestamp_seconds": i + 0.3,
                    "status": "inside_safe_area",
                }
                item = {
                    "schema_version": VERSION, "sampling_algorithm": ALGORITHM,
                    "scope": "bounded_sequential_window_batch",
                    "full_interval_verified": False,
                    "run_id": run_id, "batch_id": i + 1,
                    "previous_batch_receipt_sha256": previous,
                    **identity, "safe_area": area,
                    "cursor": i, "next_cursor": i + 1, "window_count": 1,
                    "dynamic_examined_window_count": 0,
                    "dynamic_event_examined_window_count": 0,
                    "effect_candidates_truncated_windows": 0,
                    "violation_count": 0, "no_visible_ink_count": 0,
                    "samples": [row],
                }
                target = _receipt_path(output, run_id, i + 1)
                target.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(item, sort_keys=True).encode("utf-8")
                target.write_bytes(payload)
                previous = hashlib.sha256(payload).hexdigest()
                most_recent = row
                if i == 0:
                    oldest = target
            checkpoint = {
                "schema_version": VERSION, "sampling_algorithm": ALGORITHM,
                "scope": "bounded_sequential_window_pass",
                "full_interval_verified": False,
                "run_id": run_id, **identity, "safe_area": area,
                "batch_count": batch_count,
                "next_cursor": batch_count, "unique_window_count": batch_count,
                "dialogue_count": batch_count, "dynamic_window_count": 0,
                "sample_count": batch_count, "dynamic_examined_window_count": 0,
                "dynamic_event_examined_window_count": 0,
                "effect_candidates_truncated_windows": 0,
                "violation_count": 0, "no_visible_ink_count": 0,
                "unexamined_unique_windows": 0, "pass_complete": True,
                "last_batch_samples": [most_recent],
                "last_batch_receipt_sha256": previous,
            }
            (output / COVERAGE_FILE).write_text(
                json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
            )
            tracemalloc.start()
            try:
                began = time.perf_counter()
                candidate = read_exported_ass_coverage(
                    output, config, _defer_history_audit=True,
                )
                provisional_seconds = time.perf_counter() - began
                began = time.perf_counter()
                audited = read_exported_ass_coverage(output, config)
                strict_seconds = time.perf_counter() - began
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertFalse(candidate["verified"])
            self.assertEqual(candidate["history_audit"], "deferred")
            self.assertEqual(candidate["next_cursor"], batch_count)
            self.assertTrue(audited["verified"])
            self.assertEqual(audited["state"], "window_pass_complete")
            self.assertFalse(audited["coverage"]["full_interval_verified"])
            stats = {
                "schema_version": 1,
                "fixture": "synthetic_256_chain_receipts_no_ffmpeg",
                "batch_count": batch_count,
                "provisional_seconds": round(provisional_seconds, 4),
                "strict_audit_seconds": round(strict_seconds, 4),
                "tracemalloc_peak_mib": round(peak / 1048576, 3),
                "provisional_verified": candidate["verified"],
                "strict_verified": audited["verified"],
                "full_interval_verified": False,
            }
            print("HSR_ASS_AUDIT_STRESS " + json.dumps(stats, sort_keys=True),
                  flush=True)
            directory = os.environ.get("HSR_ASS_SAMPLER_BENCHMARK_DIR")
            if directory:
                artifact = Path(directory)
                artifact.mkdir(parents=True, exist_ok=True)
                (artifact / "ass-coverage-audit-synthetic.json").write_text(
                    json.dumps(stats, indent=2), encoding="utf-8"
                )
            self.assertLess(strict_seconds, 15.0)
            self.assertLess(peak, 96 * 1048576)
            # A corrupt oldest receipt stays non-verified on provisional read
            # regardless of how many newer receipts remain on disk.
            oldest.write_text("{}", encoding="utf-8")
            candidate_again = read_exported_ass_coverage(
                output, config, _defer_history_audit=True
            )
            self.assertFalse(candidate_again["verified"])
            strict_again = read_exported_ass_coverage(output, config)
            self.assertFalse(strict_again["verified"])
            self.assertEqual(strict_again["state"], "stale")


if __name__ == "__main__":
    unittest.main()
