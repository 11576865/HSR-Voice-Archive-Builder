from __future__ import annotations

import difflib
import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from .credentials import load_translation_credentials, normalize_base_url, translation_default_model
from .version import APP_VERSION

DEFAULT_MODEL = translation_default_model()
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

SEMANTIC_VERIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "ok": {"type": "boolean"},
                    "issues": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "negation",
                                "quantity",
                                "person",
                                "condition",
                                "omission",
                                "addition",
                            ],
                        },
                    },
                    "note": {"type": "string"},
                },
                "required": ["id", "ok", "issues", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

OFFICIAL_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "decision": {
                        "type": "string",
                        "enum": ["accept_official", "revise"],
                    },
                    "reason": {
                        "type": "string",
                        "enum": [
                            "acceptable_localization",
                            "material_omission",
                            "material_addition",
                            "contradiction",
                            "numeric_mismatch",
                            "placeholder_mismatch",
                            "role_or_tone_shift",
                        ],
                    },
                    "translation": {"type": "string"},
                },
                "required": ["id", "decision", "reason", "translation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reviews"],
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


def _extract_chat_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("Translation Chat Completions response contained no choice")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("Translation Chat Completions response contained no message")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        pieces = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        ]
        text = "".join(pieces)
        if text.strip():
            return text
    raise RuntimeError(
        "Translation Chat Completions response contained no output text "
        f"(finish_reason={choices[0].get('finish_reason')!r})"
    )


def _parse_json_output(text: str, *, context: str) -> Any:
    """Parse strict JSON while tolerating a single Markdown JSON fence.

    Some OpenAI-compatible providers ignore structured-output controls and
    wrap an otherwise valid response in ```json.  No free-form prose or
    partial-object recovery is attempted, so ID/count validation remains the
    authority after parsing.
    """
    value = str(text or "").strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"}:
            value = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        preview = value[:160].replace("\n", " ")
        raise RuntimeError(
            f"{context} returned text that is not valid JSON; preview={preview!r}"
        ) from exc


@dataclass
class HTTPResponse:
    output_text: str
    raw: dict[str, Any]


def _structured_rows(data: Any, field: str, *, context: str) -> list[dict[str, Any]]:
    """Accept the schema wrapper or an equivalent bare array from relays."""
    rows = data.get(field) if isinstance(data, dict) else data if isinstance(data, list) else None
    if not isinstance(rows, list):
        raise RuntimeError(
            f"{context} response must be an object containing '{field}' or a bare array"
        )
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{context} response contains a non-object row")
    return rows


_ID_TAIL_RE = re.compile(r"(\d+)(\.[A-Za-z0-9]+)?$")


def _repairable_id_pair(wanted_id: str, got_id: str) -> bool:
    """True when a returned ID looks like a corrupted copy of a wanted ID.

    Long filename-like IDs occasionally come back from compatible providers
    with a spurious syllable inserted or dropped (for example
    ``..._silverwolf999_01_703591349.wav`` returned as
    ``..._silverwolflv999_01_703591349.wav``).  The pair must keep the same
    trailing numeric token, differ by only a few characters, and stay
    textually near-identical, so two genuinely different records can never
    be paired by accident.
    """
    if wanted_id == got_id:
        return True
    tail_wanted = _ID_TAIL_RE.search(wanted_id)
    tail_got = _ID_TAIL_RE.search(got_id)
    if not tail_wanted or not tail_got or tail_wanted.group(0) != tail_got.group(0):
        return False
    if abs(len(wanted_id) - len(got_id)) > 4:
        return False
    return difflib.SequenceMatcher(None, wanted_id, got_id).ratio() >= 0.85


