import tempfile
import unittest
from pathlib import Path
from dataclasses import dataclass

from subtitle_layout.config import SubtitleRenderConfig
from subtitle_layout.ass_writer import (
    format_karaoke_text,
    format_multiline_karaoke,
    generate_frosted_glass_card,
    render_ass,
    write_ass,
)
from subtitle_layout.layout_solver import SubtitleLinePos
from subtitle_layout.safe_area import DEFAULT_SAFE_AREA
from subtitle_layout.fonts import _installed_font_families, list_font_families, validate_ass_font_name


@dataclass
class DummyEntry:
    english: str
    chinese: str
    start_seconds: float
    display_end_seconds: float
    word_alignments: list | None = None
    id: int | str = 1
    group: str = ""
    character: str = ""


class TestAdvancedTypography(unittest.TestCase):

    def test_subtitle_render_config_presets(self):
        plain = SubtitleRenderConfig.plain_text_preset()
        self.assertFalse(plain.enable_kinetic)
        self.assertFalse(plain.enable_karaoke)
        self.assertEqual(plain.fade_in_ms, 0)

        default_cfg = SubtitleRenderConfig.pr72_default_preset()
        self.assertTrue(default_cfg.enable_kinetic)
        self.assertEqual(default_cfg.fade_in_ms, 200)

        entries = [
            DummyEntry(
                english="Preset test line.",
                chinese="预设测试行。",
                start_seconds=1.0,
                display_end_seconds=4.0,
            )
        ]
        plain_ass = render_ass(entries, config=plain)
        self.assertNotIn(r"\fad", plain_ass)

        default_ass = render_ass(entries, config=default_cfg)
        self.assertIn(r"\fad(200,200)", default_ass)

    def test_karaoke_without_word_timing_degrades_to_plain_text(self):
        english = "May this journey lead us starward."
        chinese = "愿此行，终抵群星。"
        self.assertEqual(
            format_karaoke_text(english, duration_sec=2.0, is_cjk=False),
            english,
        )
        self.assertEqual(
            format_karaoke_text(chinese, duration_sec=2.0, is_cjk=True),
            chinese,
        )

    def test_partial_karaoke_alignment_falls_back_without_dropping_text(self):
        text = "Hello world"
        formatted = format_karaoke_text(
            text,
            duration_sec=1.0,
            word_alignments=[{"word": "Hello", "start": 0.0, "end": 0.4}],
        )
        self.assertEqual(formatted, text)
        self.assertNotIn(r"{\k", formatted)

    def test_non_monotonic_karaoke_alignment_falls_back(self):
        text = "Hello world"
        formatted = format_karaoke_text(
            text,
            duration_sec=1.0,
            word_alignments=[
                {"word": "Hello ", "start": 0.4, "end": 0.7},
                {"word": "world", "start": 0.2, "end": 0.8},
            ],
        )
        self.assertEqual(formatted, text)

    def test_list_font_families_uses_fc_list_without_exposing_paths(self):
        from unittest.mock import patch
        _installed_font_families.cache_clear()
        completed = type("Completed", (), {
            "returncode": 0,
            "stdout": "Noto Sans,Noto Sans CJK SC\nInter\nNoto Sans\n",
        })()
        with patch("subtitle_layout.fonts.shutil.which", return_value="/usr/bin/fc-list"), patch(
            "subtitle_layout.fonts.subprocess.run", return_value=completed
        ):
            families = list_font_families()
        self.assertEqual(families, ["Inter", "Noto Sans", "Noto Sans CJK SC"])
        self.assertEqual(list_font_families("noto", 1), ["Noto Sans"])
        self.assertTrue(all("/" not in family for family in families))
        _installed_font_families.cache_clear()

    def test_ass_font_name_rejects_delimiters(self):
        with self.assertRaises(ValueError):
            validate_ass_font_name("Arial, sans-serif")
        with self.assertRaises(ValueError):
            validate_ass_font_name("Arial\nStyle: Injected")
        self.assertEqual(validate_ass_font_name("Noto Sans"), "Noto Sans")

    def test_format_karaoke_explicit_word_alignments(self):
        alignments = [
            {"word": "May ", "start": 0.0, "end": 0.3},
            {"word": "this ", "start": 0.3, "end": 0.6},
            {"word": "journey.", "start": 0.6, "end": 1.2},
        ]
        formatted = format_karaoke_text("May this journey.", duration_sec=1.2, word_alignments=alignments)
        self.assertEqual(formatted, r"{\k30}May {\k30}this {\k60}journey.")

    def test_format_kf_karaoke_explicit_word_alignments(self):
        alignments = [
            {"word": "May ", "start": 0.0, "end": 0.3},
            {"word": "this ", "start": 0.3, "end": 0.6},
            {"word": "journey.", "start": 0.6, "end": 1.2},
        ]
        formatted = format_karaoke_text(
            "May this journey.",
            duration_sec=1.2,
            word_alignments=alignments,
            mode="kf",
        )
        self.assertEqual(formatted, r"{\kf30}May {\kf30}this {\kf60}journey.")

    def test_render_ass_clip_karaoke_uses_progressive_clip_layer(self):
        entry = DummyEntry(
            english="May this journey.",
            chinese="愿此行。",
            start_seconds=1.0,
            display_end_seconds=3.0,
            word_alignments=[
                {"word": "May ", "start": 0.0, "end": 0.4},
                {"word": "this ", "start": 0.4, "end": 0.8},
                {"word": "journey.", "start": 0.8, "end": 1.6},
            ],
        )
        cfg = SubtitleRenderConfig(
            enable_karaoke=True,
            karaoke_mode="clip",
            enable_kinetic=False,
        )
        ass_content = render_ass([entry], config=cfg)
        self.assertIn("Dialogue: 3,", ass_content)
        self.assertIn(r"\clip(", ass_content)
        self.assertIn(r"\t(", ass_content)
        self.assertIn(r"\1c&H00E8FF&", ass_content)

    def test_render_ass_archive_hud_uses_character_group_and_entry_id(self):
        entry = DummyEntry(
            english="Archive metadata.",
            chinese="档案元数据。",
            start_seconds=1.0,
            display_end_seconds=3.0,
            id=7,
            group="chapter-a",
        )
        cfg = SubtitleRenderConfig(
            enable_kinetic=False,
            enable_archive_hud=True,
            archive_character="Evanescia",
            archive_hud_font_size=20,
            archive_hud_opacity=0.7,
        )
        ass_content = render_ass([entry], config=cfg)
        self.assertIn("Style: Archive", ass_content)
        self.assertIn("Dialogue: 5,", ass_content)
        self.assertIn("EVANESCIA · chapter-a · #7", ass_content)

    def test_generate_frosted_glass_card(self):
        lines = [
            SubtitleLinePos(text="May this journey lead us starward.", font_size=42, x=960, y=486, alignment=2)
        ]
        card_res = generate_frosted_glass_card(lines, safe_area=DEFAULT_SAFE_AREA)
        self.assertIsNotNone(card_res)
        card_text, bbox = card_res
        self.assertIn(r"\p1", card_text)
        self.assertIn(r"\1a&H60&", card_text)

        x1, y1, x2, y2 = bbox
        self.assertGreaterEqual(x1, DEFAULT_SAFE_AREA.x_min)
        self.assertLessEqual(x2, DEFAULT_SAFE_AREA.x_max)
        self.assertGreaterEqual(y1, DEFAULT_SAFE_AREA.y_min)
        self.assertLessEqual(y2, DEFAULT_SAFE_AREA.y_max)

    def test_card_opacity_is_configurable_without_changing_default(self):
        lines = [
            SubtitleLinePos(text="Opacity test.", font_size=42, x=960, y=486, alignment=2)
        ]
        default_card, _ = generate_frosted_glass_card(lines, safe_area=DEFAULT_SAFE_AREA)
        half_card, _ = generate_frosted_glass_card(
            lines, safe_area=DEFAULT_SAFE_AREA, opacity=0.5
        )
        self.assertIn(r"\1a&H60&", default_card)
        self.assertIn(r"\1a&H7F&", half_card)

    def test_render_ass_readability_and_soft_entry_controls(self):
        entry = DummyEntry(
            english="Soft entry.",
            chinese="柔和进入。",
            start_seconds=1.0,
            display_end_seconds=4.0,
        )
        cfg = SubtitleRenderConfig(
            enable_kinetic=False,
            outline_width=4.5,
            shadow_depth=1.5,
            blur_radius=0.8,
            enable_soft_entry=True,
            soft_entry_scale_percent=98.0,
            soft_entry_blur=1.5,
            soft_entry_ms=160,
        )
        ass_content = render_ass([entry], config=cfg)
        self.assertIn(",1,4.5,1.5,8,", ass_content)
        self.assertIn(r"\fscx98\fscy98\blur1.5\t(0,160,\fscx100\fscy100\blur0.8)", ass_content)
        self.assertNotIn(r"\fad", ass_content)

    def test_render_ass_base_blur_without_soft_entry(self):
        entry = DummyEntry(
            english="Blur test.",
            chinese="模糊测试。",
            start_seconds=1.0,
            display_end_seconds=3.0,
        )
        cfg = SubtitleRenderConfig(
            enable_kinetic=False,
            blur_radius=0.7,
            enable_soft_entry=False,
        )
        ass_content = render_ass([entry], config=cfg)
        self.assertIn(r"\blur0.7", ass_content)
        self.assertNotIn(r"\t(", ass_content)

    def test_render_ass_all_effects_enabled(self):
        entries = [
            DummyEntry(
                english="May this journey lead us starward.",
                chinese="愿此行，终抵群星。",
                start_seconds=1.0,
                display_end_seconds=4.0,
                word_alignments=[
                    {"word": "May ", "start": 0.0, "end": 0.4},
                    {"word": "this ", "start": 0.4, "end": 0.8},
                    {"word": "journey ", "start": 0.8, "end": 1.4},
                    {"word": "lead ", "start": 1.4, "end": 1.8},
                    {"word": "us ", "start": 1.8, "end": 2.1},
                    {"word": "starward.", "start": 2.1, "end": 3.0},
                ],
            )
        ]
        ass_content = render_ass(
            entries,
            enable_karaoke=True,
            enable_frosted_glass=True,
            enable_multi_layer_outline=True,
        )

        self.assertIn("Style: Card", ass_content)
        self.assertIn(r"\p1", ass_content)
        self.assertIn(r"\bord6", ass_content)
        self.assertIn(r"{\k", ass_content)
        self.assertIn("Dialogue: 0,", ass_content)
        self.assertIn("Dialogue: 1,", ass_content)

    def test_render_config_controls_layout_and_style_header(self):
        entry = DummyEntry(
            english="Custom layout.",
            chinese="自定义排版。",
            start_seconds=1.0,
            display_end_seconds=3.0,
        )
        cfg = SubtitleRenderConfig(
            chs_font="Test CHS",
            primary_font="Test Primary",
            base_chs_size=58,
            base_primary_size=46,
            margin_horizontal_percent=0.15,
            margin_vertical_percent=0.08,
            min_central_gap=30.0,
            enable_kinetic=False,
        )
        ass_content = render_ass([entry], config=cfg)
        self.assertIn("Style: CHS,Test CHS,58", ass_content)
        self.assertIn("Style: Primary,Test Primary,46", ass_content)
        self.assertIn(r"{\an2\pos(960,613)\fs46}", ass_content)
        self.assertIn(r"{\an8\pos(960,643)\fs58}", ass_content)

    def test_write_ass_rejects_partial_output_and_removes_stale_file(self):
        ok = DummyEntry(
            english="Short.",
            chinese="短句。",
            start_seconds=1.0,
            display_end_seconds=2.0,
        )
        bad = DummyEntry(
            english="This is an extremely long primary sentence with oversized font size that causes top overflow." * 3,
            chinese="这是一句极长且字号巨大的中文字幕，第一行第二行第三行不断向下堆叠增长直到碰撞。" * 3,
            start_seconds=3.0,
            display_end_seconds=5.0,
        )
        cfg = SubtitleRenderConfig(
            base_chs_size=64,
            base_primary_size=54,
            margin_horizontal_percent=0.25,
            margin_vertical_percent=0.20,
            min_central_gap=150.0,
        )
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "archive.ass"
            target.write_text("stale", encoding="utf-8")
            report = Path(td) / "overflow.json"
            with self.assertRaisesRegex(ValueError, "No partial ASS was produced"):
                write_ass([ok, bad], target, config=cfg, overflow_report_path=report)
            self.assertFalse(target.exists())
            self.assertTrue(report.is_file())

    def test_render_ass_toggle_flags(self):
        entries = [
            DummyEntry(
                english="May this journey lead us starward.",
                chinese="愿此行，终抵群星。",
                start_seconds=1.0,
                display_end_seconds=4.0,
            )
        ]
        plain_ass = render_ass(
            entries,
            enable_karaoke=False,
            enable_frosted_glass=False,
            enable_multi_layer_outline=False,
            enable_kinetic=False,
        )
        self.assertNotIn(r"{\k", plain_ass)
        self.assertNotIn(r"\p1", plain_ass)
        self.assertNotIn(r"\bord6", plain_ass)
        self.assertNotIn(r"\fad", plain_ass)

    def test_render_ass_kinetic_motion_displacement(self):
        entries = [
            DummyEntry(
                english="May this journey lead us starward.",
                chinese="愿此行，终抵群星。",
                start_seconds=1.0,
                display_end_seconds=4.0,
            )
        ]
        ass_output = render_ass(
            entries,
            enable_kinetic=True,
            kinetic_options={
                "primary_entry_y_offset": 8,
                "chs_entry_y_offset": -8,
            },
        )
        self.assertIn(r"\move(960,633,960,625,0,200)", ass_output)
        self.assertIn(r"\move(960,637,960,645,0,200)", ass_output)

    def test_render_ass_kinetic_motion_multi_layer_outline_sync(self):
        entries = [
            DummyEntry(
                english="Synchronized motion test.",
                chinese="同步动态测试。",
                start_seconds=1.0,
                display_end_seconds=4.0,
            )
        ]
        ass_output = render_ass(
            entries,
            enable_kinetic=True,
            enable_multi_layer_outline=True,
        )
        lines = [line for line in ass_output.splitlines() if line.startswith("Dialogue:")]
        # Ensure outline lines and text lines share identical kinetic tags
        for line in lines:
            if "Primary" in line or "CHS" in line:
                self.assertIn(r"\fad(200,200)", line)

    def test_render_ass_kinetic_motion_frosted_glass_fade(self):
        entries = [
            DummyEntry(
                english="Frosted glass motion test.",
                chinese="磨砂玻璃动态测试。",
                start_seconds=1.0,
                display_end_seconds=4.0,
            )
        ]
        ass_output = render_ass(
            entries,
            enable_kinetic=True,
            enable_frosted_glass=True,
        )
        card_lines = [line for line in ass_output.splitlines() if line.startswith("Dialogue:") and ",Card," in line]
        self.assertTrue(len(card_lines) > 0)
        for line in card_lines:
            self.assertIn(r"\fad(200,200)", line)


if __name__ == "__main__":
    unittest.main()
