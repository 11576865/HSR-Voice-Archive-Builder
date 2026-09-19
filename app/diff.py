from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .identity import parse_voice_identity

_WAV_RE = re.compile(r"(?P<name>[^|\s]+\.wav)", re.IGNORECASE)


def candidate_names(path: Path) -> list[str]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and "entries" in data:
            return [str(row["filename"]) for row in data["entries"]]
        if isinstance(data, list):
            return [str(x["filename"] if isinstance(x, dict) else x) for x in data]
    names: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = _WAV_RE.search(line)
        if match:
            names.append(Path(match.group("name")).name)
    return names


def classify(current_manifest: Path, candidates: Path) -> dict[str, object]:
    payload = json.loads(current_manifest.read_text(encoding="utf-8-sig"))
    entries = payload["entries"] if isinstance(payload, dict) else payload
    exact = {str(e["filename"]) for e in entries}
    logical_to_files: dict[str, list[str]] = {}
    for name in exact:
        ident = parse_voice_identity(name)
        logical_to_files.setdefault(ident.logical_id, []).append(name)

    result = {"exact_existing": [], "variant_of_existing": [], "new_logical": []}
    for name in candidate_names(candidates):
        ident = parse_voice_identity(name)
        if name in exact:
            result["exact_existing"].append(name)
        elif ident.logical_id in logical_to_files:
            result["variant_of_existing"].append({"candidate": name, "existing": sorted(logical_to_files[ident.logical_id])})
        else:
            result["new_logical"].append(name)
    result["counts"] = {k: len(v) for k, v in result.items() if isinstance(v, list)}
    return result


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
