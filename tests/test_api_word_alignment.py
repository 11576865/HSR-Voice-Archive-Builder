"""Opt-in API word timing must preserve source text, privacy consent, and cache safety."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.api_word_alignment import (
    PROVIDER_ID, _transcribe_words, api_word_alignment_status,
    generate_api_word_alignments,
)
from app.credentials import TranslationCredentials


def creds(key="test-privacy-key"):
    return TranslationCredentials(
        "openai", "https://api.openai.com/v1", key, "gpt-test", "fixture"
    )


class WordApiGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.wav = self.output / "line.wav"
        self.wav.write_bytes(b"RIFF" + b"x" * 200)
        self.config = Mock(source_text_language="en")
        self.entry = {
            "index": 11, "source_text": "Hello world",
            "source_duration_seconds": 2.0,
        }

    def run_generation(self, words):
        calls = []
        with (
            patch("app.api_word_alignment.load_translation_credentials", return_value=creds()),
            patch("app.api_word_alignment._manifest_entries", return_value=[self.entry]),
            patch("app.api_word_alignment.resolve_reference_audio", return_value=(self.wav, {})),
            patch("app.api_word_alignment.import_word_alignments", side_effect=lambda output, payload: calls.append(payload) or {"ok": True}),
            patch("app.api_word_alignment.alignment_diagnostics", return_value={"total": 1, "entries": []}),
        ):
            result = generate_api_word_alignments(
                self.config, self.output, item_ids=["11"],
                transcribe=lambda path, original, language, **kwargs: words,
            )
        return result, calls

    def test_exact_source_word_timing_is_only_importable_path(self):
        good = [
            {"word": "Hello", "start": 0.1, "end": 0.6},
            {"word": " world", "start": 0.7, "end": 1.7},
        ]
        result, uploads = self.run_generation(good)
        self.assertEqual(result["generated_count"], 1)
        self.assertFalse(result["failed"])
        self.assertEqual(uploads[0]["alignments"][0]["provider"], PROVIDER_ID)
        self.assertEqual(uploads[0]["alignments"][0]["words"], good)

    def test_mismatched_transcription_does_not_write_cache(self):
        bad = [
            {"word": "Goodbye", "start": 0, "end": .6},
            {"word": "world", "start": .7, "end": 1.7},
        ]
        result, writes = self.run_generation(bad)
        self.assertEqual(result["generated_count"], 0)
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("text_coverage_mismatch", result["failed"][0]["reason"])
        self.assertEqual(writes, [])

    def test_no_implicit_upload_of_project_and_bounded_selection(self):
        for values in ([], ["1"] * 6, None):
            with self.subTest(values=values), self.assertRaises(ValueError):
                generate_api_word_alignments(self.config, self.output, item_ids=values)

    def test_absent_private_key_blocks_before_reading_audio(self):
        with patch("app.api_word_alignment.load_translation_credentials", return_value=creds("")):
            with self.assertRaisesRegex(ValueError, "API Key"):
                generate_api_word_alignments(self.config, self.output, item_ids=["11"])

    def test_configured_never_claims_a_successful_connection(self):
        with patch("app.api_word_alignment.load_translation_credentials", return_value=creds()):
            info = api_word_alignment_status()
        self.assertTrue(info["configured"])
        self.assertFalse(info["verified"])
        self.assertTrue(info["potential_cost"])
        self.assertTrue(info["uploads_audio"])
        self.assertNotIn("test-privacy-key", json.dumps(info))

    def test_multipart_requests_word_level_verbose_json_and_protects_key(self):
        requests = []
        class FakeStream:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return None
            def read(self, _limit):
                return json.dumps({
                    "words": [{"word": "Hello", "start": 0.1, "end": 1.2}],
                }).encode("utf-8")
        def fake_open(request, timeout):
            requests.append((request, timeout))
            return FakeStream()
        with patch("app.api_word_alignment.urlopen", side_effect=fake_open):
            words = _transcribe_words(
                self.wav, "Hello", "en", api_key="secret-private-key",
                base_url="https://api.openai.com/v1",
            )
        self.assertEqual(words[0]["word"], "Hello")
        req, timeout = requests[0]
        self.assertTrue(req.full_url.endswith("/audio/transcriptions"))
        self.assertEqual(req.get_method(), "POST")
        self.assertIn(b'word', req.data)
        self.assertIn(b'verbose_json', req.data)
        self.assertIn(b'whisper-1', req.data)
        self.assertIn(b'Hello', req.data)
        self.assertEqual(timeout, 60)
        self.assertNotIn("secret-private-key", req.full_url)

    def test_both_web_servers_require_explicit_upload_consent(self):
        root = Path(__file__).resolve().parents[1]
        for path in ("app/server.py", "app/lite_server.py"):
            with self.subTest(path=path):
                code = (root / path).read_text("utf-8")
                self.assertIn('"whisper-api-words"', code)
                self.assertIn("confirm_audio_upload", code)
                self.assertIn("generate_api_word_alignments", code)
        lite=(root / "app/lite_server.py").read_text("utf-8")
        get_handler=lite.split("def _handle_api_get(self, path: str) -> None:",1)[1].split("def _handle_api_post(self, path: str, data: dict[str, str]) -> None:",1)[0]
        post_handler=lite.split("def _handle_api_post(self, path: str, data: dict[str, str]) -> None:",1)[1]
        self.assertNotIn('path.endswith("/word-alignments/generate")',get_handler)
        self.assertIn('path.endswith("/word-alignments/generate")',post_handler)
        self.assertIn('"api_provider": api_word_alignment_status()',get_handler)
        html = (root / "app/static/index.html").read_text("utf-8")
        self.assertIn('id="generateApiWordAlignmentBtn"', html)
        self.assertIn("window.confirm(", html)
        self.assertIn("confirm_audio_upload:true", html)
        self.assertIn("item_ids:[itemId]", html)


if __name__ == "__main__":
    unittest.main()
