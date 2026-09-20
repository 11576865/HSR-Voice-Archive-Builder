from __future__ import annotations

import argparse
import csv
import hashlib
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from .builder import (
    Entry,
    atomic_write_text,
    build_continuous_flac,
    build_entries,
    ensure_dir_or_extract,
    write_csv_rows,
    write_manifest,
)
from .identity import parse_voice_identity
from .schema import write_legacy_inputs
from .stages import build_fingerprint, load_stage, path_fingerprint, save_stage


STAGE_FILES = {
    "scan": "01_scan.json",
    "metadata": "02_metadata.json",
    "translation": "03_translation.json",
    "translation_qa": "04_translation_qa.json",
    "manifest": "05_manifest.json",
    "audio": "06_audio_state.json",
    "final": "final_report.json",
}


def _entries_payload(entries: list[Entry]) -> list[dict[str, object]]:
    return [asdict(entry) for entry in entries]


def _restore_entries(payload: object) -> list[Entry] | None:
    if not isinstance(payload, list):
        return None
    try:
        rows = [row for row in payload if isinstance(row, dict)]
        if len(rows) != len(payload):
            return None
        return [Entry(**row) for row in rows]
    except (TypeError, ValueError):
        return None


def _stage_entries(
    payload: dict[str, object] | None,
) -> tuple[list[Entry], dict[str, object]] | None:
    if payload is None or not isinstance(payload.get("report"), dict):
        return None
    entries = _restore_entries(payload.get("entries"))
    if entries is None:
        return None
    return entries, dict(payload["report"])


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


def _context_related(current: Entry, neighbor: Entry) -> bool:
    # Context must have explicit structural evidence. Empty labels never make
    # two rows related merely because they are physically adjacent.
    same_group = bool(
        current.group
        and neighbor.group
        and current.group == neighbor.group
    )
    same_detail = bool(
        current.source_detail
        and neighbor.source_detail
        and current.source_detail == neighbor.source_detail
    )
    return same_group or same_detail


def _target_records(entries) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for i, entry in enumerate(entries):
        if entry.chinese:
            continue
        row = {
            "id": entry.filename,
            "english": entry.english,
        }
        if i > 0 and _context_related(entry, entries[i - 1]):
            row["context_before"] = entries[i - 1].english
        if i + 1 < len(entries) and _context_related(entry, entries[i + 1]):
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


