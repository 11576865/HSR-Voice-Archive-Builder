from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
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
from .credentials import translation_default_model
from .identity import parse_voice_identity
from .human_review import HumanReviewRequired, write_review_txt
from .semantic_quality import SEMANTIC_QA_VERSION
from .schema import write_legacy_inputs
from .stages import build_fingerprint, load_stage, path_fingerprint, save_stage
from .subtitles import refresh_subtitle_artifacts_from_settings


STAGE_FILES = {
    "scan": "01_scan.json",
    "metadata": "02_metadata.json",
    "translation": "03_translation.json",
    "translation_qa": "04_translation_qa.json",
    "manifest": "05_manifest.json",
    "audio": "06_audio_state.json",
    "final": "final_report.json",
}

LEGACY_STATE_ITEMS = (
    ".translation_checkpoint.json",
    "translation_qa.json",
    "semantic_qa.json",
    "translation_usage.json",
    "stages",
)


def _migrate_legacy_state(out_dir: Path, state_dir: Path) -> list[str]:
    """Move legacy internal state out of the user-facing output directory.

    Existing state at the new destination always wins. Conflicting legacy
    files are left untouched rather than overwritten.
    """
    out_dir = out_dir.resolve()
    state_dir = state_dir.resolve()
    if out_dir == state_dir:
        return []
    state_dir.mkdir(parents=True, exist_ok=True)
    migrated: list[str] = []
    for name in LEGACY_STATE_ITEMS:
        source = out_dir / name
        destination = state_dir / name
        if not source.exists() or destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        migrated.append(name)
    return migrated



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
        row["source_text"] = entry.english
        row["target_text"] = entry.chinese
        row["target_text_source"] = (
            "official_target_lab"
            if entry.chinese_source == "official_chs_lab"
            else entry.chinese_source
        )
        row["reference_text"] = str(getattr(entry, "reference_text", "") or "")
        row["reference_language"] = str(
            getattr(entry, "reference_language", "auto") or "auto"
        )
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
    fields = [
        "index", "start", "audio_end", "display_end", "group", "filename",
        "logical_id", "variant",
        "target_text_source", "target_text", "source_text",
        "reference_language", "reference_text",
        "chinese_source", "chinese", "english",
        "source_duration_seconds", "sha256",
    ]
    updated_rows = []
    for old, entry in zip(old_rows, entries, strict=True):
        ident = parse_voice_identity(entry.filename, entry.group)
        old["logical_id"] = ident.logical_id
        old["variant"] = ident.variant
        old["source_text"] = entry.english
        old["target_text"] = entry.chinese
        old["target_text_source"] = (
            "official_target_lab"
            if entry.chinese_source == "official_chs_lab"
            else entry.chinese_source
        )
        old["reference_text"] = str(getattr(entry, "reference_text", "") or "")
        old["reference_language"] = str(
            getattr(entry, "reference_language", "auto") or "auto"
        )
        updated_rows.append(old)
    write_csv_rows(corrected, updated_rows, fields)


