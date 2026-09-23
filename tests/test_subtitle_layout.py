from __future__ import annotations

import unittest
from types import SimpleNamespace

from subtitle_layout import (
    SafeArea,
    break_line,
    calculate_target_font_size,
    check_bilingual_collision,
    generate_kinetic_tags,
    get_scaled_font_size,
    measure_line_height,
    measure_text_width,
    render_ass,
    solve_subtitle_layout,
    split_chinese_semantic,
)
from subtitle_layout.measure import clear_measure_cache


class SubtitleLayoutTests(unittest.TestCase):
    def test_safe_area_calculations(self) -> None:
        sa = SafeArea()
        self.assertEqual(sa.canvas_width, 1920)
        self.assertEqual(sa.canvas_height, 1080)
        self.assertEqual(sa.margin_left, 192)
        self.assertEqual(sa.margin_right, 192)
        self.assertEqual(sa.max_printable_width, 1536)
        self.assertEqual(sa.margin_top, 54)
        self.assertEqual(sa.margin_bottom, 54)
        self.assertEqual(sa.max_printable_height, 972)

    def test_font_scaling_discrete_steps(self) -> None:
        base_size = 52
        self.assertEqual(get_scaled_font_size(base_size, 1.0), 52)
        self.assertEqual(get_scaled_font_size(base_size, 0.95), 49)
        self.assertEqual(get_scaled_font_size(base_size, 0.90), 47)
        self.assertEqual(get_scaled_font_size(base_size, 0.85), 44)

    def test_center_outward_alignment_tags_and_coords(self) -> None:
        entry = SimpleNamespace(
            english="Primary main subtitle line.",
            chinese="次要辅助字幕行。",
            start_seconds=1.0,
            display_end_seconds=3.0,
        )
        ass_output = render_ass([entry], source_language="en", target_language="zh-CN")
        self.assertIn("WrapStyle: 2", ass_output)
        self.assertIn(r"{\an2\pos(960,530)", ass_output)
        self.assertIn(r"{\an8\pos(960,550)", ass_output)

    def test_kinetic_tags_duration_capping(self) -> None:
        # Long duration line (2.0s = 2000ms): base 200ms entry and 200ms exit applied
        tags_long = generate_kinetic_tags(2.0)
        self.assertIn(r"\fscx88\fscy88", tags_long)
        self.assertIn(r"\fad(200,200)", tags_long)
        self.assertIn(r"\t(0,120,0.6,\fscx106\fscy106)", tags_long)
        self.assertIn(r"\t(120,200,1.4,\fscx100\fscy100)", tags_long)
        self.assertIn(r"\t(1800,2000,1.5,\fscx92\fscy92)", tags_long)

        # Short duration line (0.3s = 300ms): entry/exit capped to 25% (75ms)
        tags_short = generate_kinetic_tags(0.3)
        self.assertIn(r"\fad(75,75)", tags_short)
        self.assertIn(r"\t(225,300,1.5,\fscx92\fscy92)", tags_short)

    def test_kinetic_tags_position_and_displacement(self) -> None:
        # Static pos tagging
        tags_pos = generate_kinetic_tags(2.0, x=960, y=530)
        self.assertTrue(tags_pos.startswith(r"\pos(960,530)"))

        # Movement displacement tagging
        tags_move = generate_kinetic_tags(2.0, x=960, y=530, entry_y_offset=10)
        self.assertTrue(tags_move.startswith(r"\move(960,540,960,530,0,200)"))

    def test_render_ass_with_kinetic_motion(self) -> None:
        entry = SimpleNamespace(
            english="Kinetic motion test line.",
            chinese="动态效果测试行。",
            start_seconds=1.0,
            display_end_seconds=3.0,
        )
        ass_output = render_ass([entry], enable_kinetic=True)
        self.assertIn(r"\fad(200,200)", ass_output)
        self.assertIn(r"\fscx88\fscy88", ass_output)
        self.assertIn(r"\fscx106\fscy106", ass_output)
        self.assertIn(r"\fscx100\fscy100", ass_output)
        self.assertIn(r"\fscx92\fscy92", ass_output)

    def test_render_ass_kinetic_motion_disabled(self) -> None:
        entry = SimpleNamespace(
            english="Kinetic motion disabled line.",
            chinese="禁用动态效果测试行。",
            start_seconds=1.0,
            display_end_seconds=3.0,
        )
        ass_output = render_ass([entry], enable_kinetic=False)
        self.assertNotIn(r"\fad", ass_output)
        self.assertNotIn(r"\fscx88", ass_output)

    def test_protected_phrases_line_breaking(self) -> None:
        text = "Please let alone this matter and do it as soon as possible in order to succeed even though right now at least kind of a lot of work remains."
        lines = break_line(text, max_width=500, font_size=42)
        full_reconstructed = " ".join(lines)
        self.assertIn("as soon as", full_reconstructed)
        self.assertIn("a lot of", full_reconstructed)
        has_phrase = any("as soon as" in line for line in lines)
        self.assertTrue(has_phrase, "Protected phrase 'as soon as' should be kept intact on a single line")

    def test_repeated_protected_phrases_no_leak(self) -> None:
        text = "We need at least two items at least for now to proceed with at least minimum effort."
        lines = break_line(text, max_width=400, font_size=42)
        full_text = " ".join(lines)
        self.assertEqual(full_text.count("at least"), 3, "All occurrences of 'at least' must be preserved")

    def test_semantic_collocation_binding(self) -> None:
        text = "You must look at all options according to the rules and rely on your team."
        lines = break_line(text, max_width=350, font_size=42)
        full_text = " ".join(lines)
        self.assertIn("look at", full_text)
        self.assertIn("according to", full_text)
        self.assertIn("rely on", full_text)

    def test_cjk_boundary_vs_accented_latin(self) -> None:
        text = "Visit Café and enjoy crème brûlée near the river bank."
        lines = break_line(text, max_width=300, font_size=42)
        for line in lines:
            self.assertNotIn("Caf", line.split()[0] if line.split() else "")

    def test_punctuation_preferred_over_whitespace(self) -> None:
        text = "Hello world, this is a test sentence."
        width = measure_text_width("Hello world, ", 42)
        lines = break_line(text, max_width=width, font_size=42)
        self.assertEqual(lines[0], "Hello world,")

    def test_short_text_layout(self) -> None:
        english = "Hello world!"
        chinese = "你好，世界！"
        layout = solve_subtitle_layout(english, chinese)
        self.assertEqual(layout.scale_factor, 1.0)
        self.assertEqual(len(layout.chs_lines), 1)
        self.assertEqual(len(layout.primary_lines), 1)
        self.assertEqual(layout.chs_lines[0].alignment, 8)
        self.assertEqual(layout.primary_lines[0].alignment, 2)
        self.assertEqual(layout.chs_lines[0].font_size, 52)
        self.assertEqual(layout.primary_lines[0].font_size, 42)
        self.assertEqual(layout.chs_lines[0].y, 550)
        self.assertEqual(layout.primary_lines[0].y, 530)

    def test_multiline_backslash_N_joining(self) -> None:
        entry = SimpleNamespace(
            english="This is a very long primary subtitle designed to exceed maximum printable line width and trigger automatic line wrapping into multiple lines.",
            chinese="这是一个非常漫长的主字幕文本，用来触发自动换行算法并验证在生成Dialogue事件时使用反斜杠N正确连接多行文本。",
            start_seconds=1.0,
            display_end_seconds=3.0,
        )
        ass_output = render_ass([entry], source_language="en", target_language="zh-CN")
        primary_dialogue = [line for line in ass_output.splitlines() if line.startswith("Dialogue: 0,")]
        chs_dialogue = [line for line in ass_output.splitlines() if line.startswith("Dialogue: 1,")]

        self.assertEqual(len(primary_dialogue), 1)
        self.assertEqual(len(chs_dialogue), 1)
        self.assertIn(r"\N", primary_dialogue[0])
        self.assertIn(r"\N", chs_dialogue[0])
        self.assertIn(r"{\an2\pos(960,530)", primary_dialogue[0])
        self.assertIn(r"{\an8\pos(960,550)", chs_dialogue[0])

    def test_long_english_text_layout(self) -> None:
        english = "This is a very long sentence designed to test the automatic line breaking capabilities of the ASS subtitle layout engine when processing English text."
        chinese = "测试中。"
        layout = solve_subtitle_layout(english, chinese)
        self.assertGreater(len(layout.primary_lines), 1)
        self.assertLessEqual(layout.scale_factor, 1.0)

    def test_long_chinese_text_layout(self) -> None:
        english = "Short."
        chinese = "这是一个非常漫长且复杂的中文句子，用来测试字幕布局引擎对于长文本自动换行以及安全区域边界检测的效果。"
        layout = solve_subtitle_layout(english, chinese)
        self.assertGreater(len(layout.chs_lines), 1)

    def test_extreme_bilingual_text_triggers_font_scaling(self) -> None:
        english = " ".join([
            f"Sentence line {i} with a lot of words to ensure long text wrapping across multiple lines."
            for i in range(15)
        ])
        chinese = "，".join([
            f"这里是第{i}句非常繁复漫长用来填满屏幕高度的中文字幕"
            for i in range(15)
        ])
        layout = solve_subtitle_layout(english, chinese)
        self.assertLess(layout.scale_factor, 1.0)
        self.assertIn(layout.scale_factor, (0.95, 0.90, 0.85))

    def test_split_chinese_semantic_punctuation_and_pos(self) -> None:
        text = "这是一个非常漫长的主字幕文本，用来触发自动换行算法并验证语义切分"
        split_res = split_chinese_semantic(text, max_chars_per_line=15)
        self.assertIn(r"\N", split_res)
        lines = split_res.split(r"\N")
        self.assertTrue(all(len(line) >= 4 for line in lines))

    def test_split_chinese_semantic_fallback_without_jieba(self) -> None:
        from unittest.mock import patch
        text = "这是一个非常漫长的主字幕文本用来触发自动换行算法并验证在无结巴分词模块时的回退表现"
        with patch("subtitle_layout.semantic_chunker.pseg", None):
            split_res = split_chinese_semantic(text, max_chars_per_line=15)
            self.assertIn(r"\N", split_res)
            lines = split_res.split(r"\N")
            self.assertTrue(all(len(line) >= 4 for line in lines))

    def test_split_chinese_semantic_kinsoku_shori_orphan_protection(self) -> None:
        text = "活动联谊活动！指标了"
        split_res = split_chinese_semantic(text, max_chars_per_line=8)
        self.assertIn(r"\N", split_res)
        lines = split_res.split(r"\N")
        self.assertFalse(lines[1].startswith("！"))
        self.assertGreaterEqual(len(lines[1]), 4)

    def test_font_size_stability_across_sentences(self) -> None:
        # Verify base font sizes remain strictly consistent (52 for CHS, 42 for Primary)
        short_layout = solve_subtitle_layout("Hello!", "你好！")
        medium_layout = solve_subtitle_layout("This is a medium length sentence for testing.", "这是一个中等长度的句子测试。")
        self.assertEqual(short_layout.chs_lines[0].font_size, 52)
        self.assertEqual(short_layout.primary_lines[0].font_size, 42)
        self.assertEqual(medium_layout.chs_lines[0].font_size, 52)
        self.assertEqual(medium_layout.primary_lines[0].font_size, 42)

    def test_dynamic_scale_factor_minimum_threshold(self) -> None:
        # Scale factors must be within safe limits: 1.0, 0.95, 0.90, 0.85
        english = "This sentence is quite long to test line wrapping and vertical scaling bounds in layout solver."
        chinese = "这是一个很长很长用来测试行数较多时动态字体缩放安全阈值的句子。"
        layout = solve_subtitle_layout(english, chinese)
        self.assertGreaterEqual(layout.scale_factor, 0.85)

    def test_clear_measure_cache(self) -> None:
        clear_measure_cache()

    def test_real_project_voice_samples(self) -> None:
        entries = [
            SimpleNamespace(
                english="May this journey lead us starward.",
                chinese="愿此行，终抵群星。",
                start_seconds=5.0,
                display_end_seconds=7.5,
            ),
            SimpleNamespace(
                english="Rules are made to be broken!",
                chinese="规则，就是用来打破的！",
                start_seconds=8.0,
                display_end_seconds=11.2,
            ),
        ]
        ass_output = render_ass(entries, source_language="en", target_language="zh-CN")
        self.assertIn("[Script Info]", ass_output)
        self.assertIn("PlayResX: 1920", ass_output)
        self.assertIn("PlayResY: 1080", ass_output)
        self.assertIn("愿此行，终抵群星。", ass_output)
        self.assertIn("May this journey lead us starward.", ass_output)

    def test_verify_ass_render_script_helper(self) -> None:
        from scripts.verify_ass_render import _get_png_bbox_stdlib
        from pathlib import Path
        import tempfile, struct, zlib

        with tempfile.TemporaryDirectory() as td:
            png_path = Path(td) / "test.png"
            width, height = 100, 100
            raw_rows = []
            for y in range(height):
                row = bytearray([0])
                for x in range(width):
                    if x == 10 and y == 20:
                        row.extend([255, 255, 255])
                    else:
                        row.extend([0, 0, 0])
                raw_rows.append(bytes(row))
            decompressed = b"".join(raw_rows)
            compressed = zlib.compress(decompressed)

            def make_chunk(chunk_type, data):
                return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", zlib.crc32(chunk_type + data))

            png_data = b"\x89PNG\r\n\x1a\n"
            png_data += make_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            png_data += make_chunk(b"IDAT", compressed)
            png_data += make_chunk(b"IEND", b"")

            png_path.write_bytes(png_data)

            bbox = _get_png_bbox_stdlib(png_path)
            self.assertIsNotNone(bbox)
            self.assertEqual(bbox, (10, 20, 11, 21))


if __name__ == "__main__":
    unittest.main()
