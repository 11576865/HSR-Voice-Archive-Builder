from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from app.translator import (
    OpenAIResponsesHTTPClient,
    _extract_output_text,
    _parse_json_output,
    make_client,
    translate_records,
)


class FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.payload


class SequenceOpener:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        item = self.sequence.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def completed_response(text: str) -> dict:
    return {
        "id": "resp_test",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ],
    }


def chat_completion_response(text: str) -> dict:
    return {
        "id": "chatcmpl_test",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": text},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class V06RestTranslationTests(unittest.TestCase):
    def test_json_parser_accepts_provider_markdown_fence(self) -> None:
        parsed = _parse_json_output(
            '```json\n{"translations": []}\n```', context="test"
        )
        self.assertEqual(parsed, {"translations": []})

    def test_model_studio_workspace_uses_chat_completions_adapter(self) -> None:
        output = {"translations": [{"id": "a.wav", "chinese": "第一句"}]}
        opener = SequenceOpener([
            FakeHTTPResponse(chat_completion_response(json.dumps(output, ensure_ascii=False)))
        ])
        client = OpenAIResponsesHTTPClient(
            "secret-test-key",
            base_url="https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            provider="custom",
            opener=opener,
        )

        rows = translate_records(
            [{"id": "a.wav", "english": "First."}], client=client
        )

        self.assertEqual(rows[0]["chinese"], "第一句")
        request = opener.requests[0][0]
        self.assertEqual(
            request.full_url,
            "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertEqual(sent["messages"][1]["role"], "user")

    def test_extract_output_text_from_raw_responses_payload(self) -> None:
        payload = completed_response('{"translations":[]}')
        self.assertEqual(_extract_output_text(payload), '{"translations":[]}')

    def test_rest_client_posts_to_responses_api_without_sdk(self) -> None:
        payload = completed_response('{"translations":[{"id":"a","chinese":"甲"}]}')
        opener = SequenceOpener([FakeHTTPResponse(payload)])
        client = OpenAIResponsesHTTPClient(
            "secret-test-key",
            opener=opener,
            sleeper=lambda _: None,
            random_fn=lambda: 0.0,
        )

        response = client.responses.create(
            model="gpt-5.6-luna",
            input="translate",
            store=False,
        )

        self.assertIn('"translations"', response.output_text)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(timeout, 120.0)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-test-key")
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent["model"], "gpt-5.6-luna")
        self.assertFalse(sent["store"])

    def test_rest_client_retries_429_and_honors_retry_after(self) -> None:
        headers = Message()
        headers["Retry-After"] = "0"
        error_body = io.BytesIO(
            json.dumps(
                {"error": {"message": "rate limited", "code": "rate_limit_exceeded"}}
            ).encode("utf-8")
        )
        first = urllib.error.HTTPError(
            "https://api.openai.com/v1/responses",
            429,
            "Too Many Requests",
            headers,
            error_body,
        )
        opener = SequenceOpener(
            [first, FakeHTTPResponse(completed_response('{"translations":[]}'))]
        )
        sleeps = []
        client = OpenAIResponsesHTTPClient(
            "secret-test-key",
            opener=opener,
            sleeper=sleeps.append,
            random_fn=lambda: 0.0,
            max_retries=2,
        )

        result = client.create(model="gpt-5.6-luna", input="x")

        self.assertEqual(result.output_text, '{"translations":[]}')
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(sleeps, [0.0])

    def test_translate_records_keeps_structured_output_and_id_validation(self) -> None:
        output = {
            "translations": [
                {"id": "a.wav", "chinese": "第一句"},
                {"id": "b.wav", "chinese": "第二句"},
            ]
        }
        opener = SequenceOpener(
            [FakeHTTPResponse(completed_response(json.dumps(output, ensure_ascii=False)))]
        )
        client = OpenAIResponsesHTTPClient("secret-test-key", opener=opener)

        rows = translate_records(
            [
                {"id": "a.wav", "english": "First."},
                {"id": "b.wav", "english": "Second."},
            ],
            client=client,
        )

        self.assertEqual([x["id"] for x in rows], ["a.wav", "b.wav"])

    def test_translate_records_accepts_relay_bare_array(self) -> None:
        output = [
            {"id": "a.wav", "chinese": "第一句"},
            {"id": "b.wav", "chinese": "第二句"},
        ]
        opener = SequenceOpener(
            [FakeHTTPResponse(completed_response(json.dumps(output, ensure_ascii=False)))]
        )
        client = OpenAIResponsesHTTPClient("secret-test-key", opener=opener)

        rows = translate_records(
            [
                {"id": "a.wav", "english": "First."},
                {"id": "b.wav", "english": "Second."},
            ],
            client=client,
        )

        self.assertEqual([x["id"] for x in rows], ["a.wav", "b.wav"])
        self.assertEqual([x["chinese"] for x in rows], ["第一句", "第二句"])
        self.assertEqual(rows[1]["chinese"], "第二句")
        sent = json.loads(opener.requests[0][0].data.decode("utf-8"))
        self.assertEqual(sent["text"]["format"]["type"], "json_schema")
        self.assertTrue(sent["text"]["format"]["strict"])
        self.assertEqual(sent["reasoning"]["effort"], "low")

    def test_translate_records_discards_extra_provider_row_without_positional_pairing(self) -> None:
        output = {
            "translations": [
                {"id": "a.wav", "chinese": "第一句"},
                {"id": "provider-guessed.wav", "chinese": "不应采用"},
                {"id": "b.wav", "chinese": "第二句"},
            ]
        }
        opener = SequenceOpener(
            [FakeHTTPResponse(completed_response(json.dumps(output, ensure_ascii=False)))]
        )
        client = OpenAIResponsesHTTPClient("secret-test-key", opener=opener)

        rows = translate_records(
            [
                {"id": "a.wav", "english": "First."},
                {"id": "b.wav", "english": "Second."},
            ],
            client=client,
        )

        self.assertEqual([row["id"] for row in rows], ["a.wav", "b.wav"])
        self.assertEqual([row["chinese"] for row in rows], ["第一句", "第二句"])

    def test_make_client_needs_only_environment_key(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-key"}, clear=False):
            client = make_client()
        self.assertIsInstance(client, OpenAIResponsesHTTPClient)


if __name__ == "__main__":
    unittest.main()
