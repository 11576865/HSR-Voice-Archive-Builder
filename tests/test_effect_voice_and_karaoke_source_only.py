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
from app.archive_bundle_lifecycle import OUTPUT, record_bundle_publication, bundle_audio_source_status
from app.delivery_artifacts import seal_complete_delivery
from app.subtitle_export_status import record_subtitle_export, mark_ass_style_stale
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
            {"index": 102, "group": "A", "start_seconds": 13.5,
             "audio_end_seconds": 16.0},
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
            self.assertEqual(cmd[max(i for i, token in enumerate(cmd) if token == "-i") + 1], str(media))
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

    def test_truncated_audio_video_preview_is_rejected_despite_audio_track(self):
        # Regression: returning a stream-count-valid fixed 8-second clip for
        # a 9.5-second PCM-selected preview is not acceptable.
        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return subprocess.CompletedProcess(
                    cmd, 0,
                    json.dumps({"streams": [{"codec_type": "video"},
                                              {"codec_type": "audio"}],
                                "format": {"duration": 8.0}}), "",
                )
            Path(cmd[-1]).write_bytes(b"old-eight-second-video")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch("app.ass_preview.shutil.which", side_effect=lambda n: n), \
             patch("app.ass_preview.write_ass"), \
             patch("app.ass_preview.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "不一致"):
                render_ass_preview_video(
                    english_text="Actual long voice", chinese_text="真正的长语音",
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
        with self.assertRaisesRegex(ValueError, "没有可用的 continuous.flac"):
            resolve_effect_voice(self.config, 101, fade_ms=100)

    def test_last_voice_preview_includes_explicit_six_hundred_ms_video_tail(self):
        # Final cue has no next voice in the archive, but its 600ms Fade is
        # still visible after the 2.5s recorded speech. The audio is unchanged.
        info = resolve_effect_voice(self.config, 102, fade_ms=600)
        self.assertEqual(info["voice_duration_seconds"], 2.5)
        self.assertAlmostEqual(info["voice_start_offset_seconds"], 0.6)
        self.assertAlmostEqual(info["duration_seconds"], 3.7)
        self.assertEqual(info["preview_after_fade_ms"], 600)
        self.assertAlmostEqual(info["audio_source_start_seconds"], 12.9)

    def test_style_only_invalidation_keeps_other_cue_preview_audio_from_sealed_mkv(self):
        # Complete delivery deliberately discards loose FLAC/ASS/SRT. A
        # subsequent style edit invalidates the subtitle, never the audio.
        for name, data in (
            ("HSR_Voice_Archive.ass", "[Events]\\n"),
            ("HSR_Voice_Archive.srt", "1\\n"),
        ):
            (self.output / name).write_text(data, encoding="utf-8")
        record_subtitle_export(self.output, state="current", ass_required=True)
        (self.output / OUTPUT).write_bytes(b"previous-published-mkv")
        record_bundle_publication(
            self.output, background_color="#112233",
            background_image=None, image_extension=None, include_fonts=False,
            background_spec={"mode": "color"},
        )
        seal_complete_delivery(self.output)
        self.assertFalse((self.output / "continuous.flac").exists())
        self.assertTrue(mark_ass_style_stale(self.output))
        self.assertEqual(bundle_audio_source_status(self.output)["source"], "sealed-mkv")
        preview = resolve_effect_voice(self.config, 102, fade_ms=600)
        self.assertEqual(preview["audio_source_path"], self.output / OUTPUT)
        self.assertAlmostEqual(preview["duration_seconds"], 3.7)

    def test_corrupt_leftover_flac_does_not_override_sealed_archive_preview(self):
        # A new/aborted audio build may leave a nonempty working file.
        # The selected archive voice must still come from the verified MKV.
        for name, data in (
            ("HSR_Voice_Archive.ass", "[Events]\\n"),
            ("HSR_Voice_Archive.srt", "1\\n"),
        ):
            (self.output / name).write_text(data, encoding="utf-8")
        record_subtitle_export(self.output, state="current", ass_required=True)
        (self.output / OUTPUT).write_bytes(b"verified-archive-placeholder")
        record_bundle_publication(
            self.output, background_color="#112233",
            background_image=None, image_extension=None, include_fonts=False,
            background_spec={"mode": "color"},
        )
        seal_complete_delivery(self.output)
        (self.output / "continuous.flac").write_bytes(b"truncated-flac-not-audio")
        self.assertEqual(bundle_audio_source_status(self.output)["source"],
                         "independent-flac")
        self.assertEqual(bundle_audio_source_status(
            self.output, prefer_sealed=True)["source"], "sealed-mkv")
        self.assertEqual(resolve_effect_voice(
            self.config, 101, fade_ms=200)["audio_source_path"],
            self.output / OUTPUT)

    def test_sealed_mkv_cue_preview_rejects_rewritten_pcm_time_without_audio_read(self):
        for name, data in (
            ("HSR_Voice_Archive.ass", "[Events]\\n"),
            ("HSR_Voice_Archive.srt", "1\\n"),
        ):
            (self.output / name).write_text(data, encoding="utf-8")
        record_subtitle_export(self.output, state="current", ass_required=True)
        (self.output / OUTPUT).write_bytes(b"verified-archive-placeholder")
        record_bundle_publication(
            self.output, background_color="#112233", background_image=None,
            image_extension=None, include_fonts=False,
            background_spec={"mode": "color"})
        seal_complete_delivery(self.output)
        recorded = json.loads((self.output / "manifest.json").read_text("utf-8"))
        recorded["entries"][1]["start_seconds"] = 14.2
        (self.output / "manifest.json").write_text(
            json.dumps(recorded), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "语音时间轴.*不一致"):
            resolve_effect_voice(self.config, 102, fade_ms=600)
        self.assertTrue((self.output / OUTPUT).is_file())

    def test_single_cue_ass_end_and_both_fades_match_encoded_preview_envelope(self):
        from types import SimpleNamespace
        entry = SimpleNamespace(
            english="Final spoken cue", chinese="档案末尾",
            start_seconds=0.6, audio_end_seconds=4.6,
            display_end_seconds=5.2, preview_end_margin_ms=600,
        )
        cfg = SubtitleRenderConfig(enable_kinetic=True, fade_in_ms=600, fade_out_ms=600)
        ass = render_ass([entry], config=cfg)
        voices = [line for line in ass.splitlines()
                  if line.startswith("Dialogue:") and (",Primary," in line or ",CHS," in line)]
        self.assertEqual(len(voices), 2)
        self.assertTrue(all("0:00:00.00,0:00:05.20" in line for line in voices))
        self.assertTrue(all(r"\fad(600,600)" in line for line in voices))
        # Even a *shorter* explicitly bounded preview tail may not be
        # overwritten by the archive-only terminal-outgoing fallback.
        entry.preview_end_margin_ms = 200
        entry.display_end_seconds = 4.8
        reduced = render_ass([entry], config=cfg)
        self.assertIn(r"\fad(600,200)", reduced)
        self.assertIn("0:00:00.00,0:00:04.80", reduced)


class LiveEffectPreviewRouteTests(unittest.TestCase):
    def test_fastapi_route_keeps_renderer_settings_separate_from_project(self):
        """Actual FastAPI handler passes SubtitleRenderConfig, not ProjectConfig."""
        import asyncio
        from app import server
        from fastapi.responses import Response
        from subtitle_layout.config import SubtitleRenderConfig

        project = SimpleNamespace(root=str(Path("/fixture/project")), output_dir="output", subtitle_fade_in_ms=200)
        selected = {
            "audio_source_path": Path("/fixture/project/output/continuous.flac"),
            "audio_source_start_seconds": 4.8,
            "voice_start_offset_seconds": 0.2,
            "voice_duration_seconds": 12.0,
            "preview_after_fade_ms": 200,
            "duration_seconds": 12.4,
        }
        submitted = {
            "english_text": "One extended real voice",
            "chinese_text": "真实语音测试",
            "project_voice_entry_id": 101,
            "expected_project_root": str(project.root),
            "preview_duration_seconds": 8,
            "fade_in_ms": 200,
            "preview_format": "mp4",
        }

        class RequestFixture:
            headers = {}
            async def stream(self):
                yield json.dumps(submitted).encode("utf-8")

        captured = {}
        async def execute(renderer, **kwargs):
            captured.update(kwargs)
            return b"encoded-audio-video"

        with patch.object(server, "_active_config", return_value=project), \
             patch.object(server, "resolve_effect_voice", return_value=selected) as resolver, \
             patch.object(server, "run_in_threadpool", side_effect=execute):
            result = asyncio.run(
                server.api_subtitle_layout_render_preview_video(RequestFixture())
            )

        self.assertIsInstance(result, Response)
        self.assertEqual(result.status_code, 200, result.body)
        self.assertIsInstance(captured["config"], SubtitleRenderConfig)
        self.assertEqual(captured["duration_seconds"], 12.4)
        self.assertEqual(captured["voice_duration_seconds"], 12.0)
        self.assertEqual(captured["audio_source_path"], selected["audio_source_path"])
        self.assertEqual(result.headers["x-hsr-preview-audio"], "project")
        self.assertEqual(result.headers["x-hsr-preview-duration"], "12.400")
        resolver.assert_called_once_with(project, 101, fade_ms=200)

    def test_effect_preview_missing_selection_is_guarded_before_sample_id_access(self):
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text("utf-8")
        preview = html[html.index("async function renderHsrEffectVideo(){"):
                       html.index("// Workbench event wiring", html.index("async function renderHsrEffectVideo(){"))]
        self.assertIn("payload.project_voice_entry_id=noProjectVoice?null:layoutStressSample.id", preview)
        self.assertIn("if(noProjectVoice)throw new Error(", preview)
        self.assertNotIn("Math.min(8", preview)
        self.assertIn("X-HSR-Preview-Audio", preview)
        self.assertIn("X-HSR-Preview-Duration", preview)
        self.assertIn("playback.catch", preview)
        self.assertIn("浏览器阻止了自动有声播放", preview)
        self.assertNotIn("video.play().catch(()=>{})", preview)


class LayoutSettingPersistenceTests(unittest.TestCase):
    def test_style_save_invalidates_receipt_without_exporting_or_re_mixing(self):
        from app.project import ProjectConfig, update_project
        from app.subtitle_export_status import record_subtitle_export, read_subtitle_export_health
        from app.auto_delivery import maybe_queue_complete_mkv
        from app.archive_bundle_lifecycle import bundle_export_status
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            output.mkdir()
            (output / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
            (output / "HSR_Voice_Archive.ass").write_text("new-ass", encoding="utf-8")
            (output / "HSR_Voice_Archive.srt").write_text("new-srt", encoding="utf-8")
            (output / "continuous.flac").write_bytes(b"audio-is-not-rewritten")
            record_subtitle_export(output, state="current", ass_required=True)
            config = ProjectConfig(
                schema_version=1, name="save-only", root=str(root),
                index_csv="index.csv", wav_source="wav", output_dir="output",
                delivery_mode="complete",
            )
            before = {
                p.name: p.read_bytes()
                for p in output.iterdir() if p.name.endswith((".ass", ".srt", ".flac"))
            }
            with patch("app.project.save_project"), \
                 patch("app.auto_delivery.create_job", side_effect=AssertionError("unexpected auto mux")):
                update_project(config, subtitle_chs_size=56)
                self.assertEqual(maybe_queue_complete_mkv(config), None)
            after = {
                p.name: p.read_bytes()
                for p in output.iterdir() if p.name.endswith((".ass", ".srt", ".flac"))
            }
            self.assertEqual(after, before)
            self.assertEqual(read_subtitle_export_health(output)["state"], "stale")
            self.assertFalse(bundle_export_status(output)["can_generate"])
            self.assertEqual(config.subtitle_chs_size, 56)


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
        translation_block = code[code.rindex("        if layout.chs_lines:"):code.index("    if overflow_records:")]
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
