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

    def test_get_subtitles_no_manifest_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = create_project(
                root,
                name="test_proj_empty",
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
            self.assertEqual(len(subs), 0)

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

    def test_update_subtitles_valid_and_persistence(self) -> None:
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
                        "source_text": "May this journey lead us starward.",
                        "target_text": "愿此行，终抵群星。",
                    }
                ]
            }
            (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            cfg = create_project(
                root,
                name="test_proj_update",
                index_csv="index.csv",
                wav_source="wavs",
                output_dir="output",
            )
            _set_active(cfg)

            payload = {
                "subtitles": [
                    {
                        "id": 1,
                        "final_chs": "愿这场旅程带我们走向群星（已审核修改）。",
                    }
                ]
            }
            resp = self.client.post("/api/project/active/subtitles/update", json=payload, headers=self.headers)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])
            self.assertEqual(resp.json()["updated_count"], 1)

            # Check overrides file created
            overrides_file = out / "subtitles_overrides.json"
            self.assertTrue(overrides_file.is_file())
            overrides_data = json.loads(overrides_file.read_text(encoding="utf-8"))
            self.assertIn("1", overrides_data)
            self.assertEqual(overrides_data["1"]["final_chs"], "愿这场旅程带我们走向群星（已审核修改）。")

            # Check ASS and SRT files regenerated
            ass_file = out / "HSR_Voice_Archive.ass"
            srt_file = out / "HSR_Voice_Archive.srt"
            self.assertTrue(ass_file.is_file())
            self.assertTrue(srt_file.is_file())
            self.assertIn("愿这场旅程带我们走向群星（已审核修改）。", ass_file.read_text(encoding="utf-8"))

            # Check GET reflects modification
            get_resp = self.client.get("/api/project/active/subtitles", headers=self.headers)
            self.assertEqual(get_resp.status_code, 200)
            sub = get_resp.json()["subtitles"][0]
            self.assertEqual(sub["final_chs"], "愿这场旅程带我们走向群星（已审核修改）。")
            self.assertTrue(sub["modified"])

    def test_update_subtitles_rejects_immutable_fields(self) -> None:
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
                        "source_text": "Original English",
                        "target_text": "原始中文",
                    }
                ]
            }
            (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            cfg = create_project(
                root,
                name="test_proj_immutable",
                index_csv="index.csv",
                wav_source="wavs",
                output_dir="output",
            )
            _set_active(cfg)

            # Mutation of start
            resp_start = self.client.post(
                "/api/project/active/subtitles/update",
                json={"subtitles": [{"id": 1, "start": 99.0, "final_chs": "修改中文"}]},
                headers=self.headers,
            )
            self.assertEqual(resp_start.status_code, 400)
            self.assertIn("Mutation of 'start'", resp_start.json()["error"])

            # Mutation of source_text
            resp_src = self.client.post(
                "/api/project/active/subtitles/update",
                json={"subtitles": [{"id": 1, "source_text": "Changed English", "final_chs": "修改中文"}]},
                headers=self.headers,
            )
            self.assertEqual(resp_src.status_code, 400)
            self.assertIn("Mutation of 'source_text'", resp_src.json()["error"])


if __name__ == "__main__":
    unittest.main()
