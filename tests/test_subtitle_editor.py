from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.project import create_project
from app.security import api_token
from app.server import app, _set_active, _clear_active
from app.subtitles import parse_time_range_str


class TestSubtitleEditor(unittest.TestCase):
    def setUp(self) -> None:
        _clear_active()
        self.token = api_token()
        self.client = TestClient(app, base_url="http://127.0.0.1:8765")
        self.headers = {"X-HSR-Token": self.token}

    def tearDown(self) -> None:
        _clear_active()

    def test_parse_time_range_str(self) -> None:
        s, e = parse_time_range_str("00:05 - 02:30")
        self.assertEqual(s, 5.0)
        self.assertEqual(e, 150.0)

        s_dash, e_dash = parse_time_range_str("03:20 -- 05:40")
        self.assertEqual(s_dash, 200.0)
        self.assertEqual(e_dash, 340.0)

        s_tilde, e_tilde = parse_time_range_str("01:00 ~ 02:00")
        self.assertEqual(s_tilde, 60.0)
        self.assertEqual(e_tilde, 120.0)

        s_invalid, e_invalid = parse_time_range_str("invalid")
        self.assertIsNone(s_invalid)
        self.assertIsNone(e_invalid)

    def test_subtitle_item_data_structure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "output"
            out.mkdir(parents=True, exist_ok=True)
            manifest = {
                "report": {},
                "entries": [
                    {
                        "index": 1,
                        "start_seconds": 2.5,
                        "display_end_seconds": 6.0,
                        "source_text": "May this journey lead us starward.",
                        "target_text": "愿此行，终抵群星。",
                        "target_text_source": "official_target_lab",
                    }
                ],
            }
            (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            cfg = create_project(
                root,
                name="test_proj_struct",
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
            self.assertEqual(len(subs), 1)

            item = subs[0]
            required_keys = {
                "id", "start", "end", "source_language",
                "source_text", "official_chs", "api_chs", "final_chs", "modified"
            }
            self.assertTrue(required_keys.issubset(set(item.keys())))
            self.assertEqual(item["id"], 1)
            self.assertEqual(item["start"], 2.5)
            self.assertEqual(item["end"], 6.0)
            self.assertEqual(item["source_text"], "May this journey lead us starward.")
            self.assertEqual(item["official_chs"], "愿此行，终抵群星。")
            self.assertEqual(item["api_chs"], "")
            self.assertEqual(item["final_chs"], "愿此行，终抵群星。")
            self.assertFalse(item["modified"])

    def test_query_filters_q_time_and_selector(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "output"
            out.mkdir(parents=True, exist_ok=True)
            manifest = {
                "entries": [
                    {
                        "index": 1,
                        "start_seconds": 0.0,
                        "display_end_seconds": 3.0,
                        "source_text": "March 7th speaking",
                        "target_text": "三月七在说话",
                        "target_text_source": "official_target_lab",
                    },
                    {
                        "index": 2,
                        "start_seconds": 4.0,
                        "display_end_seconds": 8.0,
                        "source_text": "Dan Heng speaking",
                        "target_text": "丹恒在说话",
                        "target_text_source": "api:openai:gpt-4",
                        "reference_text": "",
                    },
                    {
                        "index": 3,
                        "start_seconds": 10.0,
                        "display_end_seconds": 15.0,
                        "source_text": "Welt speaking",
                        "target_text": "瓦尔特在说话",
                        "target_text_source": "api:openai:gpt-4",
                    },
                ]
            }
            (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            cfg = create_project(
                root,
                name="test_proj_selectors",
                index_csv="index.csv",
                wav_source="wavs",
                output_dir="output",
            )
            _set_active(cfg)

            # Filter by character / text 'q'
            resp_q = self.client.get("/api/project/active/subtitles?q=丹恒", headers=self.headers)
            self.assertEqual(resp_q.status_code, 200)
            subs_q = resp_q.json()["subtitles"]
            self.assertEqual(len(subs_q), 1)
            self.assertEqual(subs_q[0]["id"], 2)

            # Filter by time range string 'MM:SS - MM:SS'
            resp_tr = self.client.get("/api/project/active/subtitles?time_range=00:03.5 - 00:09", headers=self.headers)
            self.assertEqual(resp_tr.status_code, 200)
            subs_tr = resp_tr.json()["subtitles"]
            self.assertEqual(len(subs_tr), 1)
            self.assertEqual(subs_tr[0]["id"], 2)

            # Selector: official
            resp_off = self.client.get("/api/project/active/subtitles?selector=official", headers=self.headers)
            self.assertEqual(resp_off.status_code, 200)
            subs_off = resp_off.json()["subtitles"]
            self.assertEqual(len(subs_off), 1)
            self.assertEqual(subs_off[0]["id"], 1)

            # Selector: api
            resp_api = self.client.get("/api/project/active/subtitles?selector=api", headers=self.headers)
            self.assertEqual(resp_api.status_code, 200)
            subs_api = resp_api.json()["subtitles"]
            self.assertEqual(len(subs_api), 2)
            self.assertEqual({s["id"] for s in subs_api}, {2, 3})

    def test_post_update_editing_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "output"
            out.mkdir(parents=True, exist_ok=True)
            manifest = {
                "entries": [
                    {
                        "index": 1,
                        "start_seconds": 1.0,
                        "display_end_seconds": 5.0,
                        "source_text": "Rules are made to be broken!",
                        "target_text": "规矩就是用来打破的！",
                        "target_text_source": "api:openai:gpt-4",
                        "english": "Rules are made to be broken!",
                        "chinese": "规矩就是用来打破的！",
                    }
                ]
            }
            (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            cfg = create_project(
                root,
                name="test_proj_post_update",
                index_csv="index.csv",
                wav_source="wavs",
                output_dir="output",
            )
            _set_active(cfg)

            # Post update editing final_chs
            update_payload = {
                "subtitles": [
                    {
                        "id": 1,
                        "final_chs": "规则，就是用来打破的！",
                    }
                ]
            }
            resp = self.client.post("/api/project/active/subtitles", json=update_payload, headers=self.headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["result"]["updated_count"], 1)

            # Verify overrides file created
            overrides_file = out / "subtitles_overrides.json"
            self.assertTrue(overrides_file.is_file())
            overrides = json.loads(overrides_file.read_text(encoding="utf-8"))
            self.assertEqual(overrides["1"]["final_chs"], "规则，就是用来打破的！")
            self.assertTrue(overrides["1"]["modified"])

            # Verify ASS and SRT files regenerated
            ass_file = out / "HSR_Voice_Archive.ass"
            srt_file = out / "HSR_Voice_Archive.srt"
            self.assertTrue(ass_file.is_file())
            self.assertTrue(srt_file.is_file())

            ass_content = ass_file.read_text(encoding="utf-8-sig")
            srt_content = srt_file.read_text(encoding="utf-8-sig")
            self.assertIn("规则，就是用来打破的！", ass_content)
            self.assertIn("规则，就是用来打破的！", srt_content)

            # Fetch via GET to verify timestamps and source text are NOT modified
            resp_get = self.client.get("/api/project/active/subtitles", headers=self.headers)
            self.assertEqual(resp_get.status_code, 200)
            subs = resp_get.json()["subtitles"]
            self.assertEqual(len(subs), 1)
            item = subs[0]
            self.assertEqual(item["id"], 1)
            self.assertEqual(item["start"], 1.0)
            self.assertEqual(item["end"], 5.0)
            self.assertEqual(item["source_text"], "Rules are made to be broken!")
            self.assertEqual(item["final_chs"], "规则，就是用来打破的！")
            self.assertTrue(item["modified"])


if __name__ == "__main__":
    unittest.main()
