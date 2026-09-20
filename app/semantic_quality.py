from __future__ import annotations

import re
from typing import Any

SEMANTIC_QA_VERSION = 1

_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|none|nothing|nobody|neither|nor|without|cannot|can't|won't|"
    r"don't|doesn't|didn't|isn't|aren't|wasn't|weren't|shouldn't|wouldn't|couldn't|"
    r"mustn't|hasn't|haven't|hadn't)\b",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(
    r"(?<![A-Za-z])(?:\d+(?:\.\d+)?%?|zero|one|two|three|four|five|six|seven|eight|"
    r"nine|ten|first|second|third|half|double|twice|both|all|every|each|few|several|"
    r"many|much|more|less|fewer|most|least|only)\b",
    re.IGNORECASE,
)
_CONDITION_RE = re.compile(
    r"\b(?:if|unless|provided that|as long as|in case|otherwise|whether|when|whenever|"
    r"until|once)\b",
    re.IGNORECASE,
)

_PERSON_GROUPS = {
    "first": re.compile(
        r"(?<![A-Za-z])(?:I|me|my|mine|myself|we|us|our|ours|ourselves)(?![A-Za-z])",
        re.IGNORECASE,
    ),
    "second": re.compile(
        r"(?<![A-Za-z])(?:you|your|yours|yourself|yourselves)(?![A-Za-z])",
        re.IGNORECASE,
    ),
    "third": re.compile(
        r"(?<![A-Za-z])(?:he|him|his|himself|she|her|hers|herself|they|them|their|"
        r"theirs|themselves)(?![A-Za-z])",
        re.IGNORECASE,
    ),
}


def semantic_risk_tags(english: str) -> list[str]:
    """Return sparse semantic-risk tags for lines worth a verifier call.

    Person-reference checking is activated only when at least two grammatical
    person groups occur in one line, which keeps ordinary single-speaker
    dialogue from turning the verifier into a second full translation pass.
    """
    text = str(english or "")
    tags: list[str] = []
    if _NEGATION_RE.search(text):
        tags.append("negation")
    if _QUANTITY_RE.search(text):
        tags.append("quantity")
    if _CONDITION_RE.search(text):
        tags.append("condition")

    person_groups = [
        name for name, pattern in _PERSON_GROUPS.items()
        if pattern.search(text)
    ]
    if len(person_groups) >= 2:
        tags.append("person")

    return tags


def semantic_candidate(
    *,
    row_id: str,
    english: str,
    chinese: str,
) -> dict[str, Any] | None:
    tags = semantic_risk_tags(english)
    if not tags:
        return None
    return {
        "id": row_id,
        "english": english,
        "chinese": chinese,
        "risk_tags": tags,
    }


def summarize_semantic_qa(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "count_semantic_qa_candidates": len(records),
        # "failed" means initially flagged by the semantic verifier. A row can
        # therefore be both failed=1 and repaired=1 while hard_failed remains 0.
        "count_semantic_qa_failed": sum(
            not bool(row.get("initial_ok", row.get("ok"))) for row in records
        ),
        "count_semantic_qa_repaired": sum(bool(row.get("repaired")) for row in records),
        "count_semantic_qa_hard_failed": sum(bool(row.get("hard_failed")) for row in records),
    }