def _remap_corrupted_ids(
    got: list[dict[str, Any]],
    missing: set[str],
    extra: set[Any],
) -> list[dict[str, Any]] | None:
    """Rewrite model-corrupted IDs back to the requested ones when safe.

    A remap is accepted only when every missing ID pairs with exactly one
    unused extra ID (and all extras are consumed) via _repairable_id_pair;
    anything ambiguous returns None so the caller keeps the strict error.
    """
    extra_ids = {e for e in extra if isinstance(e, str)}
    if not missing or len(missing) != len(extra_ids):
        return None
    pairs: dict[str, str] = {}
    used_extra: set[str] = set()
    for wanted_id in sorted(missing):
        candidates = [
            e for e in extra_ids
            if e not in used_extra and _repairable_id_pair(wanted_id, e)
        ]
        if len(candidates) != 1:
            return None
        pairs[candidates[0]] = wanted_id
        used_extra.add(candidates[0])
    if used_extra != extra_ids:
        return None
    remapped: list[dict[str, Any]] = []
    for row in got:
        row_id = row.get("id")
        if isinstance(row_id, str) and row_id in pairs:
            row = dict(row)
            row["id"] = pairs[row_id]
        remapped.append(row)
    return remapped


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
        self.chat_completions_url = self.base_url.rstrip("/") + "/chat/completions"
        hostname = (urlsplit(self.base_url).hostname or "").casefold()
        self.api_mode = (
            "chat_completions"
            if hostname.endswith(".maas.aliyuncs.com")
            or hostname.endswith(".dashscope.aliyuncs.com")
            else "responses"
        )
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
        endpoint = self.responses_url
        request_payload = payload
        if self.api_mode == "chat_completions":
            endpoint = self.chat_completions_url
            format_spec = payload.get("text", {}).get("format", {})
            schema = format_spec.get("schema") if isinstance(format_spec, dict) else None
            schema_instruction = (
                " Return only a valid JSON object matching this JSON Schema, with no Markdown fence: "
                + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                if isinstance(schema, dict)
                else " Return only valid JSON, with no Markdown fence."
            )
            request_payload = {
                "model": payload.get("model"),
                "messages": [
                    {"role": "system", "content": schema_instruction.strip()},
                    {"role": "user", "content": str(payload.get("input", ""))},
                ],
                # json_object has wider compatibility across Model Studio
                # models than OpenAI's provider-specific json_schema wrapper.
                "response_format": {"type": "json_object"},
            }
        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": f"HSR-Voice-Archive-Builder/{APP_VERSION}",
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
                if self.api_mode == "chat_completions":
                    return HTTPResponse(
                        output_text=_extract_chat_completion_text(data), raw=data
                    )
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
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> str:
    glossary_text = ""
    if glossary:
        glossary_text = "\nOfficial/preferred terminology:\n" + "\n".join(
            f"- {src} => {dst}" for src, dst in glossary.items()
        )
    return (
        f"Translate the target field 'english' in each Honkai: Star Rail record from {source_language} "
        f"into {target_language}. Preserve meaning, character tone, punctuation intent, and one-to-one IDs. "
        "Do not add information. Records may include context_before/context_after; use them only to disambiguate "
        "the target and do not translate them as separate outputs. A record may also include reference_text and "
        "reference_language from a second official voice/text package; treat it only as semantic reference and "
        "never as a second output. Records may include previous_chinese and qa_issues; when present, repair the "
        "previous target translation specifically for those issues. Preserve HTML-like tags and the structural "
        "form of brace control tokens such as {NICKNAME}, {M#...}{F#...}, and RUBY markers. Text payloads inside "
        "control tokens may be translated when they are user-visible, but the token type/structure must remain. "
        "Use the supplied terminology exactly when its source term occurs in the source text. "
        "Return every input ID exactly once. The JSON field remains named 'chinese' for backward compatibility, "
        f"but its value must be the requested {target_language} translation."
        + glossary_text
        + "\n\nInput JSON:\n"
        + json.dumps(records, ensure_ascii=False)
    )


