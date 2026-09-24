from __future__ import annotations

import csv
import json
import shutil
import wave
from pathlib import Path
from typing import Any


"""GPT-SoVITS WebUI dataset exporter.

This module only creates training input files. It does not call GPT-SoVITS,
start training, manage models, or perform preprocessing steps handled by the
GPT-SoVITS WebUI.
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
    min_duration: float = 1.0,
) -> dict[str, Any]:
    """Create a GPT-SoVITS WebUI compatible dataset.

    Generated structure:

    dataset/
    ├── raw/
    │   └── 000001.wav
    ├── speaker.list
    ├── dataset_report.json
    ├── rejected.csv
    └── README_GPTSoVITS.txt

    The .list format is:

    audio_path|speaker|language|text

    No ASR, translation, or filename guessing is performed here.
    """

    destination.mkdir(parents=True, exist_ok=True)
    raw_dir = destination / "raw"
    if copy_wav:
        raw_dir.mkdir(parents=True, exist_ok=True)

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

        duration = _duration_seconds(source)
        if duration is not None and duration < min_duration:
            rejected.append({"index": str(index), "reason": "too_short", "file": filename})
            continue

        if copy_wav:
            target = raw_dir / f"{exported + 1:06d}.wav"
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
        "format": "GPT-SoVITS WebUI dataset",
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

    (destination / "README_GPTSoVITS.txt").write_text(
        "HSR Voice Archive Builder generated GPT-SoVITS dataset\n\n"
        f"Speaker: {speaker}\n"
        f"Language: {language}\n"
        f"Samples: {exported}\n\n"
        "Next steps:\n"
        "1. Open GPT-SoVITS WebUI.\n"
        "2. Use dataset formatting tools.\n"
        "3. Select this .list file.\n"
        "4. Continue with GPT-SoVITS training workflow.\n",
        encoding="utf-8",
    )

    return report
