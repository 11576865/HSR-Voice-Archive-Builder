"""Real FFmpeg evidence for MKV-only delivery and lossless profile switching."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.archive_bundle_exporter import ArchiveBundleExporter
from app.archive_bundle_lifecycle import (
    OUTPUT, PREFERENCES, bundle_export_status, previous_bundle_settings,
)
from app.delivery_artifacts import (
    ASS, FLAC, SRT, export_optional_track, extract_flac_from_mkv,
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
            audio=publish_flac_only(root)
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