def translate_records(
    records: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    glossary: dict[str, str] | None = None,
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
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
        input=_translation_prompt(
            records,
            glossary,
            source_language=source_language,
            target_language=target_language,
        ),
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
    data = _parse_json_output(response.output_text, context="Translation API")
    got = _structured_rows(data, "translations", context="Translation")
    wanted_list = [r["id"] for r in records]
    wanted_set = set(wanted_list)
    relevant = [row for row in got if row.get("id") in wanted_set]
    relevant_ids = [row["id"] for row in relevant]
    if len(relevant_ids) != len(set(relevant_ids)):
        raise RuntimeError("Translation response contains duplicate IDs")
    missing = wanted_set - set(relevant_ids)
    if missing:
        extra = {row.get("id") for row in got} - wanted_set
        repaired = _remap_corrupted_ids(got, missing, extra)
        if repaired is None:
            raise RuntimeError(
                f"Translation ID mismatch: missing={missing}, extra={extra}"
            )
        got = repaired
        relevant = [row for row in got if row.get("id") in wanted_set]
        relevant_ids = [row["id"] for row in relevant]
        if len(relevant_ids) != len(set(relevant_ids)):
            raise RuntimeError("Translation response contains duplicate IDs")
    # Some compatible providers append a guessed or explanatory row despite
    # the schema.  It is safe to discard it only after every requested ID is
    # present exactly once; requested rows are still joined strictly by ID.
    by_id = {r["id"]: r for r in relevant}
    ordered = [by_id[i] for i in wanted_list]
    for row in ordered:
        if not str(row.get("chinese", "")).strip():
            raise RuntimeError(f"Translation response contains empty target text: {row['id']}")
    return ordered


def verify_semantic_records(
    records: list[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
    client: Any | None = None,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> list[dict[str, Any]]:
    """Verify only preselected high-risk translations for semantic drift."""
    if not records:
        return []
    client = client or make_client()
    prompt = (
        f"Audit each {target_language} translation against its {source_language} source. "
        "This is a semantic and record-alignment verification pass, not a style review. "
        "Check every record independently even when risk_tags is empty. A fluent target that "
        "translates another record in the same batch is wrong: set ok=false and report omission "
        "and/or addition. Detect one-row shifts, swapped targets, and repeated near-duplicate "
        "targets used for different sources. Compare the central action, subject, object, intent, "
        "question/statement function, and distinctive concepts before accepting a row. "
        "Use risk_tags as attention hints, but also flag a major omission or addition if it changes meaning. "
        "Check negation polarity, quantities/comparatives, grammatical person/reference, and conditional logic. "
        "Set ok=true when the target translation preserves the source meaning even if wording is not literal. "
        "Do not penalize natural target-language phrasing. Return every input ID exactly once. "
        "For ok=true, issues must be an empty array and note should be brief. "
        "For ok=false, list only the applicable issue categories and explain the concrete mismatch briefly. "
        "\n\nInput JSON:\n"
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
                "name": "voice_translation_semantic_audit",
                "strict": True,
                "schema": SEMANTIC_VERIFIER_SCHEMA,
            }
        },
    )

    if usage_callback is not None:
        from .translation_runtime import parse_usage

        usage_callback(parse_usage(getattr(response, "raw", None)))

    if not getattr(response, "output_text", ""):
        raise RuntimeError("Semantic verifier response contained no output_text")
    data = _parse_json_output(response.output_text, context="Semantic verifier")
    verdicts = _structured_rows(data, "verdicts", context="Semantic verifier")
    wanted = [str(row["id"]) for row in records]
    got_ids = [str(row.get("id", "")) for row in verdicts]
    if len(got_ids) != len(set(got_ids)):
        raise RuntimeError("Semantic verifier response contains duplicate IDs")
    if set(got_ids) != set(wanted) or len(verdicts) != len(records):
        missing = set(wanted) - set(got_ids)
        extra = set(got_ids) - set(wanted)
        repaired = _remap_corrupted_ids(verdicts, missing, extra)
        if repaired is None:
            raise RuntimeError(
                f"Semantic verifier ID mismatch: missing={missing}, extra={extra}"
            )
        verdicts = repaired
        got_ids = [str(row.get("id", "")) for row in verdicts]
        if len(got_ids) != len(set(got_ids)):
            raise RuntimeError("Semantic verifier response contains duplicate IDs")
    by_id = {str(row["id"]): row for row in verdicts}
    ordered: list[dict[str, Any]] = []
    for row_id in wanted:
        row = dict(by_id[row_id])
        issues = row.get("issues")
        if not isinstance(issues, list):
            raise RuntimeError(f"Semantic verifier issues must be a list: {row_id}")
        if bool(row.get("ok")) and issues:
            raise RuntimeError(
                f"Semantic verifier returned ok=true with issues for {row_id}"
            )
        if not bool(row.get("ok")) and not issues:
            raise RuntimeError(
                f"Semantic verifier returned ok=false without issues for {row_id}"
            )
        ordered.append(row)
    return ordered


def _official_review_prompt(
    records: list[dict[str, Any]],
    glossary: dict[str, str] | None,
    source_language: str,
    target_language: str,
) -> str:
    glossary_text = ""
    if glossary:
        glossary_text = "\nOfficial/preferred terminology:\n" + "\n".join(
            f"- {src} => {dst}" for src, dst in glossary.items()
        )
    return (
        f"Each record pairs a {source_language} line with its official {target_language} "
        "subtitle from the game. Judge subtitle adequacy, not literal equivalence, and "
        "decide in one pass whether the official text can stay.\n"
        "Tolerate: reordering, dropping English filler/interjections, rewritten idioms and "
        "jokes, changed forms of address, compression for dubbing rhythm, and ordinary "
        "localization of tone.\n"
        "Do not tolerate: a change in who does what, flipped or weakened negation/affirmation, "
        "changed modality (can/must/will/already), inconsistent numbers, names, places or "
        "proper nouns, lost conditions, causes or comparisons, information present in the "
        "source and fully absent in the target, new facts absent from the source, clearly "
        "changed character attitude or intent, and an answer rewritten as unrelated dialogue.\n"
        "Every record carries 'zone' and 'signals' from a local pre-check. Treat red as strong "
        "evidence of a real conflict and yellow as unresolved weak evidence; the signals are "
        "hints, not verdicts, so overrule them when the official line still conveys the "
        "source meaning.\n"
        "Return decision='accept_official' with translation='' when the official text is "
        "adequate. Return decision='revise' only for intolerable deviation, and then put a "
        f"corrected {target_language} line in 'translation' that keeps the official wording "
        "and terminology wherever it is already correct and repairs only the deviation. "
        "Preserve HTML-like tags and the structural form of brace control tokens such as "
        "{NICKNAME}, {M#...}{F#...}, and RUBY markers. Return every input ID exactly once."
        + glossary_text
        + "\n\nInput JSON:\n"
        + json.dumps(records, ensure_ascii=False)
    )


def review_official_records(
    records: list[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    glossary: dict[str, str] | None = None,
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
    client: Any | None = None,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> list[dict[str, Any]]:
    """Judge and, when required, retranslate official target lines in one call."""
    if not records:
        return []
    client = client or make_client()

    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        store=False,
        input=_official_review_prompt(
            records,
            glossary,
            source_language,
            target_language,
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "official_target_review",
                "strict": True,
                "schema": OFFICIAL_REVIEW_SCHEMA,
            }
        },
    )

    if usage_callback is not None:
        from .translation_runtime import parse_usage

        usage_callback(parse_usage(getattr(response, "raw", None)))

    if not getattr(response, "output_text", ""):
        raise RuntimeError("Official review response contained no output_text")
    data = _parse_json_output(response.output_text, context="Official review")
    reviews = _structured_rows(data, "reviews", context="Official review")
    wanted = [str(row["id"]) for row in records]
    got_ids = [str(row.get("id", "")) for row in reviews]
    if len(got_ids) != len(set(got_ids)):
        raise RuntimeError("Official review response contains duplicate IDs")
    if set(got_ids) != set(wanted) or len(reviews) != len(records):
        missing = set(wanted) - set(got_ids)
        extra = set(got_ids) - set(wanted)
        repaired = _remap_corrupted_ids(reviews, missing, extra)
        if repaired is None:
            raise RuntimeError(
                f"Official review ID mismatch: missing={missing}, extra={extra}"
            )
        reviews = repaired
        got_ids = [str(row.get("id", "")) for row in reviews]
        if len(got_ids) != len(set(got_ids)):
            raise RuntimeError("Official review response contains duplicate IDs")
    by_id = {str(row["id"]): row for row in reviews}
    ordered: list[dict[str, Any]] = []
    for row_id in wanted:
        row = dict(by_id[row_id])
        decision = str(row.get("decision", ""))
        translation = str(row.get("translation", "")).strip()
        if decision == "revise" and not translation:
            raise RuntimeError(
                f"Official review returned decision=revise without a translation: {row_id}"
            )
        row["translation"] = translation if decision == "revise" else ""
        ordered.append(row)
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
