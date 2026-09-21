from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import credentials
from app.pipeline import (
    _load_translation_checkpoint,
    _write_translation_checkpoint,
)
from app.preflight import dependency_status
from app.project import ProjectConfig
from app.translator import OpenAIResponsesHTTPClient


class V07ProviderTests(unittest.TestCase):
    def test_new_projects_default_to_qwen_mt_plus(self) -> None:
        config = ProjectConfig(
            schema_version=1,
            name="test",
            root="/tmp/test",
            index_csv="index.csv",
            wav_source="wavs",
        )
        self.assertEqual(config.translation_model, "qwen3.7-plus")

    def test_vapi_base_url_targets_responses_endpoint(self) -> None:
        client = OpenAIResponsesHTTPClient(
            "test-key",
            base_url="https://api.gpt.ge/v1/",
            provider="vapi",
        )
        self.assertEqual(client.base_url, "https://api.gpt.ge/v1")
        self.assertEqual(client.responses_url, "https://api.gpt.ge/v1/responses")
        self.assertEqual(client.provider, "vapi")

    def test_base_url_rejects_remote_plain_http_and_credentials(self) -> None:
        with self.assertRaises(ValueError):
            credentials.normalize_base_url("http://api.gpt.ge/v1")
        with self.assertRaises(ValueError):
            credentials.normalize_base_url("https://user:pass@api.gpt.ge/v1")
        self.assertEqual(
            credentials.normalize_base_url("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434/v1",
        )

    def test_saved_vapi_credentials_are_loaded_without_openai_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            cred_file = state / "translation_credentials.json"
            with (
                patch.object(credentials, "STATE_DIR", state),
                patch.object(credentials, "CREDENTIALS_FILE", cred_file),
                patch.dict(
                    os.environ,
                    {
                        "HSR_TRANSLATION_PROVIDER": "",
                        "HSR_TRANSLATION_BASE_URL": "",
                        "HSR_TRANSLATION_API_KEY": "",
                        "OPENAI_API_KEY": "",
                    },
                    clear=False,
                ),
            ):
                credentials.save_translation_credentials(
                    "vapi",
                    "https://api.gpt.ge/v1",
                    "vapi-secret",
                )
                loaded = credentials.load_translation_credentials()
                status = credentials.credentials_status()

            self.assertEqual(loaded.provider, "vapi")
            self.assertEqual(loaded.base_url, "https://api.gpt.ge/v1")
            self.assertEqual(loaded.api_key, "vapi-secret")
            self.assertTrue(status["configured"])
            self.assertNotIn("api_key", status)
            if os.name != "nt":
                self.assertEqual(cred_file.stat().st_mode & 0o777, 0o600)

    def test_environment_provider_does_not_reuse_mismatched_saved_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            cred_file = state / "translation_credentials.json"
            cred_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "openai",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "old",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(credentials, "STATE_DIR", state),
                patch.object(credentials, "CREDENTIALS_FILE", cred_file),
                patch.dict(
                    os.environ,
                    {
                        "HSR_TRANSLATION_PROVIDER": "vapi",
                        "HSR_TRANSLATION_BASE_URL": "",
                        "HSR_TRANSLATION_API_KEY": "new",
                    },
                    clear=False,
                ),
            ):
                provider, base_url = credentials.translation_identity()
                loaded = credentials.load_translation_credentials()

            self.assertEqual(provider, "vapi")
            self.assertEqual(base_url, "https://api.gpt.ge/v1")
            self.assertEqual(loaded.api_key, "new")

    def test_checkpoint_isolated_by_provider_and_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "checkpoint.json"
            rows = {
                "a.wav": {
                    "english_sha256": "abc",
                    "chinese": "甲",
                }
            }
            _write_translation_checkpoint(
                path,
                "gpt-5.6-luna",
                "vapi",
                "https://api.gpt.ge/v1",
                rows,
            )

            same = _load_translation_checkpoint(
                path,
                "gpt-5.6-luna",
                "vapi",
                "https://api.gpt.ge/v1",
            )
            other_provider = _load_translation_checkpoint(
                path,
                "gpt-5.6-luna",
                "openai",
                "https://api.openai.com/v1",
            )
            other_url = _load_translation_checkpoint(
                path,
                "gpt-5.6-luna",
                "vapi",
                "https://api1.v3.cm/v1",
            )

            self.assertEqual(same, rows)
            self.assertEqual(other_provider, {})
            self.assertEqual(other_url, {})

    def test_v06_checkpoint_reuse_only_for_official_openai(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "checkpoint.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "model": "gpt-5.6-luna",
                        "records": {"a.wav": {"english_sha256": "x", "chinese": "甲"}},
                    }
                ),
                encoding="utf-8",
            )
            official = _load_translation_checkpoint(
                path,
                "gpt-5.6-luna",
                "openai",
                "https://api.openai.com/v1",
            )
            relay = _load_translation_checkpoint(
                path,
                "gpt-5.6-luna",
                "vapi",
                "https://api.gpt.ge/v1",
            )
            self.assertIn("a.wav", official)
            self.assertEqual(relay, {})

    def test_preflight_reports_vapi_without_exposing_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            cred_file = state / "translation_credentials.json"
            with (
                patch.object(credentials, "STATE_DIR", state),
                patch.object(credentials, "CREDENTIALS_FILE", cred_file),
                patch.dict(
                    os.environ,
                    {
                        "HSR_TRANSLATION_PROVIDER": "vapi",
                        "HSR_TRANSLATION_BASE_URL": "https://api.gpt.ge/v1",
                        "HSR_TRANSLATION_API_KEY": "hidden-test-key",
                    },
                    clear=False,
                ),
            ):
                status = dependency_status()

            self.assertEqual(status["translation_provider"], "vapi")
            self.assertEqual(status["translation_base_url"], "https://api.gpt.ge/v1")
            self.assertTrue(status["translation_api_key_configured"])
            self.assertNotIn("hidden-test-key", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
