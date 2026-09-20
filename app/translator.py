from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

from .credentials import load_translation_credentials, normalize_base_url

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-sol")
DEFAULT_MAX_RETRIES = 5
DEFAULT_TIMEOUT_SECONDS = 120.0
RESPONSES_URL = "https://api.openai.com/v1/responses"
RETRYABLE_HTTP = {408, 409, 429}

TRANSLATION_SCHEMA = {
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


def translation_schema_fingerprint() -> str:
    encoded = json.dumps(
        TRANSLATION_SCHEMA, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
                    "Translation response was refused"
                    + (f": {refusal}" if refusal else "")
                )
    text = "".join(pieces)
    if not text.strip():
        status = payload.get("status")
        error = payload.get("error")
        raise RuntimeError(
            f"Translation API response contained no output text "
            f"(status={status!r}, error={error!r})"
        )
    return text


@dataclass
class HTTPResponse:
    output_text: str
    raw: dict[str, Any]


class OpenAIResponsesHTTPClient:
    """Dependency-free OpenAI-compatible Responses API client."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        provider: str = "openai",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        opener: Any | None = None,
        sleeper: Any = time.sleep,
        random_fn: Any = random.random,
    ) -> None:
        if not api_key.strip():
            raise RuntimeError("Translation API key is not configured")
        self.api_key = api_key.strip()
        self.base_url = normalize_base_url(base_url)
        self.provider = str(provider or "custom").strip() or "custom"
        self.responses_url = self.base_url.rstrip("/") + "/responses"
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
            self.responses_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "HSR-Voice-Archive-Builder/0.9-C",
            },
        )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise RuntimeError("Translation API returned a non-object JSON response")
                status = data.get("status")
                if status not in (None, "completed"):
                    raise RuntimeError(
                        f"Translation response did not complete (status={status!r}, "
                        f"error={data.get('error')!r}, "
                        f"incomplete={data.get('incomplete_details')!r})"
                    )
                return HTTPResponse(output_text=_extract_output_text(data), raw=data)

            except urllib.error.HTTPError as exc:
                error_body = exc.read()
                retryable = exc.code in RETRYABLE_HTTP or 500 <= exc.code <= 599
                message = _extract_api_error(error_body)
                last_error = RuntimeError(
                    f"Translation API HTTP {exc.code} via {self.provider}: {message}"
                )
                if not retryable or attempt >= self.max_retries:
                    raise last_error from exc
                self.sleeper(self._delay(attempt, exc.headers))

            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = RuntimeError(
                    f"Translation API network error via {self.provider}: {exc}"
                )
                if attempt >= self.max_retries:
                    raise last_error from exc
                self.sleeper(self._delay(attempt))

            except json.JSONDecodeError as exc:
                raise RuntimeError("Translation API returned invalid JSON") from exc

        raise last_error or RuntimeError("Translation API request failed")


def make_client() -> OpenAIResponsesHTTPClient:
    creds = load_translation_credentials()
    return OpenAIResponsesHTTPClient(
        creds.api_key,
        base_url=creds.base_url,
        provider=creds.provider,
        max_retries=DEFAULT_MAX_RETRIES,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def _translation_prompt(
    records: list[dict[str, str]],
    glossary: dict[str, str] | None,
) -> str:
    glossary_text = ""
    if glossary:
        glossary_text = "\nOfficial/preferred terminology:\n" + "\n".join(
            f"- {src} => {dst}" for src, dst in glossary.items()
        )
    return (
        "Translate the target field 'english' in each Honkai: Star Rail record into Simplified Chinese. "
        "Preserve meaning, character tone, punctuation intent, and one-to-one IDs. Do not add information. "
        "Records may include context_before/context_after; use them only to disambiguate the target and do not "
        "translate them as separate outputs. Records may also include previous_chinese and qa_issues; when present, "
        "repair the previous translation specifically for those issues. Preserve HTML-like tags and the structural "
        "form of brace control tokens such as {NICKNAME}, {M#...}{F#...}, and RUBY markers. Text payloads inside "
        "control tokens may be translated when they are user-visible, but the token type/structure must remain. "
        "Use the supplied terminology exactly when its English source term occurs in the target. "
        "Return every input ID exactly once and output only the requested Chinese target translation."
        + glossary_text
        + "\n\nInput JSON:\n"
        + json.dumps(records, ensure_ascii=False)
    )


def translate_records(
    records: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    glossary: dict[str, str] | None = None,
    *,
    client: Any | None = None,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> list[dict[str, str]]:
    """Translate one batch through an OpenAI-compatible Responses REST API."""
    if not records:
        return []
    client = client or make_client()

    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        store=False,
        input=_translation_prompt(records, glossary),
        text={
            "format": {
                "type": "json_schema",
                "name": "voice_translation_batch",
                "strict": True,
                "schema": TRANSLATION_SCHEMA,
            }
        },
    )

    if usage_callback is not None:
        from .translation_runtime import parse_usage

        usage_callback(parse_usage(getattr(response, "raw", None)))

    if not getattr(response, "output_text", ""):
        raise RuntimeError("Translation API response contained no output_text")
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


def ensure_translation_capability(
    model: str = DEFAULT_MODEL,
    *,
    client: OpenAIResponsesHTTPClient | None = None,
    force: bool = False,
    before_request_callback: Callable[[], None] | None = None,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> dict[str, object]:
    """Verify the selected route can satisfy the structured translation schema.

    A successful result is cached by provider + Base URL + model + schema
    fingerprint. Cache reuse avoids repeating the paid smoke request.
    """
    from .translation_runtime import (
        get_cached_capability,
        invalidate_capability,
        save_capability,
    )

    client = client or make_client()
    schema_fingerprint = translation_schema_fingerprint()
    identity = {
        "provider": client.provider,
        "base_url": client.base_url,
        "model": model,
        "schema_fingerprint": schema_fingerprint,
    }

    if not force:
        cached = get_cached_capability(**identity)
        if cached is not None:
            return {
                "ok": True,
                "cached": True,
                "usage_supported": bool(cached.get("usage_supported")),
                **identity,
            }

    observed_usage: dict[str, int] | None = None

    def capture(usage: dict[str, int] | None) -> None:
        nonlocal observed_usage
        observed_usage = usage
        if usage_callback is not None:
            usage_callback(usage)

    try:
        if before_request_callback is not None:
            before_request_callback()
        rows = translate_records(
            [{"id": "smoke-1", "english": "The story's not finished."}],
            model=model,
            client=client,
            usage_callback=capture,
        )
        if len(rows) != 1 or rows[0]["id"] != "smoke-1":
            raise RuntimeError("Translation capability smoke test returned an invalid record")
    except Exception:
        invalidate_capability(**identity)
        raise

    save_capability(
        **identity,
        usage_supported=observed_usage is not None,
    )
    return {
        "ok": True,
        "cached": False,
        "usage_supported": observed_usage is not None,
        **identity,
    }


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
