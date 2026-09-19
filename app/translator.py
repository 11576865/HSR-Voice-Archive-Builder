from __future__ import annotations

import json
import os


def translate_records(records: list[dict[str, str]], model: str = "gpt-5.6-luna") -> list[dict[str, str]]:
    """Translate records using OpenAI Responses API.

    Expects each record to have `id` and `english`. Reads OPENAI_API_KEY from
    the environment. This module is only needed when Chinese is actually
    missing; existing official CHS LAB is always preferred by builder.py.
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
    prompt = (
        "Translate the following Honkai: Star Rail English voice lines into Simplified Chinese. "
        "Preserve meaning, character tone, placeholders/tags, punctuation intent, and one-to-one IDs. "
        "Do not add context that is not present. Use established official Chinese terminology when known. "
        "Return every input exactly once.\n\n" + json.dumps(records, ensure_ascii=False)
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
    data = json.loads(response.output_text)
    got = data["translations"]
    wanted = {r["id"] for r in records}
    returned = {r["id"] for r in got}
    if wanted != returned or len(got) != len(records):
        raise RuntimeError(f"Translation ID mismatch: missing={wanted-returned}, extra={returned-wanted}")
    return got
