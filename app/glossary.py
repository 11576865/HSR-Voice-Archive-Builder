from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_GLOSSARY_TERMS = 10_000
MAX_TERM_CHARS = 500

# Small project-owned core terminology set. These are terminology constraints,
# not a copy of game dialogue. Keep this list conservative: only stable,
# established names/terms should be hard-enforced.
CORE_GLOSSARY: dict[str, str] = {
    "Evanescia": "绯英",
    "Planarcadia": "二相乐园",
    "Phantasmoon Games": "幻月游戏",
    "Wishpower": "愿力",
    "Supplicant": "谒者",
    "Graphia": "绘世",
    "Yao Guang": "爻光",
    "Fulwish": "满愿",
    "Stellar Jade": "星琼",
}


def _validated_pair(source: object, target: object, *, row: int) -> tuple[str, str]:
    english = str(source or "").strip()
    chinese = str(target or "").strip()
    if not english or not chinese:
        raise ValueError(f"Glossary row {row} must contain non-empty English and Chinese")
    if len(english) > MAX_TERM_CHARS or len(chinese) > MAX_TERM_CHARS:
        raise ValueError(
            f"Glossary row {row} exceeds the {MAX_TERM_CHARS}-character term limit"
        )
    return english, chinese


def _rows_to_mapping(rows: Iterable[tuple[object, object]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, (source, target) in enumerate(rows, 1):
        english, chinese = _validated_pair(source, target, row=index)
        existing = result.get(english)
        if existing is not None and existing != chinese:
            raise ValueError(
                f"Glossary has conflicting targets for {english!r}: "
                f"{existing!r} vs {chinese!r}"
            )
        result[english] = chinese
        if len(result) > MAX_GLOSSARY_TERMS:
            raise ValueError(
                f"Glossary contains more than {MAX_GLOSSARY_TERMS} unique terms"
            )
    return result


def load_glossary_overlay(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)

    suffix = resolved.suffix.casefold()
    if suffix == ".json":
        payload: Any = json.loads(resolved.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            return _rows_to_mapping(payload.items())
        if isinstance(payload, list):
            rows: list[tuple[object, object]] = []
            for index, item in enumerate(payload, 1):
                if not isinstance(item, dict):
                    raise ValueError(f"Glossary JSON row {index} must be an object")
                source = item.get("english", item.get("source"))
                target = item.get("chinese", item.get("target"))
                rows.append((source, target))
            return _rows_to_mapping(rows)
        raise ValueError("Glossary JSON must be an object mapping or a list of row objects")

    if suffix == ".csv":
        with resolved.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                raise ValueError("Glossary CSV has no header")
            fields = {str(name).strip().casefold(): name for name in reader.fieldnames}
            source_key = fields.get("english") or fields.get("source")
            target_key = fields.get("chinese") or fields.get("target")
            if source_key is None or target_key is None:
                raise ValueError(
                    "Glossary CSV requires English/Chinese columns "
                    "(aliases: source/target)"
                )
            return _rows_to_mapping(
                (row.get(source_key), row.get(target_key))
                for row in reader
            )

    raise ValueError("Glossary overlay must be .csv or .json")


def merge_glossary(
    base: dict[str, str],
    overlay: dict[str, str] | None = None,
) -> dict[str, str]:
    merged = dict(base)
    merged.update(overlay or {})
    return merged


def glossary_fingerprint(glossary: dict[str, str]) -> str:
    canonical = json.dumps(
        sorted(glossary.items()),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def relevant_glossary(
    glossary: dict[str, str],
    texts: Iterable[str],
) -> dict[str, str]:
    haystack = "\n".join(str(x) for x in texts).casefold()
    return {
        source: target
        for source, target in glossary.items()
        if source.casefold() in haystack
    }
