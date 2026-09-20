from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
DEFAULT_MAX_RETRIES = 5
DEFAULT_TIMEOUT_SECONDS = 120.0
RESPONSES_URL = "https://api.openai.com/v1/responses"
RETRYABLE_HTTP = {408, 409, 429}


def _chunks(records: list[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    if size < 1:
        raise ValueError("batch_size must be >= 1")
    for i in range(0, len(records), size):
        yield records[i:i + size]


def _retry_after_seconds(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(str(raw))
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


def _extract_api_error(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
        err = payload.get("error", {})
        if isinstance(err, dict):
            message = str(err.get("message", "")).strip()
            code = str(err.get("code", "")).strip()
            if message and code:
                return f"{message} ({code})"
            if message:
                return message
    except Exception:
        pass
    text = body.decode("utf-8", "replace").strip()
    return text[:1000] if text else "no response body"


def _extract_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct

    pieces: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "output_text" and isinstance(part.get("text"), str):
                pieces.append(part["text"])
            elif kind == "refusal":
                refusal = str(part.get("refusal", "")).strip()
                raise RuntimeError(
                    "OpenAI response was refused"
                    + (f": {refusal}" if refusal else "")
                )
    text = "".join(pieces)
    if not text.strip():
        status = payload.get("status")
        error = payload.get("error")
        raise RuntimeError(
            f"OpenAI response contained no output text (status={status!r}, error={error!r})"
        )
    return text


@dataclass
class HTTPResponse:
    output_text: str
    raw: dict[str, Any]


class OpenAIResponsesHTTPClient:
    """Dependency-free OpenAI Responses API client.

    Uses only Python standard library modules so the same translation path works
    on desktop and Termux/Android without jiter, pydantic-core, maturin, or Rust.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        opener: Any | None = None,
        sleeper: Any = time.sleep,
        random_fn: Any = random.random,
    ) -> None:
        if not api_key.strip():
            raise RuntimeError("OPENAI_API_KEY is not set")
        self.api_key = api_key.strip()
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.opener = opener or urllib.request.build_opener()
        self.sleeper = sleeper
        self.random_fn = random_fn
        self.responses = self

    def _delay(self, attempt: int, headers: Any = None) -> float:
        explicit = _retry_after_seconds(headers)
        if explicit is not None:
            return min(explicit, 60.0)
        base = min(0.5 * (2 ** attempt), 8.0)
        return base + (self.random_fn() * min(0.25, base * 0.25))

    def create(self, **payload: Any) -> HTTPResponse:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            RESPONSES_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "HSR-Voice-Archive-Builder/0.6",
            },
        )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise RuntimeError("OpenAI API returned a non-object JSON response")
                status = data.get("status")
                if status not in (None, "completed"):
                    raise RuntimeError(
                        f"OpenAI response did not complete (status={status!r}, "
                        f"error={data.get('error')!r}, incomplete={data.get('incomplete_details')!r})"
                    )
                return HTTPResponse(output_text=_extract_output_text(data), raw=data)

            except urllib.error.HTTPError as exc:
                error_body = exc.read()
                retryable = exc.code in RETRYABLE_HTTP or 500 <= exc.code <= 599
                message = _extract_api_error(error_body)
                last_error = RuntimeError(f"OpenAI HTTP {exc.code}: {message}")
                if not retryable or attempt >= self.max_retries:
                    raise last_error from exc
                self.sleeper(self._delay(attempt, exc.headers))

            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = RuntimeError(f"OpenAI network error: {exc}")
                if attempt >= self.max_retries:
                    raise last_error from exc
                self.sleeper(self._delay(attempt))

            except json.JSONDecodeError as exc:
                raise RuntimeError("OpenAI API returned invalid JSON") from exc

        raise last_error or RuntimeError("OpenAI request failed")


def make_client() -> OpenAIResponsesHTTPClient:
    return OpenAIResponsesHTTPClient(
        os.environ.get("OPENAI_API_KEY", ""),
        max_retries=DEFAULT_MAX_RETRIES,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def translate_records(
    records: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    glossary: dict[str, str] | None = None,
    *,
    client: Any | None = None,
) -> list[dict[str, str]]:
    """Translate one batch through the OpenAI Responses REST API.

    Each input must contain id and english. The API key is read only from
    OPENAI_API_KEY. Structured Outputs are requested with JSON Schema and IDs
    must round-trip exactly before any translation is accepted.
    """
    if not records:
        return []
    client = client or make_client()

    schema = {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "chinese": {"type": "string"},
                    },
                    "required": ["id", "chinese"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    }
    glossary_text = ""
    if glossary:
        glossary_text = "\nOfficial/preferred terminology:\n" + "\n".join(
            f"- {src} => {dst}" for src, dst in glossary.items()
        )
    prompt = (
        "Translate the following Honkai: Star Rail English voice lines into Simplified Chinese. "
        "Preserve meaning, character tone, placeholders/tags, punctuation intent, and one-to-one IDs. "
        "Do not add information. Preserve tokens such as {NICKNAME}, {M#...}{F#...}, HTML-like color tags, "
        "and RUBY tags exactly unless translating text inside the token is necessary. Use established official "
        "Chinese terminology when known. Return every input exactly once."
        + glossary_text
        + "\n\nInput JSON:\n"
        + json.dumps(records, ensure_ascii=False)
    )
    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        store=False,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "voice_translation_batch",
                "strict": True,
                "schema": schema,
            }
        },
    )
    if not getattr(response, "output_text", ""):
        raise RuntimeError("OpenAI response contained no output_text")
    data = json.loads(response.output_text)
    got = data["translations"]
    wanted_list = [r["id"] for r in records]
    got_ids = [r["id"] for r in got]
    if len(got_ids) != len(set(got_ids)):
        raise RuntimeError("Translation response contains duplicate IDs")
    if set(wanted_list) != set(got_ids) or len(got) != len(records):
        raise RuntimeError(
            f"Translation ID mismatch: missing={set(wanted_list)-set(got_ids)}, "
            f"extra={set(got_ids)-set(wanted_list)}"
        )
    by_id = {r["id"]: r for r in got}
    ordered = [by_id[i] for i in wanted_list]
    for row in ordered:
        if not str(row.get("chinese", "")).strip():
            raise RuntimeError(f"Translation response contains empty Chinese text: {row['id']}")
    return ordered


def translate_in_batches(
    records: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    batch_size: int = 80,
    glossary: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    client = make_client() if records else None
    for batch in _chunks(records, batch_size):
        out.extend(
            translate_records(
                batch,
                model=model,
                glossary=glossary,
                client=client,
            )
        )
    return out
