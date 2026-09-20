from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .builder import atomic_write_text
from .credentials import credentials_status
from .project import last_project_root, load_project, resolve_project_path
from .schema import normalize_index
from .translator import DEFAULT_MODEL, make_client, translate_records

DEFAULT_SAMPLE_SIZE = 50
DEFAULT_BATCH_SIZE = 50
DEFAULT_SEED = "hsr-translation-quality-v1"

TERM_MARKERS = (
    "aeon",
    "stellaron",
    "path",
    "elation",
    "nameless",
    "stellar jade",
    "planarcadia",
    "phantasmoon",
    "wishpower",
    "fulwish",
    "supplicant",
    "graphia",
    "yao guang",
    "evanescia",
    "ambrosial arbor",
)

TAG_RE = re.compile(r"<[^>]+>|\{[^}]+\}|RUBY", re.IGNORECASE)
EXPRESSIVE_RE = re.compile(r"(\.\.\.|[!?~—]|\b(?:don't|isn't|aren't|can't|won't|wouldn't|couldn't|shouldn't)\b)", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_sample(text: str) -> str:
    lower = text.lower()
    if TAG_RE.search(text):
        return "tags_placeholders"
    if any(term in lower for term in TERM_MARKERS):
        return "hsr_terminology"
    if len(text) >= 140:
        return "long_complex"
    if len(text) <= 45:
        return "short_context_sensitive"
    if EXPRESSIVE_RE.search(text):
        return "expressive_dialogue"
    return "general"


def _stable_key(row: dict[str, str], seed: str) -> str:
    raw = f"{seed}\0{row['filename']}\0{row['english']}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def select_sample(
    rows: list[dict[str, str]],
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    *,
    seed: str = DEFAULT_SEED,
) -> list[dict[str, str]]:
    if sample_size < 1:
        raise ValueError("sample_size must be >= 1")

    usable = [row for row in rows if str(row.get("english", "")).strip()]
    if not usable:
        raise ValueError("No English text was found in the index")
    sample_size = min(sample_size, len(usable))

    bucket_order = [
        "tags_placeholders",
        "hsr_terminology",
        "long_complex",
        "short_context_sensitive",
        "expressive_dialogue",
        "general",
    ]
    buckets: dict[str, list[dict[str, str]]] = {name: [] for name in bucket_order}
    for row in usable:
        bucket = classify_sample(row["english"])
        enriched = dict(row)
        enriched["benchmark_category"] = bucket
        buckets[bucket].append(enriched)

    for bucket in buckets.values():
        bucket.sort(key=lambda row: _stable_key(row, seed))

    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    offsets = {name: 0 for name in bucket_order}

    # First pass: rotate through categories. This intentionally favors coverage
    # over a purely random sample because the goal is translation failure
    # discovery, not corpus-frequency estimation.
    while len(selected) < sample_size:
        progressed = False
        for name in bucket_order:
            bucket = buckets[name]
            pos = offsets[name]
            while pos < len(bucket) and bucket[pos]["filename"] in seen:
                pos += 1
            offsets[name] = pos
            if pos >= len(bucket):
                continue
            row = bucket[pos]
            offsets[name] += 1
            selected.append(row)
            seen.add(row["filename"])
            progressed = True
            if len(selected) >= sample_size:
                break
        if not progressed:
            break

    # Defensive fill if future category changes leave any rows unselected.
    if len(selected) < sample_size:
        remaining = sorted(
            (row for row in usable if row["filename"] not in seen),
            key=lambda row: _stable_key(row, seed),
        )
        for row in remaining[: sample_size - len(selected)]:
            enriched = dict(row)
            enriched["benchmark_category"] = classify_sample(row["english"])
            selected.append(enriched)

    return selected


