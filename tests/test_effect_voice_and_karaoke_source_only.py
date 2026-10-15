"""Effects preview must use the chosen project's real voice, never silent 8s media."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ass_preview import render_ass_preview_video
from app.effect_voice_preview import resolve_effect_voice
from subtitle_layout.ass_writer import render_ass
from subtitle_layout.config import SubtitleRenderConfig


class EffectVoicePreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.config = SimpleNamespace(
            root=str(self.root), output_dir="output",
            subtitle_enable_kinetic=True,
        )
        (self.output / "manifest.json").write_text(json.dumps({"entries": [
            {"index": 101, "group": "A", "start_seconds": 2.0,
             "audio_end_seconds": 11.0},
            {"index": 102, "group": "A", "start_seconds": 12.0,
             "audio_end_seconds": 14.5},
        ]}), encoding="utf-8")
        (self.output / "continuous.flac").write_bytes(b"test-fixture-media")

    def test_long_voice_is_not_silently_truncated_to_eight_seconds(self):
        info = resolve_effect_voice(self.config, 101, fade_ms=250)
        self.assertEqual(info["voice_duration_seconds"], 9.0)
        self.assertAlmostEqual(info["duration_seconds"], 9.5)
        self.assertAlmostEqual(info["audio_source_start_seconds"], 1.75)
        self.assertAlmostEqual(info["voice_start_offset_seconds"], .25)
        self.assertEqual(info["preview_after_fade_ms"], 250)
        self.assertEqual(info["audio_source_path"], self.output / "continuous.flac")
        with self.assertRaisesRegex(ValueError, "时间轴"):
            resolve_effect_voice(self.config, "unknown", fade_ms=250)

    def test_backend_maps_actual_project_audio_and_certifies_encoded_track(self):
        media = self.output / "continuous.flac"
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "ffprobe":
                return subprocess.CompletedProcess(
                    cmd, 0,
                    json.dumps({"streams": [{"codec_type": "video"},
                                             {"codec_type": "audio"}],
                                "format": {"duration": 9.5}}), "",
                )
            self.assertIn("1:a:0", cmd)
            self.assertNotIn("-an", cmd)
            self.assertEqual(cmd[cmd.index("-t") + 1], "9.500")
            self.assertEqual(cmd[cmd.index("-ss") + 1], "1.750")
            self.assertEqual(cmd[cmd.index("-i") + 1], str(media))
            Path(cmd[-1]).write_bytes(b"valid-audio-video")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch("app.ass_preview.shutil.which", side_effect=lambda n: n), \
             patch("app.ass_preview.write_ass") as writer, \
             patch("app.ass_preview.subprocess.run", side_effect=fake_run):
            result = render_ass_preview_video(
                english_text="Long spoken subtitle", chinese_text="较长的一句中文",
                source_language="en", target_language="zh-CN",
                config=SubtitleRenderConfig(), duration_seconds=9.5,
                audio_source_path=media,
                audio_source_start_seconds=1.75,
                voice_start_offset_seconds=.25,
                voice_duration_seconds=9,
                preview_after_fade_ms=250,
            )
        self.assertEqual(result, b"valid-audio-video")
        self.assertEqual(len(calls), 2)
        preview = writer.call_args.args[0][0]
        self.assertAlmostEqual(preview.audio_end_seconds, 9.25)
        self.assertAlmostEqual(preview.start_seconds, .25)
        self.assertEqual(preview.preview_end_margin_ms, 250)

    def test_missing_audio_stream_rejects_untested_silent_video(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return subprocess.CompletedProcess(
                    cmd, 0, '{"streams":[{"codec_type":"video"}]}', "",
                )
            Path(cmd[-1]).write_bytes(b"silent-video")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch("app.ass_preview.shutil.which", side_effect=lambda n: n), \
             patch("app.ass_preview.write_ass"), \
             patch("app.ass_preview.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "没有真实音轨"):
                render_ass_preview_video(
                    english_text="Speech", chinese_text="台词",
                    source_language="en", target_language="zh-CN",
                    config=SubtitleRenderConfig(), duration_seconds=9.5,
                    audio_source_path=self.output / "continuous.flac",
                    audio_source_start_seconds=1.75,
                    voice_start_offset_seconds=.25,
                    voice_duration_seconds=9,
                )

    def test_selected_source_is_bound_by_project_manifest(self):
        with self.assertRaisesRegex(ValueError, "不在"):
            resolve_effect_voice(self.config, 0, fade_ms=100)
        (self.output / "continuous.flac").unlink()
        with self.assertRaisesRegex(ValueError, "无连续 FLAC"):
            resolve_effect_voice(self.config, 101, fade_ms=100)


class KaraokeSourceOnlyTests(unittest.TestCase):
    def test_original_is_progressive_but_chinese_translation_never_has_karaoke(self):
        entry = SimpleNamespace(
            english="Alpha beta", chinese="中文译文",
            start_seconds=1.0, audio_end_seconds=2.0,
            display_end_seconds=2.0,
        )
        for mode in ("k", "kf"):
            with self.subTest(mode=mode):
                config = SubtitleRenderConfig(
                    enable_karaoke=True, karaoke_mode=mode,
                    karaoke_timing="cue", enable_kinetic=True,
                    verify_libass_layout=False,
                )
                ass = render_ass([entry], config=config)
                primary = [line for line in ass.splitlines()
                           if line.startswith("Dialogue:") and ",Primary," in line]
                translated = [line for line in ass.splitlines()
                              if line.startswith("Dialogue:") and ",CHS," in line]
                self.assertTrue(primary)
                self.assertTrue(translated)
                self.assertTrue(any(r"\k" in line for line in primary))
                self.assertTrue(all(
                    r"\k" not in line and r"\kt" not in line
                    and r"\clip" not in line for line in translated
                ))
                self.assertTrue(all(r"\fad" in line for line in translated
                                    if r"\fad" in primary[0]))

    def test_chinese_has_no_clip_overlay_even_when_clip_mode_selected(self):
        code = (Path(__file__).resolve().parents[1] /
                "subtitle_layout/ass_writer.py").read_text(encoding="utf-8")
        translation_block = code[code.index("        if layout.chs_lines:"):]
        self.assertNotIn("generate_clip_karaoke_overlay(", translation_block)
        self.assertNotIn("format_multiline_karaoke(", translation_block)


class AutoSaveDoesNotExportTests(unittest.TestCase):
    def test_layout_setting_autosave_is_configuration_only(self):
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html"
                ).read_text(encoding="utf-8")
        start = html.index("async function saveSubtitleLayoutSettings(")
        end = html.index("// Shared immutable", start)
        body = html[start:end]
        self.assertIn("'/api/subtitle-layout/settings'", body)
        self.assertNotIn("startAssExport(", body)
        self.assertNotIn("runAutoAssRefresh(", body)
        self.assertNotIn("renderLibassPreview(", body)
        self.assertNotIn("renderHsrEffectVideo(", body)
        live = html[html.index("function queueLiveRealPreview()"):
                    html.index("let geometryPreviewGeneration")]
        self.assertNotIn("renderLibassPreview(", live)


if __name__ == "__main__":
    unittest.main()
