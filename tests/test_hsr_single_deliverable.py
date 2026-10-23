"""Real FFmpeg evidence for MKV-only delivery and lossless profile switching."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.archive_bundle_exporter import ArchiveBundleExporter
from app.archive_bundle_lifecycle import (
    OUTPUT, PREFERENCES, RECEIPT, _bundle_artifact_matches, _read_receipt,
    bundle_export_status, previous_bundle_settings,
)
from app.delivery_artifacts import (
    ASS, FLAC, SRT, LEGACY_BLACK, export_optional_track, extract_flac_from_mkv,
    publish_flac_only, seal_complete_delivery,
)
from app.subtitle_export_status import record_subtitle_export


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Real FFmpeg/FFprobe required")
class OneDeliverableLifecycleTests(unittest.TestCase):
    def create_inputs(self, directory: Path):
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=44100:duration=1.2",
            "-c:a", "flac", str(directory / FLAC),
        ], capture_output=True, check=True, timeout=35)
        (directory / SRT).write_text(
            "1\n00:00:00,000 --> 00:00:01,100\nHello\n", encoding="utf-8")
        (directory / ASS).write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,DejaVu Sans,20,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.10,Default,,0,0,0,,Hello\n",
            encoding="utf-8")
        (directory / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
        record_subtitle_export(directory, state="current", ass_required=True)

    def test_seal_then_export_on_demand_and_switch_modes_losslessly(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self.create_inputs(root)
            mkv=root / OUTPUT
            Exporter=ArchiveBundleExporter(width=320,height=180)
            Exporter.export(root / FLAC,root / ASS,mkv,require_current_ass=True,
                            background_color="#224466")
            self.assertEqual(bundle_export_status(root)["state"],"current")
            seal_complete_delivery(root)
            self.assertTrue(mkv.is_file())
            for name in (FLAC,SRT,ASS):
                self.assertFalse((root / name).exists(), name)
            self.assertEqual(bundle_export_status(root)["state"],"current")
            streams=Exporter._probe(mkv)["streams"]
            self.assertEqual([s["codec_name"] for s in streams
                              if s.get("codec_type")=="audio"],["flac"])
            self.assertEqual([s["codec_name"] for s in streams
                              if s.get("codec_type")=="subtitle"],["ass","subrip"])
            for kind in ("flac","srt","ass"):
                track=export_optional_track(root,kind)
                self.assertTrue(track.is_file(),kind)
                self.assertEqual(track.parent,root / "exports")
            self.assertFalse((root / FLAC).exists())
            self.assertEqual(bundle_export_status(root)["state"],"current")
            # An old subtitle-free black MKV must not survive an explicit
            # switch to the sole FLAC deliverable.
            (root / LEGACY_BLACK).write_bytes(b"legacy black output")
            audio=publish_flac_only(root)
            self.assertFalse((root / LEGACY_BLACK).exists())
            self.assertTrue(audio.is_file())
            self.assertFalse(mkv.exists())
            self.assertFalse((root / ASS).exists())
            self.assertFalse((root / SRT).exists())
            self.assertTrue((root / PREFERENCES).exists())
            self.assertEqual(previous_bundle_settings(root)["background_color"],"#224466")
            self.assertEqual([x["codec_name"] for x in Exporter._probe(audio)["streams"]
                              if x.get("codec_type")=="audio"],["flac"])
            # Switching back uses this same FLAC bitstream, never source WAV.
            compressed=audio.stat().st_size
            (root / SRT).write_text("1\n00:00:00,000 --> 00:00:01,100\nHello\n","utf-8")
            (root / ASS).write_text(
                "[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                "Style: Default,Arial,20,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
                "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:00.00,0:00:01.10,Default,,0,0,0,,Hello\n","utf-8")
            record_subtitle_export(root,state="current",ass_required=True)
            Exporter.export(audio,root / ASS,mkv,require_current_ass=True)
            seal_complete_delivery(root)
            self.assertEqual(bundle_export_status(root)["state"],"current")
            self.assertEqual(len([s for s in Exporter._probe(mkv)["streams"]
                                  if s.get("codec_name")=="flac"]),1)
            self.assertFalse((root / FLAC).exists())
            self.assertGreater(compressed,1000)

    def test_corrupted_existing_flac_cannot_retire_verified_mkv(self):
        """FFprobe header success alone cannot certify a complete audio stream."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.create_inputs(root)
            ArchiveBundleExporter(width=320, height=180).export(
                root / FLAC, root / ASS, root / OUTPUT, require_current_ass=True)
            seal_complete_delivery(root)
            original_mkv = (root / OUTPUT).read_bytes()
            original_receipt = (root / RECEIPT).read_bytes()
            self.assertEqual(bundle_export_status(root)["state"], "current")

            # A partial/hijacked working FLAC remains in the output dir.
            # Simulate a permissive metadata-only probe which reports FLAC
            # even though a full decoder cannot read the actual frames.
            (root / FLAC).write_bytes(b"not-a-decodable-flac-stream")
            with patch(
                "app.delivery_artifacts._probe",
                return_value=[{"codec_type": "audio", "codec_name": "flac"}],
            ):
                with self.assertRaisesRegex(ValueError, "完整 FLAC 解码"):
                    publish_flac_only(root)
            self.assertEqual((root / OUTPUT).read_bytes(), original_mkv)
            self.assertEqual((root / RECEIPT).read_bytes(), original_receipt)
            self.assertTrue((root / FLAC).is_file())
            # The damaged loose FLAC legitimately makes the *working* state
            # stale; what must survive is the previously sealed MKV evidence.
            self.assertEqual(bundle_export_status(root)["state"], "stale")
            self.assertTrue(_read_receipt(root)["sealed"])
            self.assertTrue(_bundle_artifact_matches(
                root / OUTPUT, _read_receipt(root)))
            (root / FLAC).unlink()
            self.assertEqual(bundle_export_status(root)["state"], "current")

    def test_audio_replaced_after_decode_keeps_previous_verified_mkv(self):
        """Non-cooperating writers cannot change just-decoded FLAC on commit."""
        from app.archive_bundle_lifecycle import RECEIPT
        from app.delivery_artifacts import atomic_write_text as write_preferences

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.create_inputs(root)
            ArchiveBundleExporter(width=320, height=180).export(
                root / FLAC, root / ASS, root / OUTPUT, require_current_ass=True)
            seal_complete_delivery(root)
            original_mkv = (root / OUTPUT).read_bytes()
            original_receipt = (root / RECEIPT).read_bytes()
            extract_flac_from_mkv(root)
            mutated = []

            def hijack_after_decode(path, value):
                if path.name == PREFERENCES and not mutated:
                    other = root / "replacement.flac"
                    other.write_bytes(b"untrusted-updated-audio")
                    os.replace(other, root / FLAC)
                    mutated.append(True)
                return write_preferences(path, value)

            with patch("app.delivery_artifacts.atomic_write_text",
                       side_effect=hijack_after_decode):
                with self.assertRaisesRegex(ValueError, "changed after full decode"):
                    publish_flac_only(root)
            self.assertEqual(mutated, [True])
            self.assertEqual((root / OUTPUT).read_bytes(), original_mkv)
            self.assertEqual((root / RECEIPT).read_bytes(), original_receipt)
            self.assertEqual(bundle_export_status(root)["state"], "stale")
            self.assertTrue(_read_receipt(root)["sealed"])
            self.assertTrue(_bundle_artifact_matches(
                root / OUTPUT, _read_receipt(root)))
            (root / FLAC).unlink()
            self.assertEqual(bundle_export_status(root)["state"], "current")

    def test_unlink_failure_after_mkv_removal_restores_last_verified_bundle(self):
        """Standalone mode change cannot strand a missing MKV plus old receipt."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "output"
            root.mkdir()
            self.create_inputs(root)
            ArchiveBundleExporter(width=320, height=180).export(
                root / FLAC, root / ASS, root / OUTPUT, require_current_ass=True)
            seal_complete_delivery(root)
            original_mkv = (root / OUTPUT).read_bytes()
            original_receipt = (root / RECEIPT).read_bytes()
            (root / PREFERENCES).write_text("previous settings", encoding="utf-8")
            extract_flac_from_mkv(root)
            old_unlink = Path.unlink
            failed = []

            def break_receipt_removal(path, *args, **kwargs):
                if path == root / RECEIPT and not failed:
                    failed.append(True)
                    raise PermissionError("injected receipt removal failure")
                return old_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=break_receipt_removal):
                with self.assertRaisesRegex(PermissionError, "receipt removal"):
                    publish_flac_only(root)
            self.assertEqual(failed, [True])
            self.assertEqual((root / OUTPUT).read_bytes(), original_mkv)
            self.assertEqual((root / RECEIPT).read_bytes(), original_receipt)
            self.assertEqual(
                (root / PREFERENCES).read_text(encoding="utf-8"),
                "previous settings",
            )
            self.assertTrue((root / FLAC).is_file())
            self.assertFalse(list(root.parent.glob(".hsr-flac-profile-*")))
            self.assertTrue(_bundle_artifact_matches(
                root / OUTPUT, _read_receipt(root)))
            (root / FLAC).unlink()
            self.assertEqual(bundle_export_status(root)["state"], "current")

    def test_late_legacy_unlink_failure_recovers_prior_subtitle_inputs(self):
        """No half-deleted sources when the last cleanup target is locked."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "output"
            root.mkdir()
            self.create_inputs(root)
            ArchiveBundleExporter(width=320, height=180).export(
                root / FLAC, root / ASS, root / OUTPUT, require_current_ass=True)
            seal_complete_delivery(root)
            extract_flac_from_mkv(root)
            saved = {
                OUTPUT: (root / OUTPUT).read_bytes(),
                RECEIPT: (root / RECEIPT).read_bytes(),
            }
            for name in (SRT, ASS, LEGACY_BLACK):
                payload = ("kept:" + name).encode("utf-8")
                (root / name).write_bytes(payload)
                saved[name] = payload
            old_unlink = Path.unlink
            calls = []

            def deny_legacy_once(path, *args, **kwargs):
                if path == root / LEGACY_BLACK and not calls:
                    calls.append(True)
                    raise PermissionError("injected last output cleanup denial")
                return old_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=deny_legacy_once):
                with self.assertRaisesRegex(PermissionError, "last output"):
                    publish_flac_only(root)
            self.assertEqual(calls, [True])
            for name, payload in saved.items():
                self.assertEqual((root / name).read_bytes(), payload, name)
            self.assertTrue((root / FLAC).is_file())
            self.assertFalse(list(root.parent.glob(".hsr-flac-profile-*")))

    def test_failed_rollback_keeps_snapshot_and_marks_output_busy(self):
        """A second I/O error must not destroy the last snapshot during cleanup."""
        from app.publishing import BUILD_MARKER
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "output"
            root.mkdir()
            self.create_inputs(root)
            ArchiveBundleExporter(width=320, height=180).export(
                root / FLAC, root / ASS, root / OUTPUT, require_current_ass=True)
            seal_complete_delivery(root)
            previous_mkv = (root / OUTPUT).read_bytes()
            extract_flac_from_mkv(root)
            old_unlink, old_replace = Path.unlink, os.replace

            def deny_receipt(path, *args, **kwargs):
                if path == root / RECEIPT:
                    raise PermissionError("injected receipt removal failure")
                return old_unlink(path, *args, **kwargs)

            def deny_mkv_restore(source, target, *args, **kwargs):
                if (Path(target) == root / OUTPUT
                        and Path(source).parent.name.startswith(".hsr-flac-profile-")):
                    raise PermissionError("injected MKV restoration denial")
                return old_replace(source, target, *args, **kwargs)

            with patch.object(Path, "unlink", new=deny_receipt), patch(
                "app.delivery_artifacts.os.replace", side_effect=deny_mkv_restore
            ):
                with self.assertRaisesRegex(RuntimeError, "snapshot retained"):
                    publish_flac_only(root)
            backups = list(root.parent.glob(".hsr-flac-profile-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / OUTPUT).read_bytes(), previous_mkv)
            self.assertTrue((root / BUILD_MARKER).exists())
            self.assertEqual(bundle_export_status(root)["state"], "stale")

    def test_explicit_export_rejects_symlinked_output_folder(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root=Path(td)
            self.create_inputs(root)
            ArchiveBundleExporter(width=320,height=180).export(
                root/FLAC,root/ASS,root/OUTPUT,require_current_ass=True)
            seal_complete_delivery(root)
            (root / "exports").symlink_to(Path(outside),target_is_directory=True)
            with self.assertRaisesRegex(ValueError,"symlink"):
                export_optional_track(root,"flac")
            self.assertFalse(list(Path(outside).iterdir()))

    def test_refuses_unsafe_seal_and_keeps_previous_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self.create_inputs(root)
            target=root / OUTPUT
            ArchiveBundleExporter(width=320,height=180).export(
                root/FLAC,root/ASS,target,require_current_ass=True)
            (root / SRT).write_text("changed after mux",encoding="utf-8")
            with self.assertRaises(ValueError):
                seal_complete_delivery(root)
            self.assertTrue((root / FLAC).is_file())
            self.assertTrue(target.is_file())


if __name__=="__main__":
    unittest.main()
