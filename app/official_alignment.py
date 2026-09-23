"""Deterministic deviation gate between source text and official target text.

Official localizations are allowed to reorder, compress, drop filler and rewrite
idioms. Only meaning-bearing deviation justifies paying for a model call, so
every line is first sorted into one of three zones by local checks:

* ``green``  - adopt the official text as-is, no API call;
* ``yellow`` - weak or unresolvable evidence, ask the model to keep or revise;
* ``red``    - core-meaning conflict, ask the model to retranslate with the
  official text as reference.
"""

from __future__ import annotations

import re
from typing import Any

from .semantic_quality import has_negation
from .translation_quality import (
    BRACE_TOKEN_RE,
    HTML_TAG_RE,
    formatting_signatures,
)

OFFICIAL_ALIGNMENT_VERSION = 1

ZONE_GREEN = "green"
ZONE_YELLOW = "yellow"
ZONE_RED = "red"

SEVERITY_HIGH = "high"
SEVERITY_LOW = "low"

_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
_CJK_NUMERALS = {
    0: ("零", "〇"),
    1: ("一", "壹"),
    2: ("二", "两", "兩", "貳"),
    3: ("三",),
    4: ("四",),
    5: ("五",),
    6: ("六",),
    7: ("七",),
    8: ("八",),
    9: ("九",),
    10: ("十",),
}
_QUESTION_MARKS = ("?", "？")
_TARGET_QUESTION_PARTICLES = ("吗", "嗎", "呢", "么", "麼", "吧", "谁", "誰", "什么", "什麼")


def _visible_text(text: str) -> str:
    return HTML_TAG_RE.sub("", BRACE_TOKEN_RE.sub("", str(text or ""))).strip()


def _numbers(text: str) -> list[str]:
    return [value.rstrip(".") for value in _NUMBER_RE.findall(text)]


def _number_present(value: str, text: str) -> bool:
    if value in text:
        return True
    try:
        number = int(value)
    except ValueError:
        return False
    return any(form in text for form in _CJK_NUMERALS.get(number, ()))