def _resolve_index(explicit: Path | None) -> tuple[Path, Path]:
    if explicit is not None:
        index = explicit.expanduser().resolve()
        if not index.is_file():
            raise FileNotFoundError(index)
        return index, index.parent

    root = last_project_root()
    if root is None:
        raise RuntimeError(
            "No recent project is available. Pass --index /path/to/index.csv."
        )
    config = load_project(root)
    index = resolve_project_path(config, config.index_csv)
    if index is None or not index.is_file():
        raise FileNotFoundError(
            "The recent project's index CSV is unavailable; pass --index explicitly."
        )
    return index.resolve(), Path(config.root).resolve()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "sample_rank",
        "category",
        "filename",
        "group",
        "english",
        "chinese",
        "english_chars",
        "semantic_fidelity",
        "omission_or_addition",
        "terminology",
        "chinese_fluency",
        "character_tone",
        "severity",
        "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_benchmark(
    index_path: Path,
    out_dir: Path,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: str = DEFAULT_MODEL,
    seed: str = DEFAULT_SEED,
    dry_run: bool = False,
) -> dict[str, Any]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    source_rows = normalize_index(index_path)
    sample = select_sample(source_rows, sample_size, seed=seed)
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    provider = credentials_status()
    started = time.monotonic()
    started_at = utc_now()
    translations: dict[str, str] = {}
    batch_timings: list[dict[str, Any]] = []

    if not dry_run:
        if not provider["configured"]:
            raise RuntimeError(
                "Translation API key is not configured. Run "
                "'python -m app.credentials configure --provider vapi' first."
            )
        client = make_client()
        for start in range(0, len(sample), batch_size):
            batch_rows = sample[start:start + batch_size]
            request_rows = [
                {"id": row["filename"], "english": row["english"]}
                for row in batch_rows
            ]
            t0 = time.monotonic()
            translated = translate_records(
                request_rows,
                model=model,
                client=client,
            )
            elapsed = time.monotonic() - t0
            batch_timings.append(
                {
                    "batch": len(batch_timings) + 1,
                    "count": len(batch_rows),
                    "elapsed_seconds": round(elapsed, 3),
                }
            )
            for row in translated:
                translations[row["id"]] = row["chinese"]

    result_rows: list[dict[str, Any]] = []
    for rank, row in enumerate(sample, 1):
        result_rows.append(
            {
                "sample_rank": rank,
                "category": row["benchmark_category"],
                "filename": row["filename"],
                "group": row["group"],
                "english": row["english"],
                "chinese": translations.get(row["filename"], ""),
                "english_chars": len(row["english"]),
                # Intentionally blank. Translation quality is judged directly;
                # official Chinese text is not treated as a single gold answer.
                "semantic_fidelity": "",
                "omission_or_addition": "",
                "terminology": "",
                "chinese_fluency": "",
                "character_tone": "",
                "severity": "",
                "notes": "",
            }
        )

    elapsed_total = time.monotonic() - started
    category_counts = Counter(row["category"] for row in result_rows)
    report = {
        "schema_version": 1,
        "kind": "translation_quality_benchmark",
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": round(elapsed_total, 3),
        "dry_run": dry_run,
        "source_index": str(index_path.resolve()),
        "source_count": len(source_rows),
        "sample_size": len(sample),
        "sample_seed": seed,
        "batch_size": batch_size,
        "model": model,
        "provider": provider["provider"],
        "base_url": provider["base_url"],
        "provider_configured": provider["configured"],
        "categories": dict(category_counts),
        "batch_timings": batch_timings,
        "integrity": {
            "expected_ids": len(sample),
            "returned_ids": len(translations),
            "complete": dry_run or len(translations) == len(sample),
        },
        "evaluation_policy": {
            "official_chinese_is_gold_answer": False,
            "criteria": [
                "semantic_fidelity",
                "omission_or_addition",
                "terminology",
                "chinese_fluency",
                "character_tone",
            ],
            "severity_scale": ["pass", "style", "minor", "major", "critical"],
        },
    }

    atomic_write_text(
        out_dir / "benchmark_report.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )
    _write_csv(out_dir / "benchmark_results.csv", result_rows)
    atomic_write_text(
        out_dir / "benchmark_sample.json",
        json.dumps(
            [
                {
                    "sample_rank": row["sample_rank"],
                    "category": row["category"],
                    "filename": row["filename"],
                    "group": row["group"],
                    "english": row["english"],
                    "chinese": row["chinese"],
                }
                for row in result_rows
            ],
            ensure_ascii=False,
            indent=2,
        ),
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Run a direct English->Chinese translation-quality benchmark against "
            "the configured API provider. Official Chinese text is not scored as a gold answer."
        )
    )
    p.add_argument("--index", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--seed", default=DEFAULT_SEED)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Select/write the benchmark sample without sending any paid API request.",
    )
    args = p.parse_args()

    index, project_root = _resolve_index(args.index)
    out = args.out or (project_root / "translation_benchmark")
    report = run_benchmark(
        index,
        out,
        sample_size=args.sample_size,
        batch_size=args.batch_size,
        model=args.model,
        seed=args.seed,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Results: {(out.expanduser().resolve() / 'benchmark_results.csv')}")


if __name__ == "__main__":
    main()
