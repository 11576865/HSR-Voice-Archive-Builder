import unittest

from app.subtitle_layout import SafeArea, SubtitleLayoutEngine


class SubtitleLayoutTests(unittest.TestCase):
    def test_safe_area_dimensions(self):
        area = SafeArea()
        self.assertEqual(area.left, 192)
        self.assertEqual(area.right, 1728)
        self.assertEqual(area.top, 54)
        self.assertEqual(area.bottom, 1026)

    def test_long_text_wraps_inside_width(self):
        engine = SubtitleLayoutEngine()
        lines, size = engine.fit(
            "This is a long subtitle sentence used for testing the layout engine.",
            60,
            40,
            3,
        )
        self.assertLessEqual(len(lines), 3)
        self.assertGreaterEqual(size, 40)

    def test_anchor_directions(self):
        engine = SubtitleLayoutEngine()
        bottom = engine.place_bottom_anchored(["a", "b"], 60)
        top = engine.place_top_anchored(["a", "b"], 60)
        self.assertGreater(bottom[0].y, bottom[1].y)
        self.assertLess(top[0].y, top[1].y)


if __name__ == "__main__":
    unittest.main()
