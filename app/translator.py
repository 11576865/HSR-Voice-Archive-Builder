from __future__ import annotations

import json
import os
from collections.abc import Iterable

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")


def _chunks(records: list[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    if size < 1:
        raise ValueError("batch_size must be >= 1")
    for i in range(0, len(records), size):
        yield records[i:i + size]


def translate_records(
    records: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    glossary: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Translate one batch using the OpenAI Responses API.

    Each input must contain `id` and `english`. The API key is read only
    from `OPENAI_API_KEY`. IDs must round-trip exactly; otherwise the batch is
    rejected instead of being partially written back.
    """
    if not records:
        return []
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")

    from openai import OpenAI

    client = OpenAI()
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
            f"Translation ID mismatch: missing={set(wanted_list)-set(got_ids)}, extra={set(got_ids)-set(wanted_list)}"
        )
    by_id = {r["id"]: r for r in got}
    return [by_id[i] for i in wanted_list]


def translate_in_batches(
    records: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    batch_size: int = 80,
    glossary: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for batch in _chunks(records, batch_size):
        out.extend(translate_records(batch, model=model, glossary=glossary))
    return out
