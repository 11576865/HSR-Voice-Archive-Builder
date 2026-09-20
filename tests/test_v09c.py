from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from app.translation_runtime import (
    TranslationBudgetExceeded,
    TranslationUsageLedger,
    estimate_request_tokens,
    parse_usage,
)
from app.translator import (
    HTTPResponse,
    OpenAIResponsesHTTPClient,
    ensure_translation_capability,
    translate_records,
)


class FakeResponsesClient:
    def __init__(self, *, provider: str = "vapi", base_url: str = "https://api.gpt.ge/v1") -> None:
        self.provider = provider
        self.base_url = base_url
        self.responses = self
        self.calls = 0

    def create(self, **payload):
        self.calls += 1
        requested = json.loads(payload["input"].split("\n\nInput JSON:\n", 1)[1])
        translations = [
            {"id": row["id"], "chinese": "测试译文"}
            for row in requested
        ]
        return HTTPResponse(
            output_text=json.dumps({"translations": translations}, ensure_ascii=False),
            raw={
                "status": "completed",
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "total_tokens": 150,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens_details": {"reasoning_tokens": 5},
                },
            },
        )


class ErrorOpener:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        raise self.error


class V09CTranslationRuntimeTests(unittest.TestCase):
    def test_parse_current_responses_usage(self) -> None:
        usage = parse_usage({
            "usage": {
                "input_tokens": 101,
                "output_tokens": 22,
                "total_tokens": 123,
                "input_tokens_details": {"cached_tokens": 40},
                "output_tokens_details": {"reasoning_tokens": 7},
            }
        })
        self.assertEqual(usage["input_tokens"], 101)
        self.assertEqual(usage["output_tokens"], 22)
        self.assertEqual(usage["total_tokens"], 123)
        self.assertEqual(usage["cached_input_tokens"], 40)
        self.assertEqual(usage["reasoning_tokens"], 7)

    def test_translate_records_reports_usage_to_callback(self) -> None:
        client = FakeResponsesClient()
        observed = []
        rows = translate_records(
            [{"id": "a.wav", "english": "Hello."}],
            client=client,
            usage_callback=observed.append,
        )
        self.assertEqual(rows[0]["id"], "a.wav")
        self.assertEqual(observed[0]["total_tokens"], 150)

    def test_401_is_not_retried(self) -> None:
        headers = Message()
        error = urllib.error.HTTPError(
            "https://api.openai.com/v1/responses",
            401,
            "Unauthorized",
            headers,
            io.BytesIO(b'{"error":{"message":"bad key"}}'),
        )
        opener = ErrorOpener(error)
        client = OpenAIResponsesHTTPClient(
            "secret",
            opener=opener,
            sleeper=lambda _: None,
            max_retries=5,
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            client.create(model="gpt-5.6-sol", input="x")
        self.assertEqual(opener.calls, 1)

    def test_token_budget_blocks_before_next_request(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ledger = TranslationUsageLedger(
                Path(td) / "usage.json",
                identity={
                    "provider": "vapi",
                    "base_url": "https://api.gpt.ge/v1",
                    "model": "gpt-5.6-sol",
                    "target_fingerprint": "abc",
                },
                estimate={},
                token_budget=200,
            )
            ledger.record(
                "first",
                {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "total_tokens": 150,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                },
            )
            with self.assertRaisesRegex(TranslationBudgetExceeded, "token budget"):
                ledger.check_before_request(
                    {"input_tokens": 40, "output_tokens": 20, "total_tokens": 60},
                    phase="second",
                )

    def test_unknown_relay_price_refuses_usd_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(
                "os.environ",
                {
                    "HSR_TRANSLATION_INPUT_USD_PER_MTOK": "",
                    "HSR_TRANSLATION_OUTPUT_USD_PER_MTOK": "",
                },
                clear=False,
            ):
                ledger = TranslationUsageLedger(
                    Path(td) / "usage.json",
                    identity={
                        "provider": "vapi",
                        "base_url": "https://api.gpt.ge/v1",
                        "model": "gpt-5.6-sol",
                        "target_fingerprint": "abc",
                    },
                    estimate={},
                    usd_budget=1.0,
                )
                with self.assertRaisesRegex(TranslationBudgetExceeded, "pricing is unknown"):
                    ledger.check_before_request(
                        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                        phase="translation",
                    )

    def test_capability_cache_reuses_same_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "capabilities.json"
            client = FakeResponsesClient()
            with patch("app.translation_runtime.CAPABILITY_CACHE_FILE", cache):
                first = ensure_translation_capability(
                    "gpt-5.6-sol",
                    client=client,
                )
                second = ensure_translation_capability(
                    "gpt-5.6-sol",
                    client=client,
                )
                third = ensure_translation_capability(
                    "gpt-5.6-luna",
                    client=client,
                )
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertFalse(third["cached"])
            self.assertEqual(client.calls, 2)

    def test_request_estimate_is_explicitly_nonzero(self) -> None:
        estimate = estimate_request_tokens(
            [{"id": "a.wav", "english": "A short sentence."}]
        )
        self.assertGreater(estimate["input_tokens"], 0)
        self.assertGreater(estimate["output_tokens"], 0)
        self.assertEqual(
            estimate["total_tokens"],
            estimate["input_tokens"] + estimate["output_tokens"],
        )


if __name__ == "__main__":
    unittest.main()
