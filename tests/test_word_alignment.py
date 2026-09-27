from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.project import create_project
from app.security import api_token
from app.server import app, _clear_active, _set_active
from app.local_word_alignment import local_alignment_provider_status
from app.word_alignment import (
    alignment_diagnostics,
    import_word_alignments,
    validate_word_alignment,
)


class TestWordAlignment(unittest.TestCase):
    def setUp(self) -> None:
        _clear_active()
        self.client = TestClient(app, base_url="http://127.0.0.1:8765")
        self.headers = {"X-HSR-Token": api_token()}

    def tearDown(self) -> None:
        _clear_active()

    def test_validate_complete_monotonic_alignment(self) -> None:
        result = validate_word_alignment(
            "Hello world",
            2.0,
            [
                {"word": "Hello ", "start": 0.0, "end": 0.5, "confidence": 0.9},
                {"word": "world", "start": 0.55, "end": 1.2, "confidence": 0.8},
            ],
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["coverage_percent"], 100.0)
        self.assertEqual(result["word_count"], 2)
        self.assertAlmostEqual(result["mean_confidence"], 0.85)

    def test_validate_rejects_incomplete_or_non_monotonic_alignment(self) -> None:
        incomplete = validate_word_alignment(
            "Hello world",
            2.0,
            [{"word": "Hello", "start": 0.0, "end": 0.5}],
        )
        self.assertFalse(incomplete["valid"])
        self.assertEqual(incomplete["reason"], "text_coverage_mismatch")

        non_monotonic = validate_word_alignment(
            "Hello world",
            2.0,
            [
                {"word": "Hello ", "start": 0.4, "end": 0.8},
                {"word": "world", "start": 0.2, "end": 1.0},
            ],
        )
        self.assertFalse(non_monotonic["valid"])
        self.assertEqual(non_monotonic["reason"], "non_monotonic")

    def _make_project(self, root: Path):
        output = root / "output"
        output.mkdir(parents=True, exist_ok=True)
        manifest = {
            "entries": [
                {
                    "index": 1,
                    "filename": "line.wav",
                    "source_member_id": "chapter_a/line.wav",
                    "source_text": "Hello world",
                    "target_text": "你好世界",
                    "start_seconds": 0.0,
                    "audio_end_seconds": 2.0,
                    "display_end_seconds": 2.0,
                    "source_duration_seconds": 2.0,
                }
            ]
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        config = create_project(
            root,
            name="alignment-test",
            index_csv="index.csv",
            wav_source="wavs",
            output_dir="output",
        )
        return config, output

    def test_local_provider_status_reports_missing_optional_dependencies(self) -> None:
        def fake_find_spec(name: str):
            return None if name == "whisperx" else object()

        with patch("app.local_word_alignment.importlib.util.find_spec", side_effect=fake_find_spec):
            status = local_alignment_provider_status()

        self.assertFalse(status["available"])
        self.assertIn("whisperx", status["missing_packages"])
        self.assertFalse(status["network_inference"])
        self.assertIn("pip install whisperx", status["install_hint"])

    def test_alignment_diagnostics_expose_local_provider_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, _output = self._make_project(root)
            _set_active(config)
            with patch(
                "app.server.local_alignment_provider_status",
                return_value={
                    "provider": "whisperx-local",
                    "available": False,
                    "missing_packages": ["whisperx"],
                },
            ):
                response = self.client.get(
                    "/api/project/active/word-alignments",
                    headers=self.headers,
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["local_provider"]["available"])
        self.assertEqual(body["local_provider"]["provider"], "whisperx-local")

    def test_local_generation_route_starts_background_job_when_provider_available(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, _output = self._make_project(root)
            _set_active(config)
            with patch(
                "app.server.local_alignment_provider_status",
                return_value={
                    "provider": "whisperx-local",
                    "available": True,
                    "missing_packages": [],
                },
            ), patch(
                "app.server.create_job",
                return_value=SimpleNamespace(id="align-job"),
            ) as create_job_mock:
                response = self.client.post(
                    "/api/project/active/word-alignments/generate",
                    json={"force": False},
                    headers=self.headers,
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"], "align-job")
        create_job_mock.assert_called_once()
        self.assertEqual(create_job_mock.call_args.args[0], "word-align")
        self.assertTrue(create_job_mock.call_args.kwargs["with_progress"])

    def test_local_generation_route_refuses_missing_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, _output = self._make_project(root)
            _set_active(config)
            with patch(
                "app.server.local_alignment_provider_status",
                return_value={
                    "provider": "whisperx-local",
                    "available": False,
                    "missing_packages": ["whisperx"],
                },
            ):
                response = self.client.post(
                    "/api/project/active/word-alignments/generate",
                    json={},
                    headers=self.headers,
                )

        self.assertEqual(response.status_code, 400)
        self.assertIn("whisperx", response.json()["error"].lower())

    def test_import_cache_is_non_destructive_and_decorates_subtitle_api(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, output = self._make_project(root)
            _set_active(config)

            payload = {
                "alignments": [
                    {
                        "id": 1,
                        "provider": "unit-test",
                        "words": [
                            {"word": "Hello ", "start": 0.0, "end": 0.5},
                            {"word": "world", "start": 0.55, "end": 1.2},
                        ],
                    }
                ]
            }
            response = self.client.post(
                "/api/project/active/word-alignments",
                json=payload,
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["result"]["imported_count"], 1)
            self.assertEqual(body["result"]["rejected_count"], 0)
            self.assertTrue((output / "word_alignments.json").is_file())

            canonical = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("word_alignments", canonical["entries"][0])

            diagnostics = self.client.get(
                "/api/project/active/word-alignments",
                headers=self.headers,
            ).json()["diagnostics"]
            self.assertEqual(diagnostics["usable"], 1)
            self.assertEqual(diagnostics["missing"], 0)
            self.assertEqual(diagnostics["invalid"], 0)

            alignment = self.client.get(
                "/api/project/active/word-alignments/1",
                headers=self.headers,
            ).json()["alignment"]
            self.assertTrue(alignment["valid"])
            self.assertEqual(alignment["provider"], "unit-test")
            self.assertEqual(len(alignment["words"]), 2)
            self.assertEqual(alignment["words"][0]["word"], "Hello ")

            subtitles = self.client.get(
                "/api/project/active/subtitles",
                headers=self.headers,
            ).json()["subtitles"]
            self.assertTrue(subtitles[0]["word_alignment_ready"])
            self.assertEqual(subtitles[0]["word_alignment"]["source"], "cache")
            self.assertEqual(subtitles[0]["word_alignment"]["provider"], "unit-test")

    def test_stale_cache_is_not_reused_after_source_text_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _config, output = self._make_project(root)
            import_word_alignments(
                output,
                {
                    "alignments": [
                        {
                            "id": 1,
                            "words": [
                                {"word": "Hello ", "start": 0.0, "end": 0.5},
                                {"word": "world", "start": 0.55, "end": 1.2},
                            ],
                        }
                    ]
                },
            )
            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["entries"][0]["source_text"] = "Hello changed world"
            (output / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            diagnostics = alignment_diagnostics(output)
            self.assertEqual(diagnostics["usable"], 0)
            self.assertEqual(diagnostics["missing"], 1)
            self.assertEqual(diagnostics["entries"][0]["reason"], "stale_cache")

    def test_import_rejects_text_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _config, output = self._make_project(root)
            result = import_word_alignments(
                output,
                {
                    "alignments": [
                        {
                            "id": 1,
                            "words": [
                                {"word": "Wrong", "start": 0.0, "end": 0.5}
                            ],
                        }
                    ]
                },
            )
            self.assertEqual(result["imported_count"], 0)
            self.assertEqual(result["rejected_count"], 1)
            self.assertEqual(
                result["rejected"][0]["reason"],
                "text_coverage_mismatch",
            )


if __name__ == "__main__":
    unittest.main()
