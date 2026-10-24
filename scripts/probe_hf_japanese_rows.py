#!/usr/bin/env python3
"""Compare real Japanese result.json row identities with the Hugging Face viewer.

No audio downloaded; no writes to a project or its source data.

Usage:
    python scripts/probe_hf_japanese_rows.py \
      --result-json "/path/to/project/.state/providers/honkai-star-rail/simon3000--starrail-voice/result.json" \
      --samples 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow direct execution from a source checkout without pip installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.hf_locale_probe import probe_japanese_row_identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--dataset", default="simon3000/starrail-voice")
    parser.add_argument("--samples", type=int, default=5)
    options = parser.parse_args()
    try:
        report = probe_japanese_row_identity(
            options.result_json, dataset=options.dataset,
            samples=options.samples,
        )
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
