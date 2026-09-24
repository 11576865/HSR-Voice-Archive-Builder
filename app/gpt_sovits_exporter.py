from __future__ import annotations

import csv
import json
import shutil
import wave
from pathlib import Path
from typing import Any


"""GPT-SoVITS training dataset export helpers.

This module intentionally sits outside the archive builder pipeline. Existing
archive outputs remain unchanged; this only converts resolved archive metadata
into a model-training dataset.
"""


def _duration_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as f:
            return f.getnframes() / float(f.getframerate() or 1)
    except Exception:
        return None


def export_gpt_sovits_dataset(
    rows: list[dict[str, str]],
    wav_root: Path,
    destination: Path,
    *,
    speaker: str,
    language: str = "en",
    copy_wav: bool = True,
) -> dict[str, Any]:
    """Export sentence-level WAV/text pairs to GPT-SoVITS list format.

    rows are expected to already contain resolved metadata. This function does
    not perform ASR, translation, or filename guessing.
    """

    destination.mkdir(parents=True, exist_ok=True)
    wav_dir = destination / "wav"
    if copy_wav:
        wav_dir.mkdir(parents=True, exist_ok=True)

    exported = 0
    rejected: list[dict[str, str]] = []
    list_rows: list[str] = []

    for index, row in enumerate(rows, 1):
        filename = str(row.get("filename", "") or "").strip()
        text = str(row.get("english", "") or row.get("text", "") or "").strip()
        if not filename:
            rejected.append({"index": str(index), "reason": "missing_filename"})
            continue
        source = wav_root / filename
        if not source.is_file():
            rejected.append({"index": str(index), "reason": "missing_audio", "file": filename})
            continue
        if not text:
            rejected.append({"index": str(index), "reason": "missing_text", "file": filename})
            continue

        target = wav_dir / f"{exported + 1:06d}{source.suffix.lower()}"
        if copy_wav:
            shutil.copy2(source, target)
            audio_path = target.resolve()
        else:
            audio_path = source.resolve()

        list_rows.append(f"{audio_path}|{speaker}|{language}|{text}")
        exported += 1

    list_file = destination / f"{speaker}.list"
    list_file.write_text("\n".join(list_rows) + ("\n" if list_rows else ""), encoding="utf-8")

    rejected_file = destination / "rejected.csv"
    with rejected_file.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["index", "reason", "file"])
        writer.writeheader()
        writer.writerows(rejected)

    report = {
        "speaker": speaker,
        "language": language,
        "total": len(rows),
        "exported": exported,
        "rejected": len(rejected),
        "list_file": str(list_file),
        "rejected_file": str(rejected_file),
    }
    (destination / "dataset_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report
