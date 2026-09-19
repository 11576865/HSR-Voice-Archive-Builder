from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from .identity import parse_voice_identity

_WAV_RE = re.compile(r"(?P<name>[^|\s]+\.wav)", re.IGNORECASE)


def candidate_names(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and "entries" in data:
            return [str(row["filename"]) for row in data["entries"]]
        if isinstance(data, list):
            return [str(x["filename"] if isinstance(x, dict) else x) for x in data]
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        for key in ("filename", "文件名", "file", "wav", "wav_filename"):
            if rows and key in rows[0]:
                names = []
                for row in rows:
                    value = str(row.get(key, "")).strip()
                    if not value:
                        continue
                    if not value.lower().endswith(".wav"):
                        value += ".wav"
                    names.append(Path(value).name)
                return names
    names: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = _WAV_RE.search(line)
        if match:
            names.append(Path(match.group("name")).name)
    return names


def classify_names(current_manifest: Path, names: list[str]) -> dict[str, object]:
    payload = json.loads(current_manifest.read_text(encoding="utf-8-sig"))
    entries = payload["entries"] if isinstance(payload, dict) else payload
    exact = {str(e["filename"]) for e in entries}
    logical_to_files: dict[str, list[str]] = {}
    for name in exact:
        ident = parse_voice_identity(name)
        logical_to_files.setdefault(ident.logical_id, []).append(name)

    result: dict[str, object] = {
        "exact_existing": [],
        "variant_of_existing": [],
        "new_logical": [],
    }
    seen_candidates: set[str] = set()
    for raw_name in names:
        name = Path(raw_name).name
        if not name or name in seen_candidates:
            continue
        seen_candidates.add(name)
        ident = parse_voice_identity(name)
        if name in exact:
            result["exact_existing"].append(name)  # type: ignore[union-attr]
        elif ident.logical_id in logical_to_files:
            result["variant_of_existing"].append({  # type: ignore[union-attr]
                "candidate": name,
                "existing": sorted(logical_to_files[ident.logical_id]),
            })
        else:
            result["new_logical"].append(name)  # type: ignore[union-attr]

    result["counts"] = {
        key: len(value)
        for key, value in result.items()
        if isinstance(value, list)
    }
    result["candidate_unique_count"] = len(seen_candidates)
    return result


def classify(current_manifest: Path, candidates: Path) -> dict[str, object]:
    return classify_names(current_manifest, candidate_names(candidates))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Classify candidate voice files against an existing manifest")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--candidates", type=Path, required=True)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    result = classify(a.manifest, a.candidates)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if a.out:
        a.out.write_text(output, encoding="utf-8")
    print(output)
