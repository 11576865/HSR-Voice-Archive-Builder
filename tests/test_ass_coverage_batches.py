"""Durable sequential ASS coverage batches: identity, cancellation, recovery."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.ass_coverage_batches import (
    ALGORITHM, COVERAGE_FILE, RECEIPTS_DIR, inspect_next_exported_ass_batch,
    read_exported_ass_coverage,
)
from app.ass_export_sampling import plan_export_ass_window_batch
from app.project import ProjectConfig, save_project
from app.security import api_token
from app.server import app, _set_active, _clear_active
from app.subtitle_export_status import ASS_NAME, SRT_NAME, record_subtitle_export, export_render_policy_sha256
from app.subtitles import subtitle_render_config
from tests.test_export_ass_infrastructure import ASS_FIXTURE


class CoverageBatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.config = ProjectConfig(schema_version=1, name="coverage-fixture",
                                    root=str(root), index_csv="index.csv", wav_source="wavs")
        self.output = root / "output"
        self.output.mkdir()
        (self.output / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
        (self.output / SRT_NAME).write_text("subtitle\n", encoding="utf-8")
        (self.output / ASS_NAME).write_text(ASS_FIXTURE, encoding="utf-8")
        self.refresh_receipt()

    def refresh_receipt(self):
        record_subtitle_export(
            self.output, state="current", ass_required=True,
            render_policy_sha256=export_render_policy_sha256(
                subtitle_render_config(self.config), "en", "zh-CN",
            ),
        )

    def run_mock(self, *, cursor=0, sample_limit=2, restart=False, event=None, bbox=None):
        with (patch("app.ass_coverage_batches.shutil.which", return_value="/usr/bin/ffmpeg"),
              patch("app.ass_coverage_batches.load_project", return_value=self.config),
              patch("app.ass_coverage_batches._render_raw_rgb", return_value=b"fake"),
              patch("app.ass_coverage_batches.visible_ink_bbox", return_value=bbox or (100, 100, 200, 200))):
            return inspect_next_exported_ass_batch(
                self.config, self.output, expected_cursor=cursor,
                sample_limit=sample_limit, restart=restart, cancel_event=event,
            )

    def test_window_planner_moves_sequentially_and_respects_frame_budget(self):
        plan = plan_export_ass_window_batch(ASS_FIXTURE, cursor=0, sample_limit=2)
        self.assertEqual(plan["next_cursor"], 2)
        self.assertEqual(plan["window_count"], 2)
        self.assertEqual(plan["sample_count"], 2)
        self.assertFalse(plan["pass_complete"])
        last = plan_export_ass_window_batch(ASS_FIXTURE, cursor=2, sample_limit=2)
        self.assertEqual(last["next_cursor"], 3)
        self.assertTrue(last["pass_complete"])
        self.assertFalse(last["full_interval_verified"])
        with self.assertRaisesRegex(ValueError, "complete"):
            plan_export_ass_window_batch(ASS_FIXTURE, cursor=3)
        with self.assertRaisesRegex(ValueError, "integer"):
            plan_export_ass_window_batch(ASS_FIXTURE, sample_limit=True)

    def test_dynamic_cues_charge_two_frames_and_count_effect_time(self):
        moving = ASS_FIXTURE.replace(
            "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,first",
            r"Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\move(5,920,960,920,0,2000)}first",
        )
        plan = plan_export_ass_window_batch(moving, sample_limit=2)
        self.assertEqual(plan["next_cursor"], 1)
        self.assertEqual(plan["sample_count"], 2)
        self.assertEqual(plan["dynamic_examined_window_count"], 1)
        self.assertEqual(plan["dynamic_event_examined_window_count"], 1)
        self.assertTrue(any(s["reasons"] != ["dialogue_midpoint"] for s in plan["samples"]))

    def test_unsupported_karaoke_absolute_offset_does_not_claim_effect_sample(self):
        # \kt establishes an absolute karaoke cursor; this parser recognizes
        # the effect family but deliberately does not decode \kt semantics.
        text = ASS_FIXTURE.replace(
            "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,first",
            r"Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\kt120}first",
        )
        plan = plan_export_ass_window_batch(text, cursor=0, sample_limit=2)
        self.assertEqual(plan["dynamic_examined_window_count"], 1)
        self.assertEqual(plan["dynamic_event_examined_window_count"], 0)
        self.assertGreater(plan["effect_candidates_truncated_windows"], 0)
        self.assertEqual(plan["samples"][0]["reasons"], ["dialogue_midpoint"])

    def test_three_static_windows_resume_across_two_durable_batches(self):
        self.assertEqual(read_exported_ass_coverage(self.output, self.config)["state"], "unverified")
        first = self.run_mock()
        self.assertEqual(first["batch_count"], 1)
        self.assertEqual(first["next_cursor"], 2)
        self.assertEqual(first["sample_count"], 2)
        self.assertFalse(first["full_interval_verified"])
        self.assertEqual(read_exported_ass_coverage(self.output, self.config)["state"], "partial")
        second = self.run_mock(cursor=2)
        self.assertEqual(second["batch_count"], 2)
        self.assertEqual(second["next_cursor"], 3)
        self.assertTrue(second["pass_complete"])
        self.assertEqual(second["sample_count"], 3)
        result = read_exported_ass_coverage(self.output, self.config)
        self.assertTrue(result["verified"])
        self.assertEqual(result["state"], "window_pass_complete")
        self.assertFalse(result["coverage"]["full_interval_verified"])
        # Both committed receipts are independently preserved in the same run.
        self.assertTrue((self.output / RECEIPTS_DIR /
                         f"{first['run_id']}-batch-000001.json").is_file())
        last = self.output / RECEIPTS_DIR / f"{first['run_id']}-batch-000002.json"
        self.assertTrue(last.is_file())
        self.assertEqual(json.loads(last.read_text("utf-8"))["previous_batch_receipt_sha256"],
                         first["last_batch_receipt_sha256"])

    def test_cancellation_mid_batch_keeps_previous_cursor(self):
        first = self.run_mock()
        event = threading.Event()
        def abort(*args, **kwargs):
            event.set()
            raise InterruptedError("canceled while rendering")
        with (patch("app.ass_coverage_batches.shutil.which", return_value="/usr/bin/ffmpeg"),
              patch("app.ass_coverage_batches._render_raw_rgb", side_effect=abort),
              patch("app.ass_coverage_batches.load_project", return_value=self.config)):
            with self.assertRaises(InterruptedError):
                inspect_next_exported_ass_batch(
                    self.config, self.output, expected_cursor=2,
                    sample_limit=2, cancel_event=event,
                )
        status = read_exported_ass_coverage(self.output, self.config)
        self.assertEqual(status["state"], "partial")
        self.assertEqual(status["next_cursor"], first["next_cursor"])
        self.assertEqual(status["coverage"]["batch_count"], 1)

    def test_explicit_restart_does_not_overwrite_old_batch_if_canceled(self):
        first = self.run_mock()
        old_receipt = (self.output / RECEIPTS_DIR /
                       f"{first['run_id']}-batch-000001.json")
        original = old_receipt.read_bytes()
        event = threading.Event()
        def abort(*args, **kwargs):
            event.set()
            raise InterruptedError("canceled")
        with (patch("app.ass_coverage_batches.shutil.which", return_value="/usr/bin/ffmpeg"),
              patch("app.ass_coverage_batches._render_raw_rgb", side_effect=abort)):
            with self.assertRaises(InterruptedError):
                inspect_next_exported_ass_batch(
                    self.config, self.output, expected_cursor=0,
                    sample_limit=2, restart=True, cancel_event=event,
                )
        self.assertEqual(old_receipt.read_bytes(), original)
        self.assertEqual(read_exported_ass_coverage(self.output, self.config)["next_cursor"], 2)
        new = self.run_mock(cursor=0, restart=True)
        self.assertNotEqual(first["run_id"], new["run_id"])
        self.assertEqual(old_receipt.read_bytes(), original)

    def test_stale_ass_and_style_reject_resume(self):
        self.run_mock()
        self.config.subtitle_margin_horizontal_percent += .01
        self.assertEqual(read_exported_ass_coverage(self.output, self.config)["state"], "stale")
        with self.assertRaisesRegex(ValueError, "regenerate ASS"):
            self.run_mock(cursor=2)
        self.config.subtitle_margin_horizontal_percent -= .01
        self.assertEqual(read_exported_ass_coverage(self.output, self.config)["state"], "partial")
        (self.output / ASS_NAME).write_text(ASS_FIXTURE+"; changed", encoding="utf-8")
        self.assertEqual(read_exported_ass_coverage(self.output, self.config)["state"], "stale")

    def test_corrupted_last_batch_receipt_fails_closed(self):
        record = self.run_mock()
        last = (self.output / RECEIPTS_DIR /
                f"{record['run_id']}-batch-000001.json")
        last.write_text("{}", encoding="utf-8")
        self.assertEqual(read_exported_ass_coverage(self.output, self.config)["state"], "stale")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.run_mock(cursor=2)

    def test_ass_change_during_full_history_audit_fails_closed(self):
        from app import ass_coverage_batches as coverage
        self.run_mock()
        original = coverage._audit_committed_batches

        def mutate_after_auditing(*args):
            original(*args)
            (self.output / ASS_NAME).write_text(
                ASS_FIXTURE + "\\n;changed-during-audit", encoding="utf-8"
            )

        with patch.object(coverage, "_audit_committed_batches",
                          side_effect=mutate_after_auditing):
            status = read_exported_ass_coverage(self.output, self.config)
        self.assertEqual(status["state"], "stale")
        self.assertFalse(status["verified"])
        self.assertIn("during audit", status["reason"])

    def test_concurrent_checkpoint_advance_during_audit_is_not_reported_verified(self):
        from app import ass_coverage_batches as coverage
        self.run_mock()
        checkpoint = self.output / COVERAGE_FILE
        original = coverage._audit_committed_batches

        def overwrite_checkpoint(*args):
            original(*args)
            record = json.loads(checkpoint.read_text(encoding="utf-8"))
            record["captured_at"] = "changed-by-concurrent-process"
            checkpoint.write_text(json.dumps(record), encoding="utf-8")

        with patch.object(coverage, "_audit_committed_batches",
                          side_effect=overwrite_checkpoint):
            status = read_exported_ass_coverage(self.output, self.config)
        self.assertEqual(status["state"], "stale")
        self.assertFalse(status["verified"])
        self.assertIn("during audit", status["reason"])

    def test_receipt_audit_reads_each_hashed_json_blob_once(self):
        first = self.run_mock()
        self.run_mock(cursor=2)
        original_read = Path.read_bytes
        reads = []

        def recording_read(path):
            if path.parent.name == RECEIPTS_DIR:
                reads.append(path.name)
            return original_read(path)

        with patch.object(Path, "read_bytes", recording_read):
            status = read_exported_ass_coverage(self.output, self.config)
        self.assertEqual(status["state"], "window_pass_complete")
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(set(reads)), 2)

    def test_oversized_historical_receipt_is_rejected_before_json_decode(self):
        from app.ass_coverage_batches import MAX_BATCH_RECEIPT_BYTES
        record = self.run_mock()
        path = (self.output / RECEIPTS_DIR /
                f"{record['run_id']}-batch-000001.json")
        path.write_bytes(b" " * (MAX_BATCH_RECEIPT_BYTES + 1))
        result = read_exported_ass_coverage(self.output, self.config)
        self.assertEqual(result["state"], "stale")
        self.assertFalse(result["verified"])

    def test_another_process_output_lock_rejects_batch_without_changing_cursor(self):
        from app.ass_coverage_lock import exclusive_coverage_writer
        with exclusive_coverage_writer(self.output):
            with self.assertRaisesRegex(RuntimeError, "another process"):
                self.run_mock()
            self.assertEqual(
                read_exported_ass_coverage(self.output, self.config)["state"],
                "unverified",
            )
        first = self.run_mock()
        self.assertEqual(first["next_cursor"], 2)
        self.assertEqual(read_exported_ass_coverage(
            self.output, self.config)["state"], "partial")

    def test_writer_candidate_progress_is_explicitly_unverified(self):
        first = self.run_mock()
        from app.ass_coverage_batches import read_exported_ass_coverage
        candidate = read_exported_ass_coverage(
            self.output, self.config, _defer_history_audit=True,
        )
        self.assertEqual(candidate["state"], "partial")
        self.assertEqual(candidate["next_cursor"], first["next_cursor"])
        self.assertFalse(candidate["verified"])
        self.assertEqual(candidate["history_audit"], "deferred")
        # The externally exposed default read still audits the full chain.
        self.assertTrue(read_exported_ass_coverage(
            self.output, self.config)["verified"])

    def test_writer_scans_existing_history_only_once_per_resume(self):
        from app import ass_coverage_batches as coverage
        self.run_mock()
        real_audit = coverage._audit_committed_batches
        with patch.object(coverage, "_audit_committed_batches",
                          wraps=real_audit) as audit:
            completed = self.run_mock(cursor=2)
        self.assertEqual(completed["next_cursor"], 3)
        self.assertEqual(audit.call_count, 1)
        self.assertTrue(read_exported_ass_coverage(
            self.output, self.config)["verified"])

    def test_old_receipt_mutated_while_rendering_prevents_new_checkpoint(self):
        from app import ass_coverage_batches as coverage
        first = self.run_mock()
        before = (self.output / COVERAGE_FILE).read_bytes()
        original = (self.output / RECEIPTS_DIR /
                    f"{first['run_id']}-batch-000001.json")
        def render_and_mutate(*args, **kwargs):
            original.write_text("{}", encoding="utf-8")
            return b"fake"
        with (patch("app.ass_coverage_batches.shutil.which",
                    return_value="/usr/bin/ffmpeg"),
              patch("app.ass_coverage_batches.load_project",
                    return_value=self.config),
              patch("app.ass_coverage_batches._render_raw_rgb",
                    side_effect=render_and_mutate),
              patch("app.ass_coverage_batches.visible_ink_bbox",
                    return_value=(100, 100, 200, 200))):
            with self.assertRaisesRegex(
                RuntimeError, "Another process advanced the coverage ledger"
            ):
                inspect_next_exported_ass_batch(
                    self.config, self.output, expected_cursor=2,
                    sample_limit=2,
                )
        self.assertEqual((self.output / COVERAGE_FILE).read_bytes(), before)
        self.assertEqual(read_exported_ass_coverage(
            self.output, self.config)["state"], "stale")
        self.assertFalse((self.output / RECEIPTS_DIR /
                          f"{first['run_id']}-batch-000002.json").exists())

    def test_corrupted_history_preexisting_writer_fails_closed_at_commit(self):
        # Use three batches so the corrupted *first* receipt is not the
        # last receipt. Latest-receipt preflight should succeed; the strict
        # pre-commit historical audit must catch the earlier corruption.
        extra = chr(10).join((
            "Dialogue: 0,0:00:08.50,0:00:09.50,Default,,0,0,0,,fourth",
            "Dialogue: 0,0:00:10.00,0:00:11.00,Default,,0,0,0,,fifth",
        )) + chr(10)
        (self.output / ASS_NAME).write_text(
            ASS_FIXTURE + extra, encoding="utf-8"
        )
        self.refresh_receipt()
        first = self.run_mock()
        second = self.run_mock(cursor=2)
        self.assertEqual(second["next_cursor"], 4)
        old = (self.output / RECEIPTS_DIR /
               f"{first['run_id']}-batch-000001.json")
        old.write_text("{}", encoding="utf-8")
        checkpoint = (self.output / COVERAGE_FILE).read_bytes()
        with self.assertRaisesRegex(
            RuntimeError, "Another process advanced the coverage ledger"
        ):
            self.run_mock(cursor=4)
        self.assertEqual((self.output / COVERAGE_FILE).read_bytes(), checkpoint)
        self.assertFalse((self.output / RECEIPTS_DIR /
                          f"{first['run_id']}-batch-000003.json").exists())

    def test_many_completed_batches_only_reaudit_once_per_writer(self):
        from app import ass_coverage_batches as coverage
        # 32 real ASS Dialogue windows, rendered with the existing deterministic
        # fake frame provider. This tracks the number of strict chain scans,
        # not FFmpeg wall time or an asserted machine-specific duration.
        prefix = ASS_FIXTURE.split("Dialogue:")[0]
        body = "".join(
            f"Dialogue: 0,0:00:{i:02d}.00,0:00:{i+1:02d}.00,"
            "Default,,0,0,0,,sample\n"
            for i in range(32)
        )
        (self.output / ASS_NAME).write_text(prefix + body, encoding="utf-8")
        self.refresh_receipt()
        original = coverage._audit_committed_batches
        with patch.object(coverage, "_audit_committed_batches", wraps=original) as audits:
            for cursor in range(0, 32, 2):
                result = self.run_mock(cursor=cursor, sample_limit=2)
                self.assertEqual(result["next_cursor"], cursor + 2)
            # First batch has no history; subsequent 15 writer callbacks
            # must perform exactly one strict scan immediately before commit.
            self.assertEqual(audits.call_count, 15)
        verified = read_exported_ass_coverage(self.output, self.config)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["state"], "window_pass_complete")
        self.assertFalse(verified["coverage"]["full_interval_verified"])

    def test_failed_frame_render_does_not_scan_old_chain_before_failure(self):
        from app import ass_coverage_batches as coverage
        original = coverage._audit_committed_batches
        first = self.run_mock()
        before = (self.output / COVERAGE_FILE).read_bytes()
        with (patch("app.ass_coverage_batches.shutil.which",
                    return_value="/usr/bin/ffmpeg"),
              patch("app.ass_coverage_batches._render_raw_rgb",
                    side_effect=RuntimeError("synthetic libass failure")),
              patch.object(coverage, "_audit_committed_batches",
                           wraps=original) as audits):
            with self.assertRaisesRegex(RuntimeError, "synthetic libass failure"):
                inspect_next_exported_ass_batch(
                    self.config, self.output, expected_cursor=2,
                    sample_limit=2,
                )
            self.assertEqual(audits.call_count, 0)
        self.assertEqual((self.output / COVERAGE_FILE).read_bytes(), before)
        self.assertEqual(read_exported_ass_coverage(
            self.output, self.config)["next_cursor"], first["next_cursor"])

    def test_damaged_latest_receipt_rejected_in_cheap_preflight(self):
        from app import ass_coverage_batches as coverage
        first = self.run_mock()
        recent = (self.output / RECEIPTS_DIR /
                  f"{first['run_id']}-batch-000001.json")
        recent.write_text("{}", encoding="utf-8")
        with (patch("app.ass_coverage_batches.shutil.which",
                    return_value="/usr/bin/ffmpeg"),
              patch("app.ass_coverage_batches._render_raw_rgb") as renderer,
              patch.object(coverage, "_audit_committed_batches") as audits):
            with self.assertRaisesRegex(ValueError, "stale"):
                inspect_next_exported_ass_batch(
                    self.config, self.output, expected_cursor=2,
                    sample_limit=2,
                )
            renderer.assert_not_called()
            audits.assert_not_called()
        self.assertEqual(read_exported_ass_coverage(
            self.output, self.config)["state"], "stale")

    def test_explicit_restart_skips_discarded_history_without_claiming_old_coverage(self):
        from app import ass_coverage_batches as coverage
        first = self.run_mock()
        self.run_mock(cursor=2)
        old_receipts = sorted((self.output / RECEIPTS_DIR).glob("*.json"))
        # Invalid historical bytes are deliberately not inherited by a fresh
        # run. A normal resume, unlike restart, MUST fail closed.
        old_receipts[0].write_text("{}", encoding="utf-8")
        self.assertEqual(read_exported_ass_coverage(
            self.output, self.config)["state"], "stale")
        with patch.object(coverage, "_audit_committed_batches",
                          side_effect=AssertionError("discarded chain was audited")) as audit:
            fresh = self.run_mock(restart=True)
        self.assertEqual(audit.call_count, 0)
        self.assertEqual(fresh["batch_count"], 1)
        self.assertNotEqual(fresh["run_id"], first["run_id"])
        self.assertEqual(fresh["next_cursor"], 2)
        self.assertEqual(fresh["sample_count"], 2)
        self.assertFalse(fresh["full_interval_verified"])
        self.assertTrue(read_exported_ass_coverage(
            self.output, self.config)["verified"])
        self.assertEqual(old_receipts[0].read_text("utf-8"), "{}")

    def test_restart_aborts_when_old_checkpoint_changes_during_render(self):
        from app import ass_coverage_batches as coverage
        self.run_mock()
        checkpoint = self.output / COVERAGE_FILE
        previously_committed = set((self.output / RECEIPTS_DIR).glob("*.json"))
        def rewrite_checkpoint(*args, **kwargs):
            current = json.loads(checkpoint.read_text(encoding="utf-8"))
            current["captured_at"] = "external-uncooperative-writer"
            checkpoint.write_text(json.dumps(current), encoding="utf-8")
            return b"fake"
        with (patch("app.ass_coverage_batches.shutil.which",
                    return_value="/usr/bin/ffmpeg"),
              patch("app.ass_coverage_batches.load_project",
                    return_value=self.config),
              patch("app.ass_coverage_batches._render_raw_rgb",
                    side_effect=rewrite_checkpoint),
              patch("app.ass_coverage_batches.visible_ink_bbox",
                    return_value=(100, 100, 200, 200)),
              patch.object(coverage, "_audit_committed_batches",
                           side_effect=AssertionError("restart audited old receipts"))):
            with self.assertRaisesRegex(RuntimeError, "changed during explicit restart"):
                inspect_next_exported_ass_batch(
                    self.config, self.output, expected_cursor=0,
                    sample_limit=2, restart=True,
                )
        self.assertEqual(
            set((self.output / RECEIPTS_DIR).glob("*.json")),
            previously_committed,
        )

    def test_restart_fails_closed_if_checkpoint_is_symlink(self):
        old = self.run_mock()
        checkpoint = self.output / COVERAGE_FILE
        elsewhere = self.output / "other-checkpoint"
        elsewhere.write_bytes(checkpoint.read_bytes())
        checkpoint.unlink()
        try:
            checkpoint.symlink_to(elsewhere)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable on this filesystem")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_mock(restart=True)
        self.assertEqual(elsewhere.read_bytes(), checkpoint.read_bytes())
        self.assertEqual(len(list(
            (self.output / RECEIPTS_DIR).glob(f"{old['run_id']}-*.json")
        )), 1)

    def test_cancel_during_historical_audit_never_advances_checkpoint(self):
        from app import ass_coverage_batches as coverage
        first = self.run_mock()
        before = (self.output / COVERAGE_FILE).read_bytes()
        second_receipt = (self.output / RECEIPTS_DIR /
                          f"{first['run_id']}-batch-000002.json")
        event = threading.Event()
        reads = [0]
        original_read = Path.read_bytes

        def cancel_during_second_receipt_read(path):
            data = original_read(path)
            if path.parent.name == RECEIPTS_DIR:
                reads[0] += 1
                if reads[0] == 2:
                    event.set()  # after strict-audit read, not preflight
            return data

        with patch.object(Path, "read_bytes", cancel_during_second_receipt_read):
            with self.assertRaisesRegex(InterruptedError, "audit canceled"):
                self.run_mock(cursor=2, event=event)
        self.assertEqual(reads[0], 2)
        self.assertEqual((self.output / COVERAGE_FILE).read_bytes(), before)
        self.assertFalse(second_receipt.exists())
        self.assertTrue(read_exported_ass_coverage(
            self.output, self.config)["verified"])

    def test_deadline_expires_during_historical_audit_without_commit(self):
        import time
        first = self.run_mock()
        before = (self.output / COVERAGE_FILE).read_bytes()
        second_receipt = (self.output / RECEIPTS_DIR /
                          f"{first['run_id']}-batch-000002.json")
        read_count = [0]
        expired = [False]
        raw_read = Path.read_bytes
        real_clock = time.monotonic

        def record_receipt_read(path):
            data = raw_read(path)
            if path.parent.name == RECEIPTS_DIR:
                read_count[0] += 1
                if read_count[0] == 2:
                    expired[0] = True
            return data

        def advancing_clock():
            return real_clock() + (1000 if expired[0] else 0)

        with (patch.object(Path, "read_bytes", record_receipt_read),
              patch("app.ass_coverage_batches.time.monotonic",
                    side_effect=advancing_clock)):
            with self.assertRaisesRegex(TimeoutError, "audit exceeded"):
                self.run_mock(cursor=2)
        self.assertEqual(read_count[0], 2)
        self.assertEqual((self.output / COVERAGE_FILE).read_bytes(), before)
        self.assertFalse(second_receipt.exists())
        self.assertEqual(read_exported_ass_coverage(
            self.output, self.config)["next_cursor"], 2)

    def test_public_coverage_audit_is_not_limited_by_writer_deadline(self):
        self.run_mock()
        from app import ass_coverage_batches as coverage
        with patch.object(coverage.time, "monotonic",
                          return_value=float("inf")):
            status = read_exported_ass_coverage(self.output, self.config)
        self.assertTrue(status["verified"])
        self.assertNotIn("history_audit", status)

    def test_stale_expected_cursor_rejected(self):
        self.run_mock()
        with self.assertRaisesRegex(ValueError, "cursor changed"):
            self.run_mock(cursor=0)
        with self.assertRaisesRegex(ValueError, "expected_cursor=0"):
            self.run_mock(cursor=2, restart=True)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is unavailable")
    @patch("app.ass_coverage_batches.load_project")
    def test_actual_ffmpeg_export_batch_produces_a_durable_checkpoint(self, load):
        load.return_value = self.config
        result = inspect_next_exported_ass_batch(
            self.config, self.output, sample_limit=2, expected_cursor=0,
        )
        self.assertEqual(result["next_cursor"], 2)
        self.assertEqual(result["sample_count"], 2)
        self.assertTrue(result["last_batch_receipt_sha256"])
        self.assertEqual(read_exported_ass_coverage(self.output, self.config)["state"], "partial")

    def test_server_contract_and_workbench_have_both_progress_controls(self):
        save_project(self.config)
        _set_active(self.config)
        self.addCleanup(_clear_active)
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        h = {"X-HSR-Token": api_token()}
        status = client.get("/api/subtitle-layout/exported-coverage", headers=h)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["state"], "unverified")
        bad = client.post("/api/subtitle-layout/exported-coverage/start", headers=h,
                          json={"expected_project_root": "/wrong", "expected_cursor": 0})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("Active project changed", bad.json()["error"])
        html = (Path(__file__).resolve().parents[1] /
                "app" / "static" / "index.html").read_text("utf-8")
        for term in ("exportAssCoverageStartBtn", "exportAssCoverageRestartBtn",
                     "/api/subtitle-layout/exported-coverage/start",
                     "full_interval_verified===false"):
            self.assertIn(term, html)


if __name__ == "__main__":
    unittest.main()