def _text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _translation_input_fingerprint(
    row: dict[str, str],
    source_language: str,
    target_language: str,
) -> str:
    payload = {
        "english": str(row.get("english", "")),
        "reference_text": str(row.get("reference_text", "")),
        "reference_language": str(row.get("reference_language", "")),
        "source_language": source_language,
        "target_language": target_language,
    }
    return _text_fingerprint(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _load_translation_checkpoint(
    path: Path,
    model: str,
    provider: str,
    base_url: str,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}

    schema = payload.get("schema_version")
    if schema == 1:
        # v0.6 checkpoints implicitly meant English -> Simplified Chinese.
        if source_language != "en" or target_language != "zh-CN":
            return {}
        if provider != "openai" or base_url != "https://api.openai.com/v1":
            return {}
        if payload.get("model") != model:
            return {}
    elif schema == 2:
        # v0.9-D checkpoints implicitly meant English -> Simplified Chinese.
        if (
            source_language != "en"
            or target_language != "zh-CN"
            or payload.get("model") != model
            or payload.get("provider") != provider
            or payload.get("base_url") != base_url
        ):
            return {}
    elif schema == 3:
        if (
            payload.get("model") != model
            or payload.get("provider") != provider
            or payload.get("base_url") != base_url
            or payload.get("source_language") != source_language
            or payload.get("target_language") != target_language
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
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> None:
    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": 3,
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "source_language": source_language,
                "target_language": target_language,
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
    # two rows related merely because they are physically adjacent. getattr()
    # keeps the helper compatible with older/injected lightweight row objects.
    current_group = str(getattr(current, "group", "") or "")
    neighbor_group = str(getattr(neighbor, "group", "") or "")
    current_detail = str(getattr(current, "source_detail", "") or "")
    neighbor_detail = str(getattr(neighbor, "source_detail", "") or "")
    same_group = bool(
        current_group
        and neighbor_group
        and current_group == neighbor_group
    )
    same_detail = bool(
        current_detail
        and neighbor_detail
        and current_detail == neighbor_detail
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
        reference_text = str(getattr(entry, "reference_text", "") or "").strip()
        if reference_text:
            row["reference_text"] = reference_text
            row["reference_language"] = str(
                getattr(entry, "reference_language", "auto") or "auto"
            )
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
    from .semantic_quality import SEMANTIC_QA_VERSION, summarize_semantic_qa

    summary = summarize_semantic_qa(records)
    summary["count_semantic_qa_checkpoint_reused"] = int(reused)
    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": 1,
                "semantic_qa_version": SEMANTIC_QA_VERSION,
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
    progress_callback: Callable[[str, str, int, int], None] | None = None,
    source_language: str = "en",
    target_language: str = "zh-CN",
    review_output_path: Path | None = None,
) -> dict[str, object]:
    if batch_size < 1:
        raise ValueError("translation_batch_size must be >= 1")
    targets = _target_records(entries)

    def translation_progress(message: str, current: int = 0, total: int = 0) -> None:
        if progress_callback is not None:
            progress_callback("translation", message, current, total)

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
        (CORE_GLOSSARY if target_language == "zh-CN" else {})
        if translation_glossary is None
        else translation_glossary
    )
    active_glossary_fingerprint = glossary_fingerprint(active_glossary)

    provider, base_url = translation_identity()
    checkpoint = _load_translation_checkpoint(
        checkpoint_path,
        model,
        provider,
        base_url,
        source_language,
        target_language,
    )
    completed: dict[str, str] = {}
    qa_rows: list[dict[str, object]] = []
    semantic_verified_ids: set[str] = set()
    reused = 0

    target_by_id = {row["id"]: row for row in targets}
    for row in targets:
        saved = checkpoint.get(row["id"], {})
        expected = _text_fingerprint(row["english"])
        expected_input = _translation_input_fingerprint(
            row, source_language, target_language
        )
        if not isinstance(saved, dict) or not str(saved.get("chinese", "")).strip():
            continue
        saved_input = str(saved.get("input_sha256", "") or "")
        if saved_input:
            if saved_input != expected_input:
                continue
        elif (
            source_language != "en"
            or target_language != "zh-CN"
            or bool(row.get("reference_text"))
            or saved.get("english_sha256") != expected
        ):
            # Legacy row checkpoints did not include language/reference roles.
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
        issues = translation_qa(row["english"], chinese, row_glossary, target_language)
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
    # Every API-produced target needs a semantic/alignment audit. Deterministic
    # QA catches tags, placeholders and glossary errors, but cannot tell that a
    # fluent Chinese line actually belongs to the next English record.
    semantic_audit_ids = {row["id"] for row in targets}
    semantic_pending_ids = semantic_audit_ids - semantic_verified_ids
    api_translated = 0
    qa_retries = 0
    client = make_client() if (remaining or semantic_pending_ids) else None

    # Alibaba Model Studio's OpenAI-compatible route is more reliable with
    # smaller structured-output batches.  Preserve the configured value for
    # other providers, while applying a safe per-request ceiling here.
    effective_batch_size = batch_size
    if isinstance(client, OpenAIResponsesHTTPClient) and client.api_mode == "chat_completions":
        effective_batch_size = min(batch_size, 20)

    usage_estimate = estimate_workload_tokens(remaining, effective_batch_size)
    semantic_batch_size = max(1, min(effective_batch_size, 40))
    semantic_estimate_rows = [
        {
            "id": row["id"],
            "english": row["english"],
            "chinese": row["english"],
            "risk_tags": semantic_risk_tags(row["english"], source_language),
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
            "source_language": source_language,
            "target_language": target_language,
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
        translation_progress("正在验证翻译 API 能力", 0, max(1, len(remaining)))
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

    translation_batch_count = (
        (len(remaining) + effective_batch_size - 1) // effective_batch_size
        if remaining else 0
    )
    for start in range(0, len(remaining), effective_batch_size):
        batch_number = start // effective_batch_size + 1
        batch = remaining[start:start + effective_batch_size]
        translation_progress(
            f"AI 翻译：正在等待批次 {batch_number}/{translation_batch_count} 返回"
            f"（本批 {len(batch)} 条）· 已完成 {start}/{len(remaining)} 条 · 已复用 {reused} 条",
            start,
            len(remaining),
        )
        batch_glossary = relevant_glossary(
            active_glossary,
            [row["english"] for row in batch],
        )
        request_estimate = estimate_request_tokens(batch, batch_glossary)
        phase = f"translation-batch-{start // effective_batch_size + 1}"
        ledger.check_before_request(request_estimate, phase=phase)
        if isinstance(client, OpenAIResponsesHTTPClient):
            translated = translate_records(
                batch,
                model=model,
                source_language=source_language,
                target_language=target_language,
                glossary=batch_glossary,
                client=client,
                usage_callback=lambda usage, phase=phase: ledger.record(phase, usage),
            )
        else:
            # Test/injected clients predate language/usage callbacks. Production
            # make_client() always returns OpenAIResponsesHTTPClient.
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
                raise RuntimeError(f"Translator returned empty target text: {source['id']}")
            row_glossary = relevant_glossary(active_glossary, [source["english"]])
            issues = translation_qa(source["english"], chinese, row_glossary, target_language)
            first_results[source["id"]] = (chinese, issues)
            api_translated += 1
            if issues:
                repair = dict(source)
                repair["previous_chinese"] = chinese
                repair["qa_issues"] = " | ".join(qa_messages(issues))
                retry_records.append(repair)

        repaired: dict[str, str] = {}
        if retry_records:
            translation_progress(
                f"翻译质量修复：本批 {len(retry_records)} 条 · 批次 {batch_number}/{translation_batch_count}",
                min(start + len(batch), len(remaining)),
                len(remaining),
            )
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
                    source_language=source_language,
                    target_language=target_language,
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
            final_issues = translation_qa(source["english"], chinese, row_glossary, target_language)
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
                "input_sha256": _translation_input_fingerprint(
                    source, source_language, target_language
                ),
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
            source_language,
            target_language,
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
    semantic_hard_failures: list[dict[str, object]] = []
    semantic_reused = len(semantic_audit_ids & semantic_verified_ids)
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
        semantic_batch_count = (
            (len(semantic_targets) + semantic_batch_size - 1) // semantic_batch_size
        )
        for start in range(0, len(semantic_targets), semantic_batch_size):
            semantic_batch_number = start // semantic_batch_size + 1
            translation_progress(
                f"语义检查：已检查 {start}/{len(semantic_targets)} 条 · 批次 {semantic_batch_number}/{semantic_batch_count}",
                start,
                len(semantic_targets),
            )
            batch_targets = semantic_targets[start:start + semantic_batch_size]
            candidates = [
                semantic_candidate(
                    row_id=row["id"],
                    english=row["english"],
                    chinese=completed[row["id"]],
                    source_language=source_language,
                    include_without_risk=True,
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
                source_language=source_language,
                target_language=target_language,
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
                translation_progress(
                    f"语义修复：本批 {len(repair_records)} 条 · 批次 {semantic_batch_number}/{semantic_batch_count}",
                    min(start + len(batch_targets), len(semantic_targets)),
                    len(semantic_targets),
                )
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
                    source_language=source_language,
                    target_language=target_language,
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
                    issues = translation_qa(source["english"], chinese, row_glossary, target_language)
                    if has_hard_issue(issues):
                        deterministic_failures.add(source["id"])
                    repaired_chinese[source["id"]] = chinese
                    completed[source["id"]] = chinese
                    checkpoint[source["id"]] = {
                        "english_sha256": _text_fingerprint(source["english"]),
                        "input_sha256": _translation_input_fingerprint(
                            source, source_language, target_language
                        ),
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
                    source_language=source_language,
                    include_without_risk=True,
                )
                for source in repair_records
                if source["id"] not in deterministic_failures
            ]
            repair_candidates = [row for row in repair_candidates if row is not None]
            final_repair_verdicts: dict[str, dict[str, object]] = {}
            if repair_candidates:
                translation_progress(
                    f"语义复核：本批 {len(repair_candidates)} 条 · 批次 {semantic_batch_number}/{semantic_batch_count}",
                    min(start + len(batch_targets), len(semantic_targets)),
                    len(semantic_targets),
                )
                reverify_phase = f"semantic-reverify-{start // semantic_batch_size + 1}"
                ledger.check_before_request(
                    estimate_request_tokens(repair_candidates),
                    phase=reverify_phase,
                )
                reverified = verify_semantic_records(
                    repair_candidates,
                    model=model,
                    source_language=source_language,
                    target_language=target_language,
                    client=client,
                    usage_callback=lambda usage, phase=reverify_phase: ledger.record(phase, usage),
                )
                final_repair_verdicts = {
                    str(row["id"]): row for row in reverified
                }

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
                    "initial_ok": bool(initial.get("ok")),
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
                    semantic_hard_failures.append(record)
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
                source_language,
                target_language,
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

    if semantic_hard_failures:
        review_path = review_output_path or checkpoint_path.with_name(
            "semantic_review_required.txt"
        )
        write_review_txt(review_path, semantic_hard_failures)
        raise HumanReviewRequired(review_path, len(semantic_hard_failures))

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


def _official_records(entries) -> list[dict[str, str]]:
    """Official target lines that were not produced by our own translator."""
    records: list[dict[str, str]] = []
    for entry in entries:
        official = str(entry.chinese or "").strip()
        english = str(entry.english or "").strip()
        origin = str(getattr(entry, "chinese_source", "") or "")
        if not official or not english or origin.startswith("api:"):
            continue
        records.append({
            "id": entry.filename,
            "english": english,
            "official_chinese": official,
        })
    return records


def _official_zero_counts() -> dict[str, object]:
    return {
        "count_official_reviewed": 0,
        "count_official_zone_green": 0,
        "count_official_zone_yellow": 0,
        "count_official_zone_red": 0,
        "count_official_api_reviewed": 0,
        "count_official_checkpoint_reused": 0,
        "count_official_revised": 0,
        "count_official_revision_rejected": 0,
        "official_review_api_call_count": 0,
        "official_review_actual_total_tokens": 0,
        "official_review_estimated_cost_usd": 0.0,
    }


def _load_official_checkpoint(
    path: Path,
    model: str,
    provider: str,
    base_url: str,
    target_language: str,
) -> dict[str, dict[str, object]]:
    from .official_alignment import OFFICIAL_ALIGNMENT_VERSION

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), dict):
        return {}
    identity = payload.get("identity")
    if identity != {
        "model": model,
        "provider": provider,
        "base_url": base_url,
        "target_language": target_language,
        "alignment_version": OFFICIAL_ALIGNMENT_VERSION,
    }:
        return {}
    return {
        str(key): value
        for key, value in payload["rows"].items()
        if isinstance(value, dict)
    }


def _write_official_checkpoint(
    path: Path,
    model: str,
    provider: str,
    base_url: str,
    target_language: str,
    rows: dict[str, dict[str, object]],
) -> None:
    from .official_alignment import OFFICIAL_ALIGNMENT_VERSION

    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": 1,
                "identity": {
                    "model": model,
                    "provider": provider,
                    "base_url": base_url,
                    "target_language": target_language,
                    "alignment_version": OFFICIAL_ALIGNMENT_VERSION,
                },
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


def _review_official_targets(
    entries,
    *,
    model: str,
    batch_size: int,
    state_dir: Path,
    token_budget: int = 0,
    usd_budget: float = 0.0,
    translation_glossary: dict[str, str] | None = None,
    progress_callback: Callable[[str, str, int, int], None] | None = None,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> dict[str, object]:
    """Compare official target text against the source and revise only deviations.

    Green rows keep the official text without any API call. Yellow and red rows
    go to a single call that returns the accept/revise decision together with a
    replacement line when one is needed.
    """
    if batch_size < 1:
        raise ValueError("translation_batch_size must be >= 1")

    def review_progress(message: str, current: int = 0, total: int = 0) -> None:
        if progress_callback is not None:
            progress_callback("official_review", message, current, total)

    rows = (
        [] if source_language == target_language else _official_records(entries)
    )
    if not rows:
        return _official_zero_counts()

    from .credentials import translation_identity
    from .glossary import CORE_GLOSSARY, glossary_fingerprint, relevant_glossary
    from .official_alignment import (
        OFFICIAL_ALIGNMENT_VERSION,
        ZONE_GREEN,
        official_candidate,
        summarize_official_alignment,
    )
    from .translation_quality import has_hard_issue, translation_qa
    from .translation_runtime import (
        TranslationUsageLedger,
        estimate_request_tokens,
        estimate_workload_tokens,
        records_fingerprint,
    )
    from .translator import make_client, review_official_records

    active_glossary = dict(
        (CORE_GLOSSARY if target_language == "zh-CN" else {})
        if translation_glossary is None
        else translation_glossary
    )
    provider, base_url = translation_identity()
    checkpoint_path = state_dir / ".official_review_checkpoint.json"
    checkpoint = _load_official_checkpoint(
        checkpoint_path, model, provider, base_url, target_language
    )

    entry_by_id = {entry.filename: entry for entry in entries}
    records: list[dict[str, object]] = []
    pending: list[dict[str, object]] = []
    for row in rows:
        candidate = official_candidate(
            row_id=row["id"],
            source_text=row["english"],
            official_text=row["official_chinese"],
            glossary=relevant_glossary(active_glossary, [row["english"]]),
            source_language=source_language,
            target_language=target_language,
        )
        record: dict[str, object] = {
            **candidate,
            "decision": "accept_official",
            "reason": "acceptable_localization",
            "translation": "",
            "api_reviewed": False,
            "checkpoint_reused": False,
            "revision_rejected": False,
        }
        if candidate["zone"] == ZONE_GREEN:
            records.append(record)
            continue
        saved = checkpoint.get(row["id"])
        fingerprint = _text_fingerprint(
            row["english"] + "\u0000" + row["official_chinese"]
        )
        if (
            isinstance(saved, dict)
            and str(saved.get("input_sha256", "")) == fingerprint
            and str(saved.get("decision", "")) in {"accept_official", "revise"}
        ):
            record["decision"] = str(saved["decision"])
            record["reason"] = str(saved.get("reason", ""))
            record["translation"] = str(saved.get("translation", ""))
            record["checkpoint_reused"] = True
            records.append(record)
            continue
        record["input_sha256"] = fingerprint
        records.append(record)
        pending.append(record)

    client = make_client() if pending else None
    review_batch_size = max(1, min(batch_size, 40))
    estimate_rows = [
        {
            "id": str(row["id"]),
            "english": str(row["english"]),
            "chinese": str(row["official_chinese"]),
        }
        for row in pending
    ]
    usage_estimate = estimate_workload_tokens(estimate_rows, review_batch_size)
    ledger = TranslationUsageLedger(
        state_dir / "official_review_usage.json",
        identity={
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "target_fingerprint": records_fingerprint(rows),
            "glossary_fingerprint": glossary_fingerprint(active_glossary),
            "source_language": source_language,
            "target_language": target_language,
            "phase": "official-review",
        },
        estimate=usage_estimate,
        token_budget=token_budget,
        usd_budget=usd_budget,
    )

    batch_count = (len(pending) + review_batch_size - 1) // review_batch_size
    for start in range(0, len(pending), review_batch_size):
        batch = pending[start:start + review_batch_size]
        review_progress(
            f"官方中文对照：已复核 {start}/{len(pending)} 条 · 批次 "
            f"{start // review_batch_size + 1}/{batch_count}",
            start,
            len(pending),
        )
        payload = [
            {
                "id": str(row["id"]),
                "english": str(row["english"]),
                "official_chinese": str(row["official_chinese"]),
                "zone": str(row["zone"]),
                "signals": [
                    f"{item['code']}: {item['message']}"
                    for item in row["signals"]  # type: ignore[union-attr]
                ],
            }
            for row in batch
        ]
        batch_glossary = relevant_glossary(
            active_glossary,
            [str(row["english"]) for row in batch],
        )
        phase = f"official-review-{start // review_batch_size + 1}"
        ledger.check_before_request(
            estimate_request_tokens(payload, batch_glossary),
            phase=phase,
        )
        reviews = review_official_records(
            payload,
            model=model,
            glossary=batch_glossary,
            source_language=source_language,
            target_language=target_language,
            client=client,
            usage_callback=lambda usage, phase=phase: ledger.record(phase, usage),
        )
        for row, review in zip(batch, reviews, strict=True):
            row["api_reviewed"] = True
            row["decision"] = str(review.get("decision", "accept_official"))
            row["reason"] = str(review.get("reason", ""))
            row["translation"] = str(review.get("translation", "")).strip()
            checkpoint[str(row["id"])] = {
                "input_sha256": str(row.get("input_sha256", "")),
                "zone": str(row["zone"]),
                "decision": row["decision"],
                "reason": row["reason"],
                "translation": row["translation"],
                "alignment_version": OFFICIAL_ALIGNMENT_VERSION,
            }
        _write_official_checkpoint(
            checkpoint_path, model, provider, base_url, target_language, checkpoint
        )
        ledger.assert_observable()

    for record in records:
        if record["decision"] != "revise":
            continue
        translation = str(record["translation"]).strip()
        english = str(record["english"])
        issues = translation_qa(
            english,
            translation,
            relevant_glossary(active_glossary, [english]),
            target_language,
        )
        if not translation or has_hard_issue(issues):
            # A retranslation that fails deterministic QA is worse than the
            # official line it would replace.
            record["revision_rejected"] = True
            record["qa_issues"] = issues
            continue
        entry = entry_by_id.get(str(record["id"]))
        if entry is None:
            continue
        entry.chinese = translation
        entry.chinese_source = (
            f"official-review:{provider}:{model}:{record['reason'] or 'revise'}"
        )

    summary = summarize_official_alignment(records)
    atomic_write_text(
        state_dir / "official_review.json",
        json.dumps(
            {
                "schema_version": 1,
                "alignment_version": OFFICIAL_ALIGNMENT_VERSION,
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
    ledger_report = ledger.report()
    return {
        **summary,
        "official_review_api_call_count": ledger_report["translation_api_call_count"],
        "official_review_actual_total_tokens": ledger_report[
            "translation_actual_total_tokens"
        ],
        "official_review_estimated_cost_usd": ledger_report[
            "translation_estimated_cost_usd"
        ],
        "official_review_model": model,
    }


def build_project_v02(
    index_csv: Path,
    wav_source: Path,
    out_dir: Path,
    bilingual_csv: Path | None = None,
    chs_source: Path | None = None,
    same_group_gap: float = 0.40,
    group_gap: float = 1.20,
    intro_gap: float = 5.0,
    make_flac: bool = True,
    generate_ass: bool = False,
    translate_missing: bool = False,
    translation_model: str = translation_default_model(),
    translation_batch_size: int = 80,
    translation_token_budget: int = 0,
    translation_budget_usd: float = 0.0,
    glossary_path: Path | None = None,
    progress_callback: Callable[[str, str, int, int], None] | None = None,
    reference_source: Path | None = None,
    audio_language: str = "auto",
    source_text_language: str = "en",
    target_language: str = "zh-CN",
    reference_language: str = "auto",
    reference_text_embedded: bool = False,
    state_dir: Path | None = None,
    review_official_target: bool = False,
) -> dict[str, object]:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir = state_dir.expanduser().resolve() if state_dir else out_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    migrated_legacy_state = _migrate_legacy_state(out_dir, state_dir)
    index_csv = index_csv.expanduser().resolve()
    wav_source = wav_source.expanduser().resolve()
    bilingual_csv = bilingual_csv.expanduser().resolve() if bilingual_csv else None
    chs_source = chs_source.expanduser().resolve() if chs_source else None
    glossary_path = glossary_path.expanduser().resolve() if glossary_path else None
    reference_source = reference_source.expanduser().resolve() if reference_source else None

    def progress(phase: str, message: str, current: int, total: int = 6) -> None:
        if progress_callback is not None:
            progress_callback(phase, message, current, total)

    progress("prepare", "1/6 正在检查输入与恢复点", 1)

    from .glossary import (
        CORE_GLOSSARY,
        glossary_fingerprint,
        load_glossary_overlay,
        merge_glossary,
    )

    glossary_overlay = load_glossary_overlay(glossary_path)
    built_in_glossary = CORE_GLOSSARY if target_language == "zh-CN" else {}
    active_glossary = merge_glossary(built_in_glossary, glossary_overlay)
    active_glossary_fingerprint = glossary_fingerprint(active_glossary)

    translation_route: dict[str, str] | None = None
    if translate_missing or review_official_target:
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
        "reference_source": path_fingerprint(reference_source),
        "audio_language": audio_language,
        "source_text_language": source_text_language,
        "target_language": target_language,
        "reference_language": reference_language,
        "reference_text_embedded": bool(reference_text_embedded),
        "intro_gap": intro_gap,
        "same_group_gap": same_group_gap,
        "group_gap": group_gap,
        "make_flac": make_flac,
        "generate_ass": generate_ass,
        "translate_missing": translate_missing,
        "review_official_target": bool(review_official_target),
        "translation_route": translation_route,
        "translation_qa_version": 2,
    })
    resumed_stages: list[str] = []
    rebuilt_stages: list[str] = []
    if load_stage(state_dir, STAGE_FILES["scan"], "scan", input_fingerprint) is not None:
        resumed_stages.append("scan")
    else:
        save_stage(
            state_dir,
            STAGE_FILES["scan"],
            "scan",
            input_fingerprint,
            {"inputs": input_state},
        )
        rebuilt_stages.append("scan")

    progress("metadata", "2/6 正在读取语音包与元数据", 2)

    # Keep extraction/work files on the output filesystem instead of the OS
    # temp drive. On Windows/Android the system temp partition is often much
    # smaller than the drive selected for an archive project.
    with tempfile.TemporaryDirectory(prefix=".hsr-work-", dir=out_dir.parent) as td:
        work = Path(td)
        wav_root: Path | None = None
        metadata = _stage_entries(load_stage(
            state_dir, STAGE_FILES["metadata"], "metadata", input_fingerprint
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
            reference_root = (
                ensure_dir_or_extract(reference_source, work, "reference")
                if reference_source and not reference_text_embedded
                else None
            )
            wav_root = ensure_dir_or_extract(wav_source, work, "wavs")
            entries, report = build_entries(
                legacy_index,
                legacy_bilingual,
                chs_root,
                wav_root,
                same_group_gap=same_group_gap,
                group_gap=group_gap,
                intro_gap=intro_gap,
                reference_lab_root=reference_root,
                reference_language=reference_language,
                source_text_language=source_text_language,
                target_language=target_language,
            )
            save_stage(
                state_dir,
                STAGE_FILES["metadata"],
                "metadata",
                input_fingerprint,
                {"entries": _entries_payload(entries), "report": report},
            )
            rebuilt_stages.append("metadata")

        # A selected official Chinese package must identify at least one
        # primary voice before it can trigger AI fallback. This prevents a
        # filename mismatch from silently translating the entire archive.
        if (
            chs_source is not None
            and source_text_language != target_language
            and int(report.get("count_official_chs_lab", 0) or 0) == 0
            and int(report.get("count_missing_chinese", 0) or 0) > 0
            and translate_missing
        ):
            raise RuntimeError(
                "The selected official Chinese package matched 0 primary voices. "
                "API fallback was stopped to avoid translating every line. "
                "Run Quick Scan and choose the corresponding Chinese package."
            )

        progress(
            "translation",
            f"3/6 正在处理 {target_language} 翻译与质量检查" if translate_missing
            else f"3/6 {target_language} 翻译阶段无需 API",
            3,
        )
        translated = _stage_entries(load_stage(
            state_dir, STAGE_FILES["translation"], "translation", input_fingerprint
        ))
        qa_stage = load_stage(
            state_dir,
            STAGE_FILES["translation_qa"],
            "translation_qa",
            input_fingerprint,
        )
        translation_rebuilt = False
        if translate_missing and qa_stage is not None:
            semantic_stage = qa_stage.get("semantic_qa")
            if (
                not isinstance(semantic_stage, dict)
                or int(semantic_stage.get("semantic_qa_version", 0) or 0)
                != SEMANTIC_QA_VERSION
            ):
                # Keep metadata/audio recovery points, but invalidate the old
                # translated entry set. Row checkpoints will be audited and
                # only mismatched translations will incur a repair call.
                translated = None
                qa_stage = None
        if (translate_missing or review_official_target) and qa_stage is None:
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
                    state_dir,
                    STAGE_FILES["translation_qa"],
                    "translation_qa",
                    input_fingerprint,
                    {"skipped": True},
                )
                rebuilt_stages.append("translation_qa")
        else:
            translation_rebuilt = True
            if translate_missing:
                report.update(
                    _translate_missing(
                        entries,
                        translation_model,
                        translation_batch_size,
                        state_dir / ".translation_checkpoint.json",
                        translation_token_budget,
                        translation_budget_usd,
                        active_glossary,
                        progress_callback,
                        source_text_language,
                        target_language,
                        out_dir / "semantic_review_required.txt",
                    )
                )
                report["count_missing_chinese"] = sum(not e.chinese for e in entries)
                report["count_missing_target_text"] = report["count_missing_chinese"]
            if review_official_target:
                report.update(
                    _review_official_targets(
                        entries,
                        model=translation_model,
                        batch_size=translation_batch_size,
                        state_dir=state_dir,
                        token_budget=translation_token_budget,
                        usd_budget=translation_budget_usd,
                        translation_glossary=active_glossary,
                        progress_callback=progress_callback,
                        source_language=source_text_language,
                        target_language=target_language,
                    )
                )
            save_stage(
                state_dir,
                STAGE_FILES["translation"],
                "translation",
                input_fingerprint,
                {"entries": _entries_payload(entries), "report": report},
            )
            qa_path = state_dir / "translation_qa.json"
            semantic_qa_path = state_dir / "semantic_qa.json"
            official_review_path = state_dir / "official_review.json"
            qa_payload: dict[str, object] = {
                "skipped": not (translate_missing or review_official_target)
            }
            qa_artifacts: list[Path] = []
            if qa_path.is_file():
                qa_payload = json.loads(qa_path.read_text(encoding="utf-8"))
                qa_artifacts.append(qa_path)
            if semantic_qa_path.is_file():
                qa_payload["semantic_qa"] = json.loads(
                    semantic_qa_path.read_text(encoding="utf-8")
                )
                qa_artifacts.append(semantic_qa_path)
            if official_review_path.is_file():
                qa_payload["official_review"] = json.loads(
                    official_review_path.read_text(encoding="utf-8")
                )
                qa_artifacts.append(official_review_path)
            save_stage(
                state_dir,
                STAGE_FILES["translation_qa"],
                "translation_qa",
                input_fingerprint,
                qa_payload,
                artifacts=qa_artifacts,
            )
            rebuilt_stages.extend(["translation", "translation_qa"])

        report["audio_language"] = audio_language
        report["source_text_language"] = source_text_language
        report["target_language"] = target_language
        report["reference_language"] = reference_language
        report["reference_text_embedded"] = bool(reference_text_embedded)
        report["count_incremental_official_reference"] = sum(
            bool(str(getattr(entry, "reference_text", "") or "").strip())
            and str(getattr(entry, "reference_language", "") or "").lower()
            in {"zh", "zh-cn", "chs", "cn"}
            for entry in entries
        )
        report["count_unreferenced_api_target_text"] = sum(
            str(getattr(entry, "chinese_source", "") or "").startswith("api:")
            and not str(getattr(entry, "reference_text", "") or "").strip()
            for entry in entries
        )

        progress("manifest", "4/6 正在生成字幕与清单", 4)

        manifest_artifacts = [
            out_dir / "manifest.json",
            out_dir / "manifest.csv",
            out_dir / "bilingual_index_corrected.csv",
            out_dir / "timeline_resolved.json",
            out_dir / "HSR_Voice_Archive.srt",
        ]
        if generate_ass:
            manifest_artifacts.append(out_dir / "HSR_Voice_Archive.ass")

        if not translation_rebuilt and load_stage(
            state_dir,
            STAGE_FILES["manifest"],
            "manifest",
            input_fingerprint,
            artifact_root=out_dir,
        ) is not None:
            resumed_stages.append("manifest")
        else:
            write_manifest(entries, report, out_dir, generate_ass=generate_ass)
            _augment_outputs(entries, report, out_dir)
            refresh_subtitle_artifacts_from_settings(
                out_dir,
                source_language=source_text_language,
                target_language=target_language,
                generate_ass=generate_ass,
            )
            save_stage(
                state_dir,
                STAGE_FILES["manifest"],
                "manifest",
                input_fingerprint,
                {"entry_count": len(entries)},
                artifacts=manifest_artifacts,
                artifact_root=out_dir,
            )
            rebuilt_stages.append("manifest")

        # Human proofreading is a derived layer. Re-apply it even when the
        # manifest stage was resumed from an earlier verified build.
        refresh_subtitle_artifacts_from_settings(
            out_dir,
            source_language=source_text_language,
            target_language=target_language,
            generate_ass=generate_ass,
        )

        progress(
            "audio",
            "5/6 正在构建并验证连续 FLAC" if make_flac
            else "5/6 已跳过连续 FLAC",
            5,
        )
        if make_flac:
            audio = load_stage(
                state_dir,
                STAGE_FILES["audio"],
                "audio",
                input_fingerprint,
                artifact_root=out_dir,
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
                # A previous black MKV contains the old FLAC. Invalidate it
                # only after the replacement FLAC has encoded and verified.
                (out_dir / "HSR_Voice_Archive_Black.mkv").unlink(missing_ok=True)
                report.update(audio_report)
                save_stage(
                    state_dir,
                    STAGE_FILES["audio"],
                    "audio",
                    input_fingerprint,
                    {"report": audio_report},
                    artifacts=[out_dir / "continuous.flac"],
                    artifact_root=out_dir,
                )
                rebuilt_stages.append("audio")
            write_manifest(entries, report, out_dir, generate_ass=generate_ass)
            _augment_outputs(entries, report, out_dir)
            save_stage(
                state_dir,
                STAGE_FILES["manifest"],
                "manifest",
                input_fingerprint,
                {"entry_count": len(entries)},
                artifacts=manifest_artifacts,
                artifact_root=out_dir,
            )
        elif load_stage(
            state_dir,
            STAGE_FILES["audio"],
            "audio",
            input_fingerprint,
            artifact_root=out_dir,
        ) is not None:
            resumed_stages.append("audio")
        else:
            save_stage(
                state_dir,
                STAGE_FILES["audio"],
                "audio",
                input_fingerprint,
                {"skipped": True, "report": {}},
            )
            rebuilt_stages.append("audio")

        progress("final", "6/6 正在写入最终报告", 6)

        report["stage_resume"] = {
            "input_fingerprint": input_fingerprint,
            "resumed": resumed_stages,
            "rebuilt": rebuilt_stages,
        }
        report["state_layout"] = {
            "separate_from_output": state_dir != out_dir,
            "migrated_legacy_items": migrated_legacy_state,
        }
        atomic_write_text(
            out_dir / "build_report.json",
            json.dumps(report, ensure_ascii=False, indent=2),
        )
        save_stage(
            state_dir,
            STAGE_FILES["final"],
            "final_report",
            input_fingerprint,
            {"report": report},
            artifacts=[out_dir / "build_report.json"],
            artifact_root=out_dir,
        )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="HSR Voice Archive Builder v0.9-I pipeline")
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--wavs", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--state-dir", type=Path)
    p.add_argument("--bilingual", type=Path)
    p.add_argument("--chs", type=Path)
    p.add_argument("--intro-gap", type=float, default=5.0)
    p.add_argument("--same-gap", type=float, default=0.40)
    p.add_argument("--group-gap", type=float, default=1.20)
    p.add_argument("--no-flac", action="store_true")
    p.add_argument("--generate-ass", action="store_true", help="Generate ASS subtitle file")
    p.add_argument("--translate-missing", action="store_true")
    p.add_argument("--review-official-target", action="store_true")
    p.add_argument("--translation-model", default=translation_default_model())
    p.add_argument("--translation-batch-size", type=int, default=80)
    p.add_argument("--translation-token-budget", type=int, default=0)
    p.add_argument("--translation-budget-usd", type=float, default=0.0)
    p.add_argument("--glossary", type=Path)
    p.add_argument("--reference", type=Path)
    p.add_argument("--audio-language", default="auto")
    p.add_argument("--source-language", default="en")
    p.add_argument("--target-language", default="zh-CN")
    p.add_argument("--reference-language", default="auto")
    a = p.parse_args()
    result = build_project_v02(
        a.index, a.wavs, a.out,
        bilingual_csv=a.bilingual,
        chs_source=a.chs,
        same_group_gap=a.same_gap,
        group_gap=a.group_gap,
        intro_gap=a.intro_gap,
        make_flac=not a.no_flac,
        generate_ass=a.generate_ass,
        translate_missing=a.translate_missing,
        review_official_target=a.review_official_target,
        translation_model=a.translation_model,
        translation_batch_size=a.translation_batch_size,
        translation_token_budget=a.translation_token_budget,
        translation_budget_usd=a.translation_budget_usd,
        glossary_path=a.glossary,
        reference_source=a.reference,
        audio_language=a.audio_language,
        source_text_language=a.source_language,
        target_language=a.target_language,
        reference_language=a.reference_language,
        state_dir=a.state_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
