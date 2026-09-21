from __future__ import annotations

import unittest
from types import SimpleNamespace

from subtitle_layout import (
    SafeArea,
    break_line,
    check_bilingual_collision,
    get_scaled_font_size,
    measure_line_height,
    measure_text_width,
    render_ass,
    solve_subtitle_layout,
)
from subtitle_layout.collision import LayoutBlock


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

    def test_protected_phrases_line_breaking(self) -> None:
        # Long English text containing protected phrases
        text = "Please let alone this matter and do it as soon as possible in order to succeed even though right now at least kind of a lot of work remains."
        lines = break_line(text, max_width=500, font_size=42)
        full_reconstructed = " ".join(lines)
        self.assertIn("as soon as", full_reconstructed)
        self.assertIn("a lot of", full_reconstructed)
        # Verify protected phrase 'as soon as' is intact on some line
        has_phrase = any("as soon as" in line for line in lines)
        self.assertTrue(has_phrase, "Protected phrase 'as soon as' should be kept intact on a single line")

    def test_punctuation_preferred_over_whitespace(self) -> None:
        # Text with punctuation boundary vs earlier whitespace boundary
        text = "Hello world, this is a test sentence."
        # Width where splitting after comma vs splitting after world
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
        self.assertEqual(layout.chs_lines[0].font_size, 52)
        self.assertEqual(layout.primary_lines[0].font_size, 42)

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
        self.assertNotIn(r"\N", ass_output)

    def test_verify_ass_render_script_helper(self) -> None:
        from scripts.verify_ass_render import _get_png_bbox_stdlib
        from pathlib import Path
        import tempfile, struct, zlib

        with tempfile.TemporaryDirectory() as td:
            png_path = Path(td) / "test.png"
            width, height = 100, 100
            # Construct a raw RGB PNG in memory
            raw_rows = []
            for y in range(height):
                row = bytearray([0])  # filter byte
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

    def test_no_english_backslash_N_in_dialogue(self) -> None:
        entry = SimpleNamespace(
            english="Line 1\nLine 2",
            chinese="第一行\n第二行",
            start_seconds=1.0,
            display_end_seconds=3.0,
        )
        ass_output = render_ass([entry], source_language="en", target_language="zh-CN")
        dialogues = [line for line in ass_output.splitlines() if line.startswith("Dialogue:")]
        # Every line must be an independent Dialogue event
        self.assertGreaterEqual(len(dialogues), 2)
        for d in dialogues:
            self.assertNotIn(r"\N", d)


if __name__ == "__main__":
    unittest.main()
