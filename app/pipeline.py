from __future__ import annotations

import argparse
import csv
import hashlib
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


def _text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_translation_checkpoint(
    path: Path,
    model: str,
    provider: str,
    base_url: str,
) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}

    schema = payload.get("schema_version")
    if schema == 1:
        # v0.6 checkpoints were created only against the official OpenAI URL.
        # Reuse them only when that identity is still selected.
        if provider != "openai" or base_url != "https://api.openai.com/v1":
            return {}
        if payload.get("model") != model:
            return {}
    elif schema == 2:
        if (
            payload.get("model") != model
            or payload.get("provider") != provider
            or payload.get("base_url") != base_url
        ):
            return {}
    else:
        return {}

    records = payload.get("records", {})
    return records if isinstance(records, dict) else {}


def _write_translation_checkpoint(
    path: Path,
    model: str,
    provider: str,
    base_url: str,
    records: dict[str, dict[str, str]],
) -> None:
    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": 2,
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


def _translation_counts(total: int, reused: int, api_translated: int) -> dict[str, int]:
    # Generic names are authoritative in v0.7. The older count_gpt_* aliases are
    # retained so existing dashboards/reports do not break immediately.
    return {
        "count_api_translated": total,
        "count_api_checkpoint_reused": reused,
        "count_api_new_translations": api_translated,
        "count_gpt_translated": total,
        "count_gpt_checkpoint_reused": reused,
        "count_gpt_api_translated": api_translated,
    }


def _target_records(entries) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for i, entry in enumerate(entries):
        if entry.chinese:
            continue
        row = {
            "id": entry.filename,
            "english": entry.english,
        }
        if i > 0:
            row["context_before"] = entries[i - 1].english
        if i + 1 < len(entries):
            row["context_after"] = entries[i + 1].english
        records.append(row)
    return records


def _write_qa_report(
    path: Path,
    *,
    provider: str,
    base_url: str,
    model: str,
    records: list[dict[str, object]],
) -> dict[str, int]:
    from .translation_quality import summarize_qa

    summary = summarize_qa(records)
    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": 1,
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "summary": summary,
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return summary


