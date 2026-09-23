import unittest
from pathlib import Path
from dataclasses import dataclass

from subtitle_layout.ass_writer import (
    format_karaoke_text,
    format_multiline_karaoke,
    generate_frosted_glass_card,
    render_ass,
)
from subtitle_layout.layout_solver import SubtitleLinePos
from subtitle_layout.safe_area import DEFAULT_SAFE_AREA


@dataclass
class DummyEntry:
    english: str
    chinese: str
    start_seconds: float
    display_end_seconds: float
    word_alignments: list | None = None


class TestAdvancedTypography(unittest.TestCase):

    def test_format_karaoke_text_english(self):
        text = "May this journey lead us starward."
        formatted = format_karaoke_text(text, duration_sec=2.0, is_cjk=False)
        self.assertIn(r"{\k", formatted)
        self.assertIn("May ", formatted)
        self.assertIn("starward.", formatted)

    def test_format_karaoke_text_cjk(self):
        text = "愿此行，终抵群星。"
        formatted = format_karaoke_text(text, duration_sec=2.0, is_cjk=True)
        self.assertIn(r"{\k", formatted)
        self.assertIn("愿", formatted)
        self.assertIn("星", formatted)

    def test_format_karaoke_explicit_word_alignments(self):
        alignments = [
            {"word": "May ", "start": 0.0, "end": 0.3},
            {"word": "this ", "start": 0.3, "end": 0.6},
            {"word": "journey.", "start": 0.6, "end": 1.2},
        ]
        formatted = format_karaoke_text("May this journey.", duration_sec=1.2, word_alignments=alignments)
        self.assertEqual(formatted, r"{\k30}May {\k30}this {\k60}journey.")

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

    def test_render_ass_all_effects_enabled(self):
        entries = [
            DummyEntry(
                english="May this journey lead us starward.",
                chinese="愿此行，终抵群星。",
                start_seconds=1.0,
                display_end_seconds=4.0,
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
        )
        self.assertNotIn(r"{\k", plain_ass)
        self.assertNotIn(r"\p1", plain_ass)
        self.assertNotIn(r"\bord6", plain_ass)


if __name__ == "__main__":
    unittest.main()
