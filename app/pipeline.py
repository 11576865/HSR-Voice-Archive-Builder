from __future__ import annotations

import argparse
import csv
import json
import tempfile
from pathlib import Path

from .builder import (
    atomic_write_text,
    build_continuous_flac,
    build_entries,
    ensure_dir_or_extract,
    write_csv_rows,
    write_manifest,
)
from .identity import parse_voice_identity
from .schema import write_legacy_inputs


def _augment_outputs(entries, report: dict[str, object], out_dir: Path) -> None:
    variants = 0
    payload = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    for row, entry in zip(payload["entries"], entries, strict=True):
        ident = parse_voice_identity(entry.filename, entry.group)
        row["logical_id"] = ident.logical_id
        row["variant"] = ident.variant
        variants += bool(ident.variant)
    report["count_variants"] = variants
    payload["report"] = report
    atomic_write_text(
        out_dir / "manifest.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )

    rows = payload["entries"]
    fields = list(rows[0].keys()) if rows else []
    write_csv_rows(out_dir / "manifest.csv", rows, fields)

    corrected = out_dir / "bilingual_index_corrected.csv"
    with corrected.open("r", encoding="utf-8-sig", newline="") as f:
        old_rows = list(csv.DictReader(f))
    fields = ["index", "start", "audio_end", "display_end", "group", "filename", "logical_id", "variant", "chinese_source", "chinese", "english", "source_duration_seconds", "sha256"]
    updated_rows = []
    for old, entry in zip(old_rows, entries, strict=True):
        ident = parse_voice_identity(entry.filename, entry.group)
        old["logical_id"] = ident.logical_id
        old["variant"] = ident.variant
        updated_rows.append(old)
    write_csv_rows(corrected, updated_rows, fields)


def _translate_missing(entries, model: str, batch_size: int) -> int:
    targets = [{"id": e.filename, "english": e.english} for e in entries if not e.chinese]
    if not targets:
        return 0
    from .translator import translate_in_batches
    translated = translate_in_batches(targets, model=model, batch_size=batch_size)
    by_id = {row["id"]: row["chinese"].strip() for row in translated}
    if set(by_id) != {row["id"] for row in targets}:
        raise RuntimeError("Translator returned a different ID set")
    for e in entries:
        if not e.chinese:
            text = by_id.get(e.filename, "")
            if not text:
                raise RuntimeError(f"Translator returned empty Chinese text: {e.filename}")
            e.chinese = text
            e.chinese_source = f"gpt:{model}"
    return len(targets)


def build_project_v02(
    index_csv: Path,
    wav_source: Path,
    out_dir: Path,
    bilingual_csv: Path | None = None,
    chs_source: Path | None = None,
    same_group_gap: float = 0.40,
    group_gap: float = 1.20,
    make_flac: bool = True,
    translate_missing: bool = False,
    translation_model: str = "gpt-5.6-luna",
    translation_batch_size: int = 80,
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hsr_v02_") as td:
        work = Path(td)
        legacy_index, legacy_bilingual = write_legacy_inputs(index_csv, bilingual_csv, work / "schema")
        empty_chs = work / "empty_chs"
        empty_chs.mkdir()
        chs_root = ensure_dir_or_extract(chs_source, work, "chs") if chs_source else empty_chs
        wav_root = ensure_dir_or_extract(wav_source, work, "wavs")
        entries, report = build_entries(
            legacy_index,
            legacy_bilingual,
            chs_root,
            wav_root,
            same_group_gap=same_group_gap,
            group_gap=group_gap,
        )
        if translate_missing:
            report["count_gpt_translated"] = _translate_missing(entries, translation_model, translation_batch_size)
            report["count_missing_chinese"] = sum(not e.chinese for e in entries)
        write_manifest(entries, report, out_dir)
        _augment_outputs(entries, report, out_dir)
        if make_flac:
            report.update(build_continuous_flac(entries, wav_root, out_dir / "continuous.flac"))
            write_manifest(entries, report, out_dir)
            _augment_outputs(entries, report, out_dir)
        atomic_write_text(
            out_dir / "build_report.json",
            json.dumps(report, ensure_ascii=False, indent=2),
        )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="HSR Voice Archive Builder v0.2 pipeline")
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--wavs", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--bilingual", type=Path)
    p.add_argument("--chs", type=Path)
    p.add_argument("--same-gap", type=float, default=0.40)
    p.add_argument("--group-gap", type=float, default=1.20)
    p.add_argument("--no-flac", action="store_true")
    p.add_argument("--translate-missing", action="store_true")
    p.add_argument("--translation-model", default="gpt-5.6-luna")
    p.add_argument("--translation-batch-size", type=int, default=80)
    a = p.parse_args()
    result = build_project_v02(
        a.index, a.wavs, a.out, a.bilingual, a.chs,
        a.same_gap, a.group_gap, not a.no_flac,
        a.translate_missing, a.translation_model, a.translation_batch_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
