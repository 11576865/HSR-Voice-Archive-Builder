"""Durable ASS/SRT export receipts do not confuse committed edits with current files."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.subtitle_export_status import (
    ASS_NAME,
    EXPORT_RECEIPT_NAME,
    SRT_NAME,
    read_subtitle_export_health,
    record_subtitle_export,
)


class SubtitleExportReceiptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name)
        (self.output / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
        (self.output / SRT_NAME).write_text("one\n", encoding="utf-8")
        (self.output / ASS_NAME).write_text("[Events]\n", encoding="utf-8")

    def test_success_receipt_survives_process_reload_and_rejects_tampered_inputs(self):
        record_subtitle_export(self.output, state="current", ass_required=True)
        self.assertEqual(read_subtitle_export_health(self.output)["state"], "current")
        self.assertTrue(read_subtitle_export_health(self.output)["artifacts_current"])
        (self.output / "subtitle_timing_overrides.json").write_text(
            '{"schema_version":1,"entries":{}}', encoding="utf-8"
        )
        status = read_subtitle_export_health(self.output)
        self.assertEqual(status["state"], "stale")
        self.assertFalse(status["artifacts_current"])
        self.assertIn("source or human", status["artifact_error"])
        record_subtitle_export(self.output, state="current", ass_required=True)
        self.assertEqual(read_subtitle_export_health(self.output)["state"], "current")
        (self.output / SRT_NAME).write_text("altered outside app\n", encoding="utf-8")
        self.assertEqual(read_subtitle_export_health(self.output)["state"], "stale")

    def test_missing_ass_is_not_marked_complete_when_ass_was_required(self):
        record_subtitle_export(self.output, state="current", ass_required=True)
        (self.output / ASS_NAME).unlink()
        status=read_subtitle_export_health(self.output)
        self.assertEqual(status["state"], "stale")
        self.assertTrue(status["ass_required"])
        self.assertFalse(status["artifacts_current"])
        with self.assertRaisesRegex(ValueError, "missing ASS"):
            record_subtitle_export(self.output, state="current", ass_required=True)

    def test_pending_failure_and_corrupt_receipts_fail_closed(self):
        record_subtitle_export(self.output, state="pending", ass_required=True)
        self.assertEqual(read_subtitle_export_health(self.output)["state"], "pending")
        record_subtitle_export(
            self.output, state="failed", ass_required=True,
            artifact_error="synthetic ASS writer failure",
        )
        fail = read_subtitle_export_health(self.output)
        self.assertEqual(fail["state"], "failed")
        self.assertIn("synthetic ASS", fail["artifact_error"])
        (self.output / EXPORT_RECEIPT_NAME).write_text("{", encoding="utf-8")
        status=read_subtitle_export_health(self.output)
        self.assertEqual(status["state"], "unverified")
        self.assertFalse(status["artifacts_current"])

    def test_absent_receipt_distinguishes_unknown_legacy_export_from_unbuilt(self):
        status=read_subtitle_export_health(self.output)
        self.assertEqual(status["state"], "unverified")
        self.assertFalse(status["artifacts_current"])
        self.assertTrue(status["ass_required"])
        (self.output / "manifest.json").unlink()
        self.assertEqual(read_subtitle_export_health(self.output)["state"], "not-built")


if __name__ == "__main__":
    unittest.main()
