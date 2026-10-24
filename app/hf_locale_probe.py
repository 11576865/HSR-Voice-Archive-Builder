"""Read-only live metadata parity probe for the HSR Japanese Hugging Face dataset.

The result.json index is produced separately from the Parquet dataset.
A matching /rows index is not independently trustworthy if the two move apart.
This probe never downloads audio and never writes or updates project files.
"""
from __future__ import annotations

import mmap
from pathlib import Path
from typing import Any, Callable

from .huggingface_audio import (
    DATASET, ENTRY_RE, ROWS_URL, _field, _json_get,
    _verify_dataset_audio_row, _voice_relative_path,
)


def probe_japanese_row_identity(
    result_json: Path,
    *,
    dataset: str = DATASET,
    samples: int = 5,
    fetch: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare sampled real Japanese result.json rows against dataset /rows.

    A PASS is metadata provenance only, not speech-language identification.
    """
    if not 1 <= samples <= 20:
        raise ValueError("Probe samples must be between 1 and 20")
    if not result_json.is_file() or result_json.stat().st_size == 0:
        raise FileNotFoundError(result_json)
    getter = fetch or _json_get
    selected: list[dict[str, Any]] = []
    total = 0
    with result_json.open("rb") as source, mmap.mmap(
        source.fileno(), 0, access=mmap.ACCESS_READ
    ) as mm:
        iterator = ENTRY_RE.finditer(mm)
        current = next(iterator, None)
        while current is not None and len(selected) < samples:
            following = next(iterator, None)
            index = total
            total += 1
            key = current.group(1).decode("utf-8", "replace")
            if key.casefold().startswith(("japanese/", "japanese(jp)/")):
                end = following.start() if following else len(mm)
                path = _field(mm[current.start():end], "inGameFilename")
                if _voice_relative_path(path).endswith(".wem"):
                    selected.append({
                        "row_idx": index, "key": key,
                        "ingame_filename": path,
                    })
            current = following
    evidence = []
    for item in selected:
        idx = item["row_idx"]
        payload = getter(ROWS_URL, {
            "dataset": dataset, "config": "default", "split": "train",
            "offset": idx // 100 * 100, "length": 100,
        })
        matching = [
            r for r in payload.get("rows", [])
            if int(r.get("row_idx", -1)) == idx
        ]
        if len(matching) != 1:
            evidence.append({"row_idx": idx, "status": "mismatch", "reason": "Dataset row missing or duplicated"})
            continue
        row = matching[0].get("row")
        try:
            _verify_dataset_audio_row(row, item, "ja")
            evidence.append({"row_idx": idx, "status": "match",
                             "ingame_filename": item["ingame_filename"]})
        except ValueError as exc:
            evidence.append({"row_idx": idx, "status": "mismatch", "reason": str(exc)})
    matches = sum(e["status"] == "match" for e in evidence)
    return {
        "dataset": dataset, "audio_language": "ja",
        "result_json": str(result_json), "requested_samples": samples,
        "checked": len(evidence), "matched": matches,
        "passed": len(evidence) == samples and matches == samples,
        "evidence": evidence,
        "metadata_only": True,
        "audio_downloaded": False,
        "spoken_language_verified": False,
        "disclaimer": "Row identity alone cannot prove spoken Japanese. Original audio/field listening acceptance is separate.",
    }
