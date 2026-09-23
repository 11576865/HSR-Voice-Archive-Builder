from __future__ import annotations

import json
import unittest
from fastapi.testclient import TestClient

from app.security import api_token
from app.server import app as fastapi_app
from subtitle_layout.preview import preview_subtitle_layout


class SubtitlePreviewUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = api_token()
        self.client = TestClient(fastapi_app, base_url="http://127.0.0.1:8765")
        self.headers = {"X-HSR-Token": self.token}

    def test_default_layout_preview(self):
        res = preview_subtitle_layout(
            english_text="May this journey lead us starward.",
            chinese_text="愿此行，终抵群星。",
            base_chs_size=52,
            base_primary_size=42,
            margin_left_percent=0.10,
            margin_top_percent=0.05,
            min_central_gap=20.0,
        )
        self.assertTrue(res["ok"])
        self.assertFalse(res["layout"]["failed"])
        self.assertIsNone(res["layout"]["failed_condition"])
        self.assertEqual(res["safe_area"]["margin_left"], 192)
        self.assertEqual(res["safe_area"]["margin_top"], 54)
        self.assertEqual(len(res["layout"]["primary_lines"]), 1)
        self.assertEqual(len(res["layout"]["chs_lines"]), 1)
        self.assertIn("parallax", res)
        self.assertIn("primary_offset", res["parallax"])
        self.assertIn("chs_offset", res["parallax"])
        self.assertIn("total_span", res["parallax"])
        self.assertIn("parallax_ratio", res["parallax"])

    def test_custom_margins_and_gap(self):
        res = preview_subtitle_layout(
            margin_left_percent=0.15,
            margin_top_percent=0.08,
            min_central_gap=30.0,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["safe_area"]["margin_left"], round(1920 * 0.15))
        self.assertEqual(res["safe_area"]["margin_top"], round(1080 * 0.08))
        self.assertEqual(res["central_gap"]["min_central_gap"], 30.0)

    def test_collision_detection_in_preview(self):
        # Oversized text and extreme central gap forcing collision
        res = preview_subtitle_layout(
            english_text="This is an extremely long primary sentence with oversized font size that causes top overflow." * 3,
            chinese_text="这是一句极长且字号巨大的中文字幕，第一行第二行第三行不断向下堆叠增长直到碰撞。" * 3,
            base_chs_size=64,
            base_primary_size=54,
            margin_left_percent=0.25,
            margin_top_percent=0.20,
            min_central_gap=150.0,
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["layout"]["failed"])
        self.assertIsNotNone(res["layout"]["failed_condition"])

    def test_fastapi_endpoint(self):
        response = self.client.post(
            "/api/subtitle-layout/preview",
            headers=self.headers,
            json={
                "english_text": "Hello World",
                "chinese_text": "你好世界",
                "base_chs_size": 52,
                "base_primary_size": 42,
                "margin_left_percent": 0.10,
                "margin_top_percent": 0.05,
                "min_central_gap": 20.0,
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["layout"]["failed"])
        self.assertIn("canvas", data)
        self.assertIn("safe_area", data)
        self.assertIn("central_gap", data)
        self.assertIn("parallax", data)
        self.assertIn("layout", data)


if __name__ == "__main__":
    unittest.main()
