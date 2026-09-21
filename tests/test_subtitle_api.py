from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.project import create_project
from app.security import api_token
from app.server import app, _set_active, _clear_active


class TestSubtitleAPI(unittest.TestCase):
    def setUp(self) -> None:
        _clear_active()
        self.token = api_token()
        self.client = TestClient(app, base_url="http://127.0.0.1:8765")
        self.headers = {"X-HSR-Token": self.token}

    def tearDown(self) -> None:
        _clear_active()

    def test_get_subtitles_mock_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = create_project(
                root,
                name="test_proj_mock",
                index_csv="index.csv",
                wav_source="wavs",
                output_dir="output",
            )
            _set_active(cfg)

            resp = self.client.get("/api/project/active/subtitles", headers=self.headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            subs = data["subtitles"]
            self.assertEqual(len(subs), 2)
            self.assertEqual(subs[0]["id"], 1)
            self.assertIn("May this journey", subs[0]["source_text"])

    def test_get_subtitles_manifest_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "output"
            out.mkdir(parents=True, exist_ok=True)
            manifest = {
                "report": {},
                "entries": [
                    {
                        "index": 1,
                        "start_seconds": 1.0,
                        "display_end_seconds": 4.5,
                        "source_text": "Hello world",
                        "target_text": "你好世界",
                        "target_text_source": "official_target_lab",
                    },
                    {
                        "index": 2,
                        "start_seconds": 5.0,
                        "display_end_seconds": 9.0,
                        "source_text": "Goodbye world",
                        "target_text": "再见世界",
                        "target_text_source": "api:openai:gpt-4",
                        "reference_text": "官方对照文本",
                        "reference_language": "zh-CN",
                    },
                ],
            }
            (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            cfg = create_project(
                root,
                name="test_proj_manifest",
                index_csv="index.csv",
                wav_source="wavs",
                output_dir="output",
            )
            _set_active(cfg)

            resp = self.client.get("/api/project/active/subtitles", headers=self.headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            subs = data["subtitles"]
            self.assertEqual(len(subs), 2)

            self.assertEqual(subs[0]["id"], 1)
            self.assertEqual(subs[0]["official_chs"], "你好世界")
            self.assertEqual(subs[0]["api_chs"], "")
            self.assertEqual(subs[0]["final_chs"], "你好世界")

            self.assertEqual(subs[1]["id"], 2)
            self.assertEqual(subs[1]["official_chs"], "官方对照文本")
            self.assertEqual(subs[1]["api_chs"], "再见世界")
            self.assertEqual(subs[1]["final_chs"], "再见世界")

    def test_get_subtitles_search_and_time_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "output"
            out.mkdir(parents=True, exist_ok=True)
            manifest = {
                "entries": [
                    {
                        "index": 1,
                        "start_seconds": 1.0,
                        "display_end_seconds": 4.0,
                        "source_text": "First line of text",
                        "target_text": "第一行文本",
                    },
                    {
                        "index": 2,
                        "start_seconds": 5.0,
                        "display_end_seconds": 8.0,
                        "source_text": "Second line of text",
                        "target_text": "第二行文本",
                    },
                    {
                        "index": 3,
                        "start_seconds": 10.0,
                        "display_end_seconds": 15.0,
                        "source_text": "Third line of text",
                        "target_text": "第三行文本",
                    },
                ]
            }
            (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            cfg = create_project(
                root,
                name="test_proj_filters",
                index_csv="index.csv",
                wav_source="wavs",
                output_dir="output",
            )
            _set_active(cfg)

            # Test text query 'q'
            resp_q = self.client.get("/api/project/active/subtitles?q=第二", headers=self.headers)
            self.assertEqual(resp_q.status_code, 200)
            subs_q = resp_q.json()["subtitles"]
            self.assertEqual(len(subs_q), 1)
            self.assertEqual(subs_q[0]["id"], 2)

            # Test time range filters
            resp_time = self.client.get("/api/project/active/subtitles?start_time=4.5&end_time=9.0", headers=self.headers)
            self.assertEqual(resp_time.status_code, 200)
            subs_time = resp_time.json()["subtitles"]
            self.assertEqual(len(subs_time), 1)
            self.assertEqual(subs_time[0]["id"], 2)


if __name__ == "__main__":
    unittest.main()
