"""Final exported-ASS raster evidence and cancellable job infrastructure."""
from __future__ import annotations

import json
import shutil
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.project import ProjectConfig
from app.ass_artifact_evidence import (
    PROOF_NAME, plan_export_ass_samples, read_exported_ass_evidence,
    verify_exported_ass,
)
from app.jobs import create_job, cancel_job, get_job
from app.security import api_token
from app.server import app as fastapi_app, _set_active, _clear_active
from app.subtitle_export_status import ASS_NAME, SRT_NAME, record_subtitle_export, export_render_policy_sha256
from app.subtitles import subtitle_render_config


ASS_FIXTURE = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,42,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,first
Dialogue: 0,0:00:02.50,0:00:05.50,Default,,0,0,0,,second
Dialogue: 0,0:00:05.50,0:00:08.00,Default,,0,0,0,,third
"""


class ExportedAssInfraTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.config = ProjectConfig(
            schema_version=1, name="export-evidence",
            root=str(root), index_csv="index.csv", wav_source="wavs",
        )
        self.output = root / "output"
        self.output.mkdir()
        (self.output / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
        (self.output / SRT_NAME).write_text("subtitle\n", encoding="utf-8")
        (self.output / ASS_NAME).write_text(ASS_FIXTURE, encoding="utf-8")
        record_subtitle_export(
            self.output, state="current", ass_required=True,
            render_policy_sha256=export_render_policy_sha256(
                subtitle_render_config(self.config), "en", "zh-CN"
            ),
        )

    def test_plan_selects_widely_distributed_actual_ass_windows(self):
        plan = plan_export_ass_samples(ASS_FIXTURE, 2)
        self.assertEqual(plan["dialogue_count"], 3)
        self.assertEqual(plan["unique_window_count"], 3)
        self.assertEqual(plan["sample_count"], 2)
        self.assertEqual(plan["unexamined_unique_windows"], 1)
        self.assertEqual([x["timestamp_seconds"] for x in plan["samples"]], [1, 6.75])

    def test_motion_and_karaoke_select_real_dynamic_boundaries(self):
        dynamic = ASS_FIXTURE.replace(
            "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,first",
            r"Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\move(20,850,960,850,0,2000)}first",
        ).replace(
            "Dialogue: 0,0:00:02.50,0:00:05.50,Default,,0,0,0,,second",
            r"Dialogue: 0,0:00:02.50,0:00:05.50,Default,,0,0,0,,{\k40}A{\kf60}B{\k50}C",
        )
        plan = plan_export_ass_samples(dynamic, sample_limit=8)
        self.assertEqual(plan["dynamic_window_count"], 2)
        self.assertEqual(plan["unexamined_dynamic_windows"], 0)
        self.assertGreater(plan["candidate_count"], plan["unique_window_count"])
        self.assertLessEqual(plan["sample_count"], 8)
        self.assertTrue(any("motion_transition" in sample["reasons"]
                            or "animated_entry" in sample["reasons"]
                            for sample in plan["samples"]))
        self.assertTrue(any(any(reason.startswith("karaoke_") for reason in sample["reasons"])
                            for sample in plan["samples"]))
        self.assertEqual(plan["candidate_count_scope"],
                         "window_midpoints_plus_targeted_effect_candidates")

    def test_motion_extrema_are_not_mistaken_for_static_midpoint(self):
        dynamic = ASS_FIXTURE.replace(
            "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,first",
            r"Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\move(0,850,960,850)}first",
        )
        plan = plan_export_ass_samples(dynamic, sample_limit=6)
        timestamps = [s["timestamp_seconds"] for s in plan["samples"]]
        self.assertTrue(any(0 <= t < .1 for t in timestamps))
        self.assertTrue(any(.9 <= t <= 1.1 for t in timestamps))
        self.assertTrue(all(s["compositing_scope"] == "all_active_ass_dialogues"
                            for s in plan["samples"]))

    def test_overlapping_dialogues_report_composited_frame_not_cue_ownership(self):
        content = ASS_FIXTURE.replace(
            "Dialogue: 0,0:00:02.50,0:00:05.50,Default,,0,0,0,,second",
            "Dialogue: 0,0:00:01.00,0:00:05.50,Default,,0,0,0,,second",
        )
        plan = plan_export_ass_samples(content, sample_limit=3)
        self.assertTrue(any(p["active_dialogue_count"] >= 2 for p in plan["samples"]))
        self.assertTrue(all(p["compositing_scope"] == "all_active_ass_dialogues"
                            for p in plan["samples"]))

    def test_dynamic_tag_limit_is_bounded_and_explicitly_reported(self):
        many_tags = "".join(r"{\k1}A" for _ in range(250))
        content = ASS_FIXTURE.replace(",,first", ",," + many_tags)
        plan = plan_export_ass_samples(content, sample_limit=8)
        self.assertGreater(plan["effect_candidates_truncated_windows"], 0)
        self.assertLessEqual(plan["sample_count"], 8)
        self.assertEqual(plan["dynamic_window_count"], 1)

    def test_export_parser_respects_reordered_ass_events_format(self):
        modified = ASS_FIXTURE.replace(
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            "Format: Layer, End, Start, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ).replace("0,0:00:00.00,0:00:02.00,Default",
                  "0,0:00:02.00,0:00:00.00,Default")
        with self.assertRaisesRegex(ValueError, "Non-positive"):
            plan_export_ass_samples(modified)
        corrected = modified.replace("0,0:00:02.00,0:00:00.00,Default",
                                     "0,0:00:02.00,0:00:00.00,Default")
        # First Dialogue is now valid under the declared reversed column mapping.
        # Remaining rows use old values and must also be re-ordered explicitly.
        corrected = corrected.replace("0,0:00:02.50,0:00:05.50,Default",
                                      "0,0:00:05.50,0:00:02.50,Default").replace(
                                      "0,0:00:05.50,0:00:08.00,Default",
                                      "0,0:00:08.00,0:00:05.50,Default")
        plan = plan_export_ass_samples(corrected)
        self.assertEqual(plan["dialogue_count"], 3)

    def test_plan_invalid_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            plan_export_ass_samples("not ASS")
        with self.assertRaises(ValueError):
            plan_export_ass_samples(ASS_FIXTURE, 50)
        with self.assertRaises(ValueError):
            plan_export_ass_samples(ASS_FIXTURE.replace("0:00:02.00", "nan"))

    def test_legacy_receipt_requires_real_ass_regeneration_before_new_evidence(self):
        record_subtitle_export(self.output, state="current", ass_required=True)
        from app.ass_artifact_evidence import _export_identity
        with self.assertRaisesRegex(ValueError, "regenerate ASS"):
            _export_identity(self.output, self.config)

    def test_style_changed_after_export_without_regeneration_is_blocked(self):
        from app.ass_artifact_evidence import _export_identity
        self.config.subtitle_chs_size += 4
        with self.assertRaisesRegex(ValueError, "regenerate ASS"):
            _export_identity(self.output, self.config)

    @patch("app.ass_artifact_evidence.load_project")
    @patch("app.ass_artifact_evidence.visible_ink_bbox")
    @patch("app.ass_artifact_evidence._render_raw_rgb", return_value=b"fake")
    @patch("app.ass_artifact_evidence.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_evidence_is_persisted_for_exact_ass_receipt_style_hashes(self, ffmpeg, frame, bbox, load):
        load.return_value = self.config
        bbox.side_effect = [(100, 100, 200, 200), (0, 10, 100, 20)]
        proof = verify_exported_ass(self.config, self.output, sample_limit=2)
        self.assertEqual(proof["status"], "sampled_violation")
        self.assertIs(proof["full_interval_verified"], False)
        self.assertEqual(proof["violation_count"], 1)
        self.assertEqual(proof["dynamic_window_count"], 0)
        self.assertEqual(proof["compositing_scope"], "all_active_ass_dialogues")
        self.assertIn("max_sample_gap_seconds", proof)
        self.assertEqual(frame.call_count, 4)
        self.assertEqual(read_exported_ass_evidence(self.output, self.config)["state"], "sampled_violation")
        self.assertTrue((self.output / PROOF_NAME).exists())
        (self.output / ASS_NAME).write_text(ASS_FIXTURE+"\n; altered", encoding="utf-8")
        self.assertEqual(read_exported_ass_evidence(self.output, self.config)["state"], "stale")

    @patch("app.ass_artifact_evidence.load_project")
    @patch("app.ass_artifact_evidence.visible_ink_bbox", return_value=(100, 100, 200, 200))
    @patch("app.ass_artifact_evidence._render_raw_rgb", return_value=b"fake")
    @patch("app.ass_artifact_evidence.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_policy_change_invalidates_previous_evidence(self, ffmpeg, frame, bbox, load):
        load.return_value = self.config
        verify_exported_ass(self.config, self.output, sample_limit=1)
        self.config.subtitle_margin_horizontal_percent = 0.10
        self.assertEqual(read_exported_ass_evidence(self.output, self.config)["state"], "stale")

    @patch("app.ass_artifact_evidence.load_project")
    @patch("app.ass_artifact_evidence._render_raw_rgb")
    @patch("app.ass_artifact_evidence.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_canceled_export_does_not_publish_proof(self, ffmpeg, frame, load):
        load.return_value = self.config
        event = threading.Event()
        def abort(*args, **kwargs):
            event.set()
            raise InterruptedError("canceled")
        frame.side_effect = abort
        with self.assertRaises(InterruptedError):
            verify_exported_ass(self.config, self.output, cancel_event=event, sample_limit=1)
        self.assertFalse((self.output / PROOF_NAME).exists())

    def test_job_cancellation_is_project_scoped_and_finishes_canceled(self):
        started = threading.Event()
        def work(cancel_event, report):
            started.set()
            if not cancel_event.wait(timeout=5):
                raise RuntimeError("cancel did not arrive")
            raise InterruptedError("cancel arrived")
        root = "/tmp/hsr-evidence-unit-root-20261012"
        job = create_job("ass-artifact-evidence", work, with_cancel=True, with_progress=True,
                         project_root=root)
        self.assertTrue(started.wait(timeout=4))
        with self.assertRaisesRegex(ValueError, "active project"):
            cancel_job(job.id, project_root="/other")
        self.assertTrue(cancel_job(job.id, project_root=root)["cancel_requested"])
        for _ in range(100):
            if (get_job(job.id) or {}).get("state") == "canceled":
                break
            time.sleep(.02)
        self.assertEqual(get_job(job.id)["state"], "canceled")
        with self.assertRaises(ValueError):
            create_job("build", lambda: None, with_cancel=True)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    @patch("app.ass_artifact_evidence.load_project")
    def test_real_ffmpeg_snapshot_receipt_smoke(self, load):
        load.return_value = self.config
        evidence = verify_exported_ass(self.config, self.output, sample_limit=1)
        self.assertTrue(evidence["ass_sha256"])
        self.assertEqual(evidence["scope"], "bounded_exported_ass_samples")
        self.assertIs(evidence["full_interval_verified"], False)
        self.assertEqual(evidence["sample_count"], 1)
        self.assertEqual(read_exported_ass_evidence(self.output, self.config)["state"],
                         evidence["status"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    @patch("app.ass_artifact_evidence.load_project")
    def test_real_exported_motion_detects_boundary_violation(self, load):
        load.return_value = self.config
        # Motion begins outside the saved SafeArea and ends well inside it.
        # A midpoint-only inspector can miss the entrance overshoot.
        moving = ASS_FIXTURE.replace(
            "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,first",
            r"Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\move(5,900,960,900,0,2000)}first",
        )
        (self.output / ASS_NAME).write_text(moving, encoding="utf-8")
        self.config.subtitle_margin_horizontal_percent = .06
        record_subtitle_export(
            self.output, state="current", ass_required=True,
            render_policy_sha256=export_render_policy_sha256(
                subtitle_render_config(self.config), "en", "zh-CN",
            ),
        )
        evidence = verify_exported_ass(self.config, self.output, sample_limit=5)
        self.assertEqual(evidence["dynamic_window_count"], 1)
        self.assertGreater(evidence["sample_count"], 3)
        self.assertTrue(any(
            "motion_transition" in row["reasons"] or "animated_entry" in row["reasons"]
            for row in evidence["samples"]
        ))
        self.assertEqual(evidence["status"], "sampled_violation")
        self.assertGreater(evidence["violation_count"], 0)
        self.assertIs(evidence["full_interval_verified"], False)

    def test_fastapi_evidence_route_requires_matching_project(self):
        from app.project import save_project
        save_project(self.config)
        _set_active(self.config)
        self.addCleanup(_clear_active)
        client = TestClient(fastapi_app, base_url="http://127.0.0.1:8765")
        h = {"X-HSR-Token": api_token()}
        response = client.post(
            "/api/subtitle-layout/exported-evidence/start", headers=h,
            json={"expected_project_root": "/another", "sample_limit": 2},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Active project changed", response.json()["error"])
        status = client.get("/api/subtitle-layout/exported-evidence", headers=h)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["state"], "unverified")

    def test_workbench_exposes_export_binding_and_cancellation(self):
        html = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text("utf-8")
        for fragment in ('id="exportAssEvidenceStartBtn"', 'id="exportAssEvidenceCancelBtn"',
                         "/api/subtitle-layout/exported-evidence/start",
                         "/api/subtitle-layout/exported-evidence/cancel",
                         "full_interval_verified===false"):
            self.assertIn(fragment, html)


if __name__ == "__main__":
    unittest.main()