def _signal(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def alignment_signals(
    source_text: str,
    official_text: str,
    *,
    glossary: dict[str, str] | None = None,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> list[dict[str, str]]:
    """Return local deviation evidence between a source line and official text.

    Signals are evidence, not a similarity score: ``high`` marks a conflict in
    information that a localization is not allowed to change, ``low`` marks
    weaker evidence that only a model can settle.
    """
    source = str(source_text or "")
    official = str(official_text or "")
    signals: list[dict[str, str]] = []

    if not official.strip():
        return [_signal("empty_official", SEVERITY_HIGH, "Official target text is empty")]
    if not source.strip():
        return signals

    source_sig = formatting_signatures(source)
    official_sig = formatting_signatures(official)
    if source_sig != official_sig:
        missing = list((source_sig - official_sig).elements())
        extra = list((official_sig - source_sig).elements())
        signals.append(_signal(
            "placeholder_mismatch",
            SEVERITY_HIGH,
            f"Control token mismatch; missing={missing[:8]}, extra={extra[:8]}",
        ))

    visible_source = _visible_text(source)
    visible_official = _visible_text(official)

    source_numbers = _numbers(visible_source)
    official_numbers = _numbers(visible_official)
    dropped = [value for value in source_numbers if not _number_present(value, visible_official)]
    added = [value for value in official_numbers if not _number_present(value, visible_source)]
    if dropped or added:
        signals.append(_signal(
            "numeric_conflict",
            SEVERITY_HIGH,
            f"Number mismatch; missing={dropped[:8]}, extra={added[:8]}",
        ))

    source_negated = has_negation(visible_source, source_language)
    official_negated = has_negation(visible_official, target_language)
    if source_negated and not official_negated:
        signals.append(_signal(
            "negation_dropped",
            SEVERITY_HIGH,
            "Source is negated but the official text carries no negation marker",
        ))
    elif official_negated and not source_negated:
        # Chinese frequently negates a positively phrased source ("stay away"
        # -> "不要靠近"), so this direction is only weak evidence.
        signals.append(_signal(
            "negation_added",
            SEVERITY_LOW,
            "Official text is negated while the source is not",
        ))

    for term, expected in (glossary or {}).items():
        if term.casefold() in visible_source.casefold() and expected not in visible_official:
            signals.append(_signal(
                "terminology_missing",
                SEVERITY_LOW,
                f"Official text does not use the required term: {term} => {expected}",
            ))

    source_question = visible_source.endswith(_QUESTION_MARKS)
    official_question = visible_official.endswith(_QUESTION_MARKS) or any(
        particle in visible_official for particle in _TARGET_QUESTION_PARTICLES
    )
    if source_question and not official_question:
        signals.append(_signal(
            "sentence_type_shift",
            SEVERITY_LOW,
            "Source is a question but the official text reads as a statement",
        ))

    source_length = len(visible_source)
    official_length = len(visible_official)
    if source_length >= 20 and official_length <= max(1, round(source_length * 0.12)):
        signals.append(_signal(
            "length_anomaly",
            SEVERITY_LOW,
            f"Official text is extremely short ({official_length} vs source {source_length} chars)",
        ))
    elif source_length >= 20 and official_length >= source_length * 1.6:
        signals.append(_signal(
            "length_anomaly",
            SEVERITY_LOW,
            f"Official text is extremely long ({official_length} vs source {source_length} chars)",
        ))

    return signals


def alignment_zone(signals: list[dict[str, str]]) -> str:
    if any(item.get("severity") == SEVERITY_HIGH for item in signals):
        return ZONE_RED
    if signals:
        return ZONE_YELLOW
    return ZONE_GREEN


def official_candidate(
    *,
    row_id: str,
    source_text: str,
    official_text: str,
    glossary: dict[str, str] | None = None,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> dict[str, Any]:
    """Classify one official line. Green candidates never reach the API."""
    signals = alignment_signals(
        source_text,
        official_text,
        glossary=glossary,
        source_language=source_language,
        target_language=target_language,
    )
    return {
        "id": row_id,
        "english": source_text,
        "official_chinese": official_text,
        "zone": alignment_zone(signals),
        "signals": signals,
    }


def compute_alignment_line_gaps(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute silence gaps between consecutive alignment records and annotate gap modes."""
    if not records:
        return []

    processed: list[dict[str, Any]] = []
    for i, record in enumerate(records):
        item = dict(record)
        start_sec = float(item.get("start_seconds", item.get("start", 0.0)))
        end_sec = float(
            item.get(
                "display_end_seconds",
                item.get("end", item.get("audio_end_seconds", 0.0)),
            )
        )

        if i > 0:
            prev_item = records[i - 1]
            prev_end = float(
                prev_item.get(
                    "display_end_seconds",
                    prev_item.get("end", prev_item.get("audio_end_seconds", 0.0)),
                )
            )
            gap_before = max(0.0, start_sec - prev_end)
        else:
            gap_before = float(item.get("gap_before", 1.5))

        if i < len(records) - 1:
            next_item = records[i + 1]
            next_start = float(next_item.get("start_seconds", next_item.get("start", 0.0)))
            gap_after = max(0.0, next_start - end_sec)
        else:
            gap_after = float(item.get("gap_after", 1.5))

        item["gap_before"] = round(gap_before, 4)
        item["gap_after"] = round(gap_after, 4)
        item["mode_before"] = (
            "compact" if gap_before < 0.3 else ("spacious" if gap_before > 1.0 else "normal")
        )
        item["mode_after"] = (
            "compact" if gap_after < 0.3 else ("spacious" if gap_after > 1.0 else "normal")
        )
        processed.append(item)

    return processed


def summarize_official_alignment(records: list[dict[str, Any]]) -> dict[str, int]:
    def zone_count(zone: str) -> int:
        return sum(str(row.get("zone", "")) == zone for row in records)

    return {
        "count_official_reviewed": len(records),
        "count_official_zone_green": zone_count(ZONE_GREEN),
        "count_official_zone_yellow": zone_count(ZONE_YELLOW),
        "count_official_zone_red": zone_count(ZONE_RED),
        "count_official_api_reviewed": sum(
            bool(row.get("api_reviewed")) for row in records
        ),
        "count_official_checkpoint_reused": sum(
            bool(row.get("checkpoint_reused")) for row in records
        ),
        "count_official_revised": sum(
            str(row.get("decision", "")) == "revise" for row in records
        ),
        "count_official_revision_rejected": sum(
            bool(row.get("revision_rejected")) for row in records
        ),
    }
