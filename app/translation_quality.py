from __future__ import annotations

import re
from collections import Counter
from typing import Any

HTML_TAG_RE = re.compile(r"<[^>]+>")
BRACE_TOKEN_RE = re.compile(r"\{[^}]+\}")
ASCII_WORD_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]{5,}(?![A-Za-z])")


def _brace_signature(token: str) -> str:
    inner = token[1:-1]
    if "#" in inner:
        return "{" + inner.split("#", 1)[0] + "#}"
    return token


def formatting_signatures(text: str) -> Counter[str]:
    signatures: Counter[str] = Counter()
    for tag in HTML_TAG_RE.findall(text):
        signatures["html:" + tag] += 1
    for token in BRACE_TOKEN_RE.findall(text):
        signatures["brace:" + _brace_signature(token)] += 1
    return signatures


def translation_qa(
    english: str,
    chinese: str,
    glossary: dict[str, str] | None = None,
    target_language: str = "zh-CN",
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    glossary = glossary or {}

    source_sig = formatting_signatures(english)
    target_sig = formatting_signatures(chinese)
    if source_sig != target_sig:
        missing = list((source_sig - target_sig).elements())
        extra = list((target_sig - source_sig).elements())
        issues.append({
            "code": "format_tokens",
            "severity": "major",
            "message": f"Formatting token mismatch; missing={missing[:8]}, extra={extra[:8]}",
        })

    lower_en = english.casefold()
    for source, target in glossary.items():
        if source.casefold() in lower_en and target not in chinese:
            issues.append({
                "code": "terminology",
                "severity": "major",
                "message": f"Required terminology missing: {source} => {target}",
            })

    stripped = HTML_TAG_RE.sub("", BRACE_TOKEN_RE.sub("", chinese))
    residues = sorted(set(ASCII_WORD_RE.findall(stripped)))
    if str(target_language).lower().startswith("zh") and residues:
        issues.append({
            "code": "english_residue",
            "severity": "minor",
            "message": "Possible untranslated English residue: " + ", ".join(residues[:8]),
        })

    en_len = len(HTML_TAG_RE.sub("", BRACE_TOKEN_RE.sub("", english)).strip())
    zh_len = len(HTML_TAG_RE.sub("", BRACE_TOKEN_RE.sub("", chinese)).strip())
    if en_len >= 20 and zh_len <= max(1, int(en_len * 0.05)):
        issues.append({
            "code": "too_short",
            "severity": "major",
            "message": f"Translation is unexpectedly short ({zh_len} vs source {en_len} chars)",
        })
    if en_len >= 20 and zh_len >= en_len * 2.5:
        issues.append({
            "code": "too_long",
            "severity": "minor",
            "message": f"Translation is unexpectedly long ({zh_len} vs source {en_len} chars)",
        })

    return issues


def has_hard_issue(issues: list[dict[str, str]]) -> bool:
    return any(item.get("severity") in {"major", "critical"} for item in issues)


def qa_messages(issues: list[dict[str, str]]) -> list[str]:
    return [f"{item.get('code')}: {item.get('message')}" for item in issues]


def summarize_qa(records: list[dict[str, Any]]) -> dict[str, int]:
    total = len(records)
    hard = sum(bool(row.get("hard_failed")) for row in records)
    warnings = sum(bool(row.get("issues")) for row in records)
    retried = sum(bool(row.get("retried")) for row in records)
    return {
        "count_translation_qa_records": total,
        "count_translation_qa_warnings": warnings,
        "count_translation_qa_hard_failed": hard,
        "count_translation_qa_retried": retried,
    }