def _translate_missing(
    entries,
    model: str,
    batch_size: int,
    checkpoint_path: Path,
) -> dict[str, object]:
    if batch_size < 1:
        raise ValueError("translation_batch_size must be >= 1")
    targets = _target_records(entries)
    if not targets:
        return {
            **_translation_counts(0, 0, 0),
            "translation_provider": "",
            "translation_base_url": "",
            "translation_model": model,
            "count_translation_qa_records": 0,
            "count_translation_qa_warnings": 0,
            "count_translation_qa_hard_failed": 0,
            "count_translation_qa_retried": 0,
        }

    from .credentials import translation_identity
    from .glossary import CORE_GLOSSARY, relevant_glossary
    from .translation_quality import (
        has_hard_issue,
        qa_messages,
        translation_qa,
    )
    from .translator import make_client, translate_records

    provider, base_url = translation_identity()
    checkpoint = _load_translation_checkpoint(
        checkpoint_path,
        model,
        provider,
        base_url,
    )
    completed: dict[str, str] = {}
    qa_rows: list[dict[str, object]] = []
    reused = 0

    target_by_id = {row["id"]: row for row in targets}
    for row in targets:
        saved = checkpoint.get(row["id"], {})
        expected = _text_fingerprint(row["english"])
        if not (
            isinstance(saved, dict)
            and saved.get("english_sha256") == expected
            and str(saved.get("chinese", "")).strip()
        ):
            continue
        chinese = str(saved["chinese"]).strip()
        glossary = relevant_glossary(CORE_GLOSSARY, [row["english"]])
        issues = translation_qa(row["english"], chinese, glossary)
        if has_hard_issue(issues):
            # A new glossary/QA rule can invalidate an old cached translation.
            # Re-run only this item instead of trusting stale paid output.
            continue
        completed[row["id"]] = chinese
        reused += 1
        qa_rows.append({
            "id": row["id"],
            "english": row["english"],
            "chinese": chinese,
            "issues": issues,
            "retried": False,
            "checkpoint_reused": True,
            "hard_failed": False,
        })

    remaining = [row for row in targets if row["id"] not in completed]
    api_translated = 0
    qa_retries = 0
    client = make_client() if remaining else None

    for start in range(0, len(remaining), batch_size):
        batch = remaining[start:start + batch_size]
        batch_glossary = relevant_glossary(
            CORE_GLOSSARY,
            [row["english"] for row in batch],
        )
        translated = translate_records(
            batch,
            model=model,
            glossary=batch_glossary,
            client=client,
        )

        retry_records: list[dict[str, str]] = []
        first_results: dict[str, tuple[str, list[dict[str, str]]]] = {}
        for source, result in zip(batch, translated, strict=True):
            if result["id"] != source["id"]:
                raise RuntimeError(
                    f"Translation order mismatch: {result['id']} != {source['id']}"
                )
            chinese = str(result["chinese"]).strip()
            if not chinese:
                raise RuntimeError(f"Translator returned empty Chinese text: {source['id']}")
            glossary = relevant_glossary(CORE_GLOSSARY, [source["english"]])
            issues = translation_qa(source["english"], chinese, glossary)
            first_results[source["id"]] = (chinese, issues)
            api_translated += 1
            if issues:
                repair = dict(source)
                repair["previous_chinese"] = chinese
                repair["qa_issues"] = " | ".join(qa_messages(issues))
                retry_records.append(repair)

        repaired: dict[str, str] = {}
        if retry_records:
            retry_glossary = relevant_glossary(
                CORE_GLOSSARY,
                [row["english"] for row in retry_records],
            )
            retried_rows = translate_records(
                retry_records,
                model=model,
                glossary=retry_glossary,
                client=client,
            )
            qa_retries += len(retry_records)
            repaired = {row["id"]: str(row["chinese"]).strip() for row in retried_rows}

        hard_failures: list[dict[str, object]] = []
        for source in batch:
            first_chinese, first_issues = first_results[source["id"]]
            chinese = repaired.get(source["id"], first_chinese)
            glossary = relevant_glossary(CORE_GLOSSARY, [source["english"]])
            final_issues = translation_qa(source["english"], chinese, glossary)
            hard_failed = has_hard_issue(final_issues)
            record = {
                "id": source["id"],
                "english": source["english"],
                "chinese": chinese,
                "issues": final_issues,
                "initial_issues": first_issues,
                "retried": source["id"] in repaired,
                "checkpoint_reused": False,
                "hard_failed": hard_failed,
            }
            qa_rows.append(record)

            checkpoint[source["id"]] = {
                "english_sha256": _text_fingerprint(source["english"]),
                "chinese": chinese,
                "qa_version": 1,
                "qa_issues": final_issues,
            }
            if hard_failed:
                hard_failures.append(record)
            else:
                completed[source["id"]] = chinese

        # Persist successful and failed QA results after every batch. A later
        # network/process failure never discards already-paid translations.
        _write_translation_checkpoint(
            checkpoint_path,
            model,
            provider,
            base_url,
            checkpoint,
        )
        qa_summary = _write_qa_report(
            checkpoint_path.with_name("translation_qa.json"),
            provider=provider,
            base_url=base_url,
            model=model,
            records=qa_rows,
        )

        if hard_failures:
            first = hard_failures[0]
            raise RuntimeError(
                f"Translation QA still has {len(hard_failures)} hard failure(s) after one repair pass; "
                f"first={first['id']}. See translation_qa.json."
            )

    wanted = {row["id"] for row in targets}
    if set(completed) != wanted:
        raise RuntimeError(
            f"Translation checkpoint/result mismatch: missing={wanted-set(completed)}"
        )

    for entry in entries:
        if not entry.chinese:
            text = completed.get(entry.filename, "")
            if not text:
                raise RuntimeError(f"Missing completed translation: {entry.filename}")
            entry.chinese = text
            entry.chinese_source = f"api:{provider}:{model}:qa"

    qa_summary = _write_qa_report(
        checkpoint_path.with_name("translation_qa.json"),
        provider=provider,
        base_url=base_url,
        model=model,
        records=qa_rows,
    )

    return {
        **_translation_counts(len(targets), reused, api_translated),
        **qa_summary,
        "count_translation_qa_api_retries": qa_retries,
        "translation_provider": provider,
        "translation_base_url": base_url,
        "translation_model": model,
        "translation_glossary_terms": len(CORE_GLOSSARY),
        "translation_context_neighbors": True,
    }


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
    translation_model: str = "gpt-5.6-sol",
    translation_batch_size: int = 80,
) -> dict[str, object]:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Keep extraction/work files on the output filesystem instead of the OS
    # temp drive. On Windows/Android the system temp partition is often much
    # smaller than the drive selected for an archive project.
    with tempfile.TemporaryDirectory(prefix=".hsr-work-", dir=out_dir.parent) as td:
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
            report.update(
                _translate_missing(
                    entries,
                    translation_model,
                    translation_batch_size,
                    out_dir / ".translation_checkpoint.json",
                )
            )
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
    p = argparse.ArgumentParser(description="HSR Voice Archive Builder v0.8 pipeline")
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--wavs", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--bilingual", type=Path)
    p.add_argument("--chs", type=Path)
    p.add_argument("--same-gap", type=float, default=0.40)
    p.add_argument("--group-gap", type=float, default=1.20)
    p.add_argument("--no-flac", action="store_true")
    p.add_argument("--translate-missing", action="store_true")
    p.add_argument("--translation-model", default="gpt-5.6-sol")
    p.add_argument("--translation-batch-size", type=int, default=80)
    a = p.parse_args()
    result = build_project_v02(
        a.index, a.wavs, a.out, a.bilingual, a.chs,
        a.same_gap, a.group_gap, not a.no_flac,
        a.translate_missing, a.translation_model, a.translation_batch_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