def _write_semantic_qa_report(
    path: Path,
    *,
    provider: str,
    base_url: str,
    model: str,
    records: list[dict[str, object]],
    reused: int = 0,
) -> dict[str, int]:
    from .semantic_quality import summarize_semantic_qa

    summary = summarize_semantic_qa(records)
    summary["count_semantic_qa_checkpoint_reused"] = int(reused)
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
    token_budget: int = 0,
    usd_budget: float = 0.0,
    translation_glossary: dict[str, str] | None = None,
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
            "count_semantic_qa_candidates": 0,
            "count_semantic_qa_failed": 0,
            "count_semantic_qa_repaired": 0,
            "count_semantic_qa_hard_failed": 0,
            "count_semantic_qa_checkpoint_reused": 0,
            "count_semantic_qa_skipped_injected": 0,
            "translation_estimated_input_tokens": 0,
            "translation_estimated_output_tokens": 0,
            "translation_estimated_total_tokens": 0,
            "translation_estimated_api_calls": 0,
            "translation_actual_input_tokens": 0,
            "translation_actual_output_tokens": 0,
            "translation_actual_total_tokens": 0,
            "translation_api_call_count": 0,
            "translation_estimated_cost_usd": 0.0,
            "translation_token_budget": max(0, int(token_budget)),
            "translation_budget_usd": max(0.0, float(usd_budget)),
        }

    from .credentials import translation_identity
    from .glossary import CORE_GLOSSARY, glossary_fingerprint, relevant_glossary
    from .semantic_quality import (
        SEMANTIC_QA_VERSION,
        semantic_candidate,
        semantic_risk_tags,
    )
    from .translation_quality import (
        has_hard_issue,
        qa_messages,
        translation_qa,
    )
    from .translation_runtime import (
        TranslationBudgetExceeded,
        TranslationUsageLedger,
        estimate_request_tokens,
        estimate_workload_tokens,
        records_fingerprint,
    )
    from .translator import (
        OpenAIResponsesHTTPClient,
        ensure_translation_capability,
        make_client,
        translate_records,
        verify_semantic_records,
    )

    active_glossary = dict(
        CORE_GLOSSARY if translation_glossary is None else translation_glossary
    )
    active_glossary_fingerprint = glossary_fingerprint(active_glossary)

    provider, base_url = translation_identity()
    checkpoint = _load_translation_checkpoint(
        checkpoint_path,
        model,
        provider,
        base_url,
    )
    completed: dict[str, str] = {}
    qa_rows: list[dict[str, object]] = []
    semantic_verified_ids: set[str] = set()
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
        row_glossary = relevant_glossary(active_glossary, [row["english"]])
        row_glossary_fingerprint = glossary_fingerprint(row_glossary)
        saved_glossary_fingerprint = str(saved.get("glossary_fingerprint", "")).strip()
        if (
            saved_glossary_fingerprint
            and saved_glossary_fingerprint != row_glossary_fingerprint
        ):
            continue
        issues = translation_qa(row["english"], chinese, row_glossary)
        if has_hard_issue(issues):
            # A new glossary/QA rule can invalidate an old cached translation.
            # Re-run only this item instead of trusting stale paid output.
            continue
        completed[row["id"]] = chinese
        if int(saved.get("semantic_qa_version", 0) or 0) == SEMANTIC_QA_VERSION:
            semantic_verified_ids.add(row["id"])
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
    semantic_risky_ids = {
        row["id"] for row in targets if semantic_risk_tags(row["english"])
    }
    semantic_pending_ids = semantic_risky_ids - semantic_verified_ids
    api_translated = 0
    qa_retries = 0
    client = make_client() if (remaining or semantic_pending_ids) else None

    usage_estimate = estimate_workload_tokens(remaining, batch_size)
    semantic_batch_size = max(1, min(batch_size, 40))
    semantic_estimate_rows = [
        {
            "id": row["id"],
            "english": row["english"],
            "chinese": row["english"],
            "risk_tags": semantic_risk_tags(row["english"]),
        }
        for row in targets
        if row["id"] in semantic_pending_ids
    ]
    semantic_estimate = estimate_workload_tokens(
        semantic_estimate_rows,
        semantic_batch_size,
    )
    usage_estimate["translation_estimated_input_tokens"] += int(
        semantic_estimate["translation_estimated_input_tokens"]
    )
    usage_estimate["translation_estimated_output_tokens"] += int(
        semantic_estimate["translation_estimated_output_tokens"]
    )
    usage_estimate["translation_estimated_total_tokens"] += int(
        semantic_estimate["translation_estimated_total_tokens"]
    )
    usage_estimate["translation_estimated_api_calls"] += int(
        semantic_estimate["translation_estimated_api_calls"]
    )
    usage_estimate["translation_estimate_includes_semantic_verifier"] = True
    ledger = TranslationUsageLedger(
        checkpoint_path.with_name("translation_usage.json"),
        identity={
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "target_fingerprint": records_fingerprint(targets),
            "glossary_fingerprint": active_glossary_fingerprint,
        },
        estimate=usage_estimate,
        token_budget=token_budget,
        usd_budget=usd_budget,
    )

    capability: dict[str, object] = {
        "cached": True,
        "usage_supported": True,
    }
    if (remaining or semantic_pending_ids) and isinstance(client, OpenAIResponsesHTTPClient):
        smoke = [{"id": "smoke-1", "english": "The story's not finished."}]
        smoke_estimate = estimate_request_tokens(smoke)
        capability = ensure_translation_capability(
            model,
            client=client,
            before_request_callback=lambda: ledger.check_before_request(
                smoke_estimate,
                phase="capability-smoke",
            ),
            usage_callback=lambda usage: ledger.record("capability-smoke", usage),
        )
        if ledger.budget_active and not capability.get("usage_supported"):
            raise TranslationBudgetExceeded(
                "The cached capability result says this provider/model does not expose "
                "token usage, so a token/USD budget cannot be enforced safely."
            )

    for start in range(0, len(remaining), batch_size):
        batch = remaining[start:start + batch_size]
        batch_glossary = relevant_glossary(
            active_glossary,
            [row["english"] for row in batch],
        )
        request_estimate = estimate_request_tokens(batch, batch_glossary)
        phase = f"translation-batch-{start // batch_size + 1}"
        ledger.check_before_request(request_estimate, phase=phase)
        if isinstance(client, OpenAIResponsesHTTPClient):
            translated = translate_records(
                batch,
                model=model,
                glossary=batch_glossary,
                client=client,
                usage_callback=lambda usage, phase=phase: ledger.record(phase, usage),
            )
        else:
            # Test/injected clients predate usage callbacks. Production make_client()
            # always returns OpenAIResponsesHTTPClient.
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
            row_glossary = relevant_glossary(active_glossary, [source["english"]])
            issues = translation_qa(source["english"], chinese, row_glossary)
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
                active_glossary,
                [row["english"] for row in retry_records],
            )
            repair_phase = f"repair-batch-{start // batch_size + 1}"
            ledger.check_before_request(
                estimate_request_tokens(retry_records, retry_glossary),
                phase=repair_phase,
            )
            if isinstance(client, OpenAIResponsesHTTPClient):
                retried_rows = translate_records(
                    retry_records,
                    model=model,
                    glossary=retry_glossary,
                    client=client,
                    usage_callback=lambda usage, phase=repair_phase: ledger.record(phase, usage),
                )
            else:
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
            row_glossary = relevant_glossary(active_glossary, [source["english"]])
            final_issues = translation_qa(source["english"], chinese, row_glossary)
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
                "glossary_fingerprint": glossary_fingerprint(row_glossary),
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

        ledger.assert_observable()

        if hard_failures:
            first = hard_failures[0]
            raise RuntimeError(
                f"Translation QA still has {len(hard_failures)} hard failure(s) after one repair pass; "
                f"first={first['id']}. See translation_qa.json."
            )

    semantic_rows: list[dict[str, object]] = []
    semantic_reused = len(semantic_risky_ids & semantic_verified_ids)
    semantic_skipped_injected = 0

    if semantic_pending_ids and not isinstance(client, OpenAIResponsesHTTPClient):
        # Historical unit tests inject a minimal object client and mock only the
        # translation function. Production make_client() always returns the REST
        # client, so semantic verification is never skipped in a real build.
        semantic_skipped_injected = len(semantic_pending_ids)
    elif semantic_pending_ids:
        semantic_targets = [
            row for row in targets if row["id"] in semantic_pending_ids
        ]
        for start in range(0, len(semantic_targets), semantic_batch_size):
            batch_targets = semantic_targets[start:start + semantic_batch_size]
            candidates = [
                semantic_candidate(
                    row_id=row["id"],
                    english=row["english"],
                    chinese=completed[row["id"]],
                )
                for row in batch_targets
            ]
            candidates = [row for row in candidates if row is not None]
            if not candidates:
                continue

            verify_phase = f"semantic-verify-{start // semantic_batch_size + 1}"
            ledger.check_before_request(
                estimate_request_tokens(candidates),
                phase=verify_phase,
            )
            verdicts = verify_semantic_records(
                candidates,
                model=model,
                client=client,
                usage_callback=lambda usage, phase=verify_phase: ledger.record(phase, usage),
            )
            initial_by_id = {str(row["id"]): row for row in verdicts}
            failed_ids = {
                row_id
                for row_id, verdict in initial_by_id.items()
                if not bool(verdict.get("ok"))
            }

            repair_records: list[dict[str, str]] = []
            for source in batch_targets:
                if source["id"] not in failed_ids:
                    continue
                verdict = initial_by_id[source["id"]]
                repair = dict(source)
                repair["previous_chinese"] = completed[source["id"]]
                repair["qa_issues"] = (
                    "Semantic verifier: "
                    + ", ".join(str(x) for x in verdict.get("issues", []))
                    + " | "
                    + str(verdict.get("note", ""))
                )
                repair_records.append(repair)

            repaired_chinese: dict[str, str] = {}
            deterministic_failures: set[str] = set()
            if repair_records:
                semantic_repair_glossary = relevant_glossary(
                    active_glossary,
                    [row["english"] for row in repair_records],
                )
                repair_phase = f"semantic-repair-{start // semantic_batch_size + 1}"
                ledger.check_before_request(
                    estimate_request_tokens(repair_records, semantic_repair_glossary),
                    phase=repair_phase,
                )
                repaired_rows = translate_records(
                    repair_records,
                    model=model,
                    glossary=semantic_repair_glossary,
                    client=client,
                    usage_callback=lambda usage, phase=repair_phase: ledger.record(phase, usage),
                )
                for source, result in zip(repair_records, repaired_rows, strict=True):
                    chinese = str(result["chinese"]).strip()
                    row_glossary = relevant_glossary(
                        active_glossary,
                        [source["english"]],
                    )
                    issues = translation_qa(source["english"], chinese, row_glossary)
                    if has_hard_issue(issues):
                        deterministic_failures.add(source["id"])
                    repaired_chinese[source["id"]] = chinese
                    completed[source["id"]] = chinese
                    checkpoint[source["id"]] = {
                        "english_sha256": _text_fingerprint(source["english"]),
                        "chinese": chinese,
                        "qa_version": 1,
                        "qa_issues": issues,
                        "glossary_fingerprint": glossary_fingerprint(row_glossary),
                    }
                    for qa_row in qa_rows:
                        if qa_row.get("id") == source["id"]:
                            qa_row["chinese"] = chinese
                            qa_row["issues"] = issues
                            qa_row["semantic_repaired"] = True
                            qa_row["hard_failed"] = has_hard_issue(issues)
                            break

            repair_candidates = [
                semantic_candidate(
                    row_id=source["id"],
                    english=source["english"],
                    chinese=completed[source["id"]],
                )
                for source in repair_records
                if source["id"] not in deterministic_failures
            ]
            repair_candidates = [row for row in repair_candidates if row is not None]
            final_repair_verdicts: dict[str, dict[str, object]] = {}
            if repair_candidates:
                reverify_phase = f"semantic-reverify-{start // semantic_batch_size + 1}"
                ledger.check_before_request(
                    estimate_request_tokens(repair_candidates),
                    phase=reverify_phase,
                )
                reverified = verify_semantic_records(
                    repair_candidates,
                    model=model,
                    client=client,
                    usage_callback=lambda usage, phase=reverify_phase: ledger.record(phase, usage),
                )
                final_repair_verdicts = {
                    str(row["id"]): row for row in reverified
                }

            hard_failures: list[dict[str, object]] = []
            for candidate in candidates:
                row_id = str(candidate["id"])
                initial = initial_by_id[row_id]
                repaired = row_id in repaired_chinese
                final = final_repair_verdicts.get(row_id, initial)
                hard_failed = (
                    row_id in deterministic_failures
                    or not bool(final.get("ok"))
                )
                record: dict[str, object] = {
                    "id": row_id,
                    "english": candidate["english"],
                    "chinese": completed[row_id],
                    "risk_tags": candidate["risk_tags"],
                    "ok": not hard_failed,
                    "issues": initial.get("issues", []),
                    "note": initial.get("note", ""),
                    "repaired": repaired,
                    "final_issues": final.get("issues", []),
                    "final_note": final.get("note", ""),
                    "hard_failed": hard_failed,
                }
                semantic_rows.append(record)
                if hard_failed:
                    hard_failures.append(record)
                else:
                    semantic_verified_ids.add(row_id)
                    saved = checkpoint.get(row_id)
                    if isinstance(saved, dict):
                        saved["semantic_qa_version"] = SEMANTIC_QA_VERSION

            _write_translation_checkpoint(
                checkpoint_path,
                model,
                provider,
                base_url,
                checkpoint,
            )
            _write_semantic_qa_report(
                checkpoint_path.with_name("semantic_qa.json"),
                provider=provider,
                base_url=base_url,
                model=model,
                records=semantic_rows,
                reused=semantic_reused,
            )
            _write_qa_report(
                checkpoint_path.with_name("translation_qa.json"),
                provider=provider,
                base_url=base_url,
                model=model,
                records=qa_rows,
            )
            ledger.assert_observable()

            if hard_failures:
                first = hard_failures[0]
                raise RuntimeError(
                    f"Semantic QA still has {len(hard_failures)} hard failure(s) "
                    f"after one targeted repair; first={first['id']}. "
                    "See semantic_qa.json."
                )

    semantic_summary = _write_semantic_qa_report(
        checkpoint_path.with_name("semantic_qa.json"),
        provider=provider,
        base_url=base_url,
        model=model,
        records=semantic_rows,
        reused=semantic_reused,
    )
    semantic_summary["count_semantic_qa_skipped_injected"] = semantic_skipped_injected

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
        **semantic_summary,
        **ledger.report(),
        "translation_capability_probe_cached": bool(capability.get("cached", True)),
        "translation_capability_usage_supported": bool(capability.get("usage_supported", True)),
        "count_translation_qa_api_retries": qa_retries,
        "translation_provider": provider,
        "translation_base_url": base_url,
        "translation_model": model,
        "translation_glossary_terms": len(active_glossary),
        "translation_glossary_fingerprint": active_glossary_fingerprint,
        "translation_context_neighbors": "group-aware",
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
    translation_token_budget: int = 0,
    translation_budget_usd: float = 0.0,
    glossary_path: Path | None = None,
) -> dict[str, object]:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    index_csv = index_csv.expanduser().resolve()
    wav_source = wav_source.expanduser().resolve()
    bilingual_csv = bilingual_csv.expanduser().resolve() if bilingual_csv else None
    chs_source = chs_source.expanduser().resolve() if chs_source else None
    glossary_path = glossary_path.expanduser().resolve() if glossary_path else None

    from .glossary import (
        CORE_GLOSSARY,
        glossary_fingerprint,
        load_glossary_overlay,
        merge_glossary,
    )

    glossary_overlay = load_glossary_overlay(glossary_path)
    active_glossary = merge_glossary(CORE_GLOSSARY, glossary_overlay)
    active_glossary_fingerprint = glossary_fingerprint(active_glossary)

    translation_route: dict[str, str] | None = None
    if translate_missing:
        from .credentials import translation_identity

        provider, base_url = translation_identity()
        translation_route = {
            "provider": provider,
            "base_url": base_url,
            "model": translation_model,
        }
    input_fingerprint, input_state = build_fingerprint({
        "index": path_fingerprint(index_csv),
        "wavs": path_fingerprint(wav_source),
        "bilingual": path_fingerprint(bilingual_csv),
        "chinese": path_fingerprint(chs_source),
        "glossary": path_fingerprint(glossary_path),
        "glossary_fingerprint": active_glossary_fingerprint,
        "same_group_gap": same_group_gap,
        "group_gap": group_gap,
        "make_flac": make_flac,
        "translate_missing": translate_missing,
        "translation_route": translation_route,
        "translation_qa_version": 2,
    })
    resumed_stages: list[str] = []
    rebuilt_stages: list[str] = []
    if load_stage(out_dir, STAGE_FILES["scan"], "scan", input_fingerprint) is not None:
        resumed_stages.append("scan")
    else:
        save_stage(
            out_dir,
            STAGE_FILES["scan"],
            "scan",
            input_fingerprint,
            {"inputs": input_state},
        )
        rebuilt_stages.append("scan")

    # Keep extraction/work files on the output filesystem instead of the OS
    # temp drive. On Windows/Android the system temp partition is often much
    # smaller than the drive selected for an archive project.
    with tempfile.TemporaryDirectory(prefix=".hsr-work-", dir=out_dir.parent) as td:
        work = Path(td)
        wav_root: Path | None = None
        metadata = _stage_entries(load_stage(
            out_dir, STAGE_FILES["metadata"], "metadata", input_fingerprint
        ))
        if metadata is not None:
            entries, report = metadata
            resumed_stages.append("metadata")
        else:
            legacy_index, legacy_bilingual = write_legacy_inputs(
                index_csv, bilingual_csv, work / "schema"
            )
            empty_chs = work / "empty_chs"
            empty_chs.mkdir()
            chs_root = (
                ensure_dir_or_extract(chs_source, work, "chs")
                if chs_source
                else empty_chs
            )
            wav_root = ensure_dir_or_extract(wav_source, work, "wavs")
            entries, report = build_entries(
                legacy_index,
                legacy_bilingual,
                chs_root,
                wav_root,
                same_group_gap=same_group_gap,
                group_gap=group_gap,
            )
            save_stage(
                out_dir,
                STAGE_FILES["metadata"],
                "metadata",
                input_fingerprint,
                {"entries": _entries_payload(entries), "report": report},
            )
            rebuilt_stages.append("metadata")

        translated = _stage_entries(load_stage(
            out_dir, STAGE_FILES["translation"], "translation", input_fingerprint
        ))
        qa_stage = load_stage(
            out_dir,
            STAGE_FILES["translation_qa"],
            "translation_qa",
            input_fingerprint,
        )
        if translate_missing and qa_stage is None:
            # A translated entry set without its validated QA stage is not a
            # complete translation result. Re-run from metadata; row-level
            # translation checkpoints still prevent duplicate paid calls.
            translated = None
        if translated is not None:
            entries, report = translated
            resumed_stages.append("translation")
            if qa_stage is not None:
                resumed_stages.append("translation_qa")
            else:
                save_stage(
                    out_dir,
                    STAGE_FILES["translation_qa"],
                    "translation_qa",
                    input_fingerprint,
                    {"skipped": True},
                )
                rebuilt_stages.append("translation_qa")
        else:
            if translate_missing:
                report.update(
                    _translate_missing(
                        entries,
                        translation_model,
                        translation_batch_size,
                        out_dir / ".translation_checkpoint.json",
                        translation_token_budget,
                        translation_budget_usd,
                        active_glossary,
                    )
                )
                report["count_missing_chinese"] = sum(not e.chinese for e in entries)
            save_stage(
                out_dir,
                STAGE_FILES["translation"],
                "translation",
                input_fingerprint,
                {"entries": _entries_payload(entries), "report": report},
            )
            qa_path = out_dir / "translation_qa.json"
            semantic_qa_path = out_dir / "semantic_qa.json"
            qa_payload: dict[str, object] = {"skipped": not translate_missing}
            qa_artifacts: list[Path] = []
            if qa_path.is_file():
                qa_payload = json.loads(qa_path.read_text(encoding="utf-8"))
                qa_artifacts.append(qa_path)
            if semantic_qa_path.is_file():
                qa_payload["semantic_qa"] = json.loads(
                    semantic_qa_path.read_text(encoding="utf-8")
                )
                qa_artifacts.append(semantic_qa_path)
            save_stage(
                out_dir,
                STAGE_FILES["translation_qa"],
                "translation_qa",
                input_fingerprint,
                qa_payload,
                artifacts=qa_artifacts,
            )
            rebuilt_stages.extend(["translation", "translation_qa"])

        manifest_artifacts = [
            out_dir / "manifest.json",
            out_dir / "manifest.csv",
            out_dir / "bilingual_index_corrected.csv",
            out_dir / "bilingual.srt",
        ]
        if load_stage(
            out_dir, STAGE_FILES["manifest"], "manifest", input_fingerprint
        ) is not None:
            resumed_stages.append("manifest")
        else:
            write_manifest(entries, report, out_dir)
            _augment_outputs(entries, report, out_dir)
            save_stage(
                out_dir,
                STAGE_FILES["manifest"],
                "manifest",
                input_fingerprint,
                {"entry_count": len(entries)},
                artifacts=manifest_artifacts,
            )
            rebuilt_stages.append("manifest")

        if make_flac:
            audio = load_stage(
                out_dir, STAGE_FILES["audio"], "audio", input_fingerprint
            )
            if audio is not None and isinstance(audio.get("report"), dict):
                report.update(audio["report"])
                resumed_stages.append("audio")
            else:
                if wav_root is None:
                    wav_root = ensure_dir_or_extract(wav_source, work, "wavs")
                audio_report = build_continuous_flac(
                    entries, wav_root, out_dir / "continuous.flac"
                )
                report.update(audio_report)
                save_stage(
                    out_dir,
                    STAGE_FILES["audio"],
                    "audio",
                    input_fingerprint,
                    {"report": audio_report},
                    artifacts=[out_dir / "continuous.flac"],
                )
                rebuilt_stages.append("audio")
            write_manifest(entries, report, out_dir)
            _augment_outputs(entries, report, out_dir)
            save_stage(
                out_dir,
                STAGE_FILES["manifest"],
                "manifest",
                input_fingerprint,
                {"entry_count": len(entries)},
                artifacts=manifest_artifacts,
            )
        elif load_stage(
            out_dir, STAGE_FILES["audio"], "audio", input_fingerprint
        ) is not None:
            resumed_stages.append("audio")
        else:
            save_stage(
                out_dir,
                STAGE_FILES["audio"],
                "audio",
                input_fingerprint,
                {"skipped": True, "report": {}},
            )
            rebuilt_stages.append("audio")

        report["stage_resume"] = {
            "input_fingerprint": input_fingerprint,
            "resumed": resumed_stages,
            "rebuilt": rebuilt_stages,
        }
        atomic_write_text(
            out_dir / "build_report.json",
            json.dumps(report, ensure_ascii=False, indent=2),
        )
        save_stage(
            out_dir,
            STAGE_FILES["final"],
            "final_report",
            input_fingerprint,
            {"report": report},
            artifacts=[out_dir / "build_report.json"],
        )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="HSR Voice Archive Builder v0.9-C pipeline")
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
    p.add_argument("--translation-token-budget", type=int, default=0)
    p.add_argument("--translation-budget-usd", type=float, default=0.0)
    p.add_argument("--glossary", type=Path)
    a = p.parse_args()
    result = build_project_v02(
        a.index, a.wavs, a.out, a.bilingual, a.chs,
        a.same_gap, a.group_gap, not a.no_flac,
        a.translate_missing, a.translation_model, a.translation_batch_size,
        a.translation_token_budget, a.translation_budget_usd, a.glossary,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
