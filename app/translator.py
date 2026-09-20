from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
DEFAULT_MAX_RETRIES = 5
DEFAULT_TIMEOUT_SECONDS = 120.0


def _chunks(records: list[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    if size < 1:
        raise ValueError("batch_size must be >= 1")
    for i in range(0, len(records), size):
        yield records[i:i + size]


def make_client():
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "The OpenAI Python SDK is unavailable on this runtime. "
            "On Termux/Android the current SDK dependency chain requires jiter/Rust, "
            "which is not reliably installable. Disable GPT fallback or run translation "
            "from a desktop host."
        ) from exc

    # The SDK retries transient connection/rate-limit/server failures. The
    # higher-level pipeline also checkpoints completed batches so a later
    # failure does not discard already-paid-for translations.
    return OpenAI(
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
    """Translate one batch using the OpenAI Responses API.

    Each input must contain id and english. The API key is read only
    from OPENAI_API_KEY. IDs must round-trip exactly; otherwise the batch is
    rejected instead of being partially written back.
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
