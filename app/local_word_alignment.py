from __future__ import annotations

import importlib.metadata
import importlib.util
import json
from pathlib import Path
from typing import Any, Callable

from .project import ProjectConfig
from .reference_workbench import resolve_reference_audio
from .word_alignment import alignment_diagnostics, import_word_alignments

PROVIDER_ID = "whisperx-local"
DEFAULT_SAMPLE_RATE = 16000


class LocalAlignmentUnavailable(RuntimeError):
    pass


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def local_alignment_provider_status() -> dict[str, Any]:
    whisperx_installed = importlib.util.find_spec("whisperx") is not None
    torch_installed = importlib.util.find_spec("torch") is not None
    available = whisperx_installed and torch_installed
    missing = []
    if not whisperx_installed:
        missing.append("whisperx")
    if not torch_installed:
        missing.append("torch")
    return {
        "provider": PROVIDER_ID,
        "available": available,
        "whisperx_installed": whisperx_installed,
        "whisperx_version": _package_version("whisperx") if whisperx_installed else "",
        "torch_installed": torch_installed,
        "torch_version": _package_version("torch") if torch_installed else "",
        "missing_packages": missing,
        "network_inference": False,
        "note": (
            "WhisperX forced alignment runs on the processing host against the project's "
            "existing source text. The first use may need an alignment model to be present "
            "locally (WhisperX may download one if its own cache is empty)."
        ),
        "install_hint": "python -m pip install whisperx",
    }


def _normalize_language_code(value: str) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "chs": "zh",
        "zh-cn": "zh",
        "zh-hans": "zh",
        "cht": "zh",
        "zh-tw": "zh",
        "zh-hant": "zh",
        "jp": "ja",
        "jpn": "ja",
        "kr": "ko",
        "kor": "ko",
        "eng": "en",
    }
    if raw in aliases:
        return aliases[raw]
    if raw and raw != "auto":
        return raw.split("-", 1)[0]
    raise ValueError(
        "Local forced alignment requires an explicit source_text_language; 'auto' is not sufficient"
    )


def _load_whisperx_modules():
    status = local_alignment_provider_status()
    if not status["available"]:
        missing = ", ".join(status["missing_packages"]) or "WhisperX dependencies"
        raise LocalAlignmentUnavailable(
            f"Local forced alignment is unavailable because {missing} is not installed"
        )
    try:
        import torch  # type: ignore
        import whisperx  # type: ignore
    except Exception as exc:
        raise LocalAlignmentUnavailable(
            f"WhisperX could not be imported: {type(exc).__name__}: {exc}"
        ) from exc
    return whisperx, torch


def _manifest_entries(output_dir: Path) -> list[dict[str, Any]]:
    path = output_dir / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError("Manifest file not found in project output directory")
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        raise ValueError("Manifest entries must be a list")
    return [entry for entry in entries if isinstance(entry, dict)]


def _entry_id(entry: dict[str, Any]) -> str:
    value = entry.get("index")
    if value is None:
        value = entry.get("id") or entry.get("filename") or ""
    return str(value)


def _source_text(entry: dict[str, Any]) -> str:
    return str(entry.get("source_text") or entry.get("english") or "").strip()


def _word_rows_from_whisperx(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    raw_words = result.get("word_segments")
    if not isinstance(raw_words, list):
        raw_words = []
        segments = result.get("segments")
        if isinstance(segments, list):
            for segment in segments:
                if isinstance(segment, dict) and isinstance(segment.get("words"), list):
                    raw_words.extend(segment["words"])

    words: list[dict[str, Any]] = []
    for raw in raw_words:
        if not isinstance(raw, dict):
            return []
        if raw.get("start") is None or raw.get("end") is None:
            return []
        word = str(raw.get("word") or "")
        if not word:
            return []
        try:
            item: dict[str, Any] = {
                "word": word,
                "start": float(raw["start"]),
                "end": float(raw["end"]),
            }
        except (TypeError, ValueError):
            return []
        score = raw.get("score")
        if score is None:
            score = raw.get("confidence")
        if score is not None:
            try:
                item["confidence"] = float(score)
            except (TypeError, ValueError):
                pass
        words.append(item)
    return words


def _align_one(
    whisperx: Any,
    *,
    model_a: Any,
    metadata: Any,
    device: str,
    audio_path: Path,
    text: str,
) -> list[dict[str, Any]]:
    audio = whisperx.load_audio(str(audio_path))
    try:
        sample_rate = int(getattr(getattr(whisperx, "audio", None), "SAMPLE_RATE", DEFAULT_SAMPLE_RATE))
    except (TypeError, ValueError):
        sample_rate = DEFAULT_SAMPLE_RATE
    try:
        sample_count = len(audio)
    except TypeError as exc:
        raise RuntimeError("WhisperX returned audio without a measurable sample length") from exc
    duration = max(0.01, float(sample_count) / max(1, sample_rate))
    segments = [{"text": text, "start": 0.0, "end": duration}]
    try:
        result = whisperx.align(
            segments,
            model_a,
            metadata,
            audio,
            device,
            return_char_alignments=False,
        )
    except TypeError:
        # Compatibility with older WhisperX releases whose align() signature
        # does not expose return_char_alignments.
        result = whisperx.align(segments, model_a, metadata, audio, device)
    words = _word_rows_from_whisperx(result)
    if not words:
        raise RuntimeError("WhisperX did not return complete word-level start/end timings")
    return words


def generate_local_word_alignments(
    config: ProjectConfig,
    output_dir: Path,
    *,
    item_ids: list[str | int] | None = None,
    force: bool = False,
    report_progress: Callable[[str, str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Generate exact-text forced alignments locally with optional WhisperX.

    Existing valid cache entries are preserved unless force=True. Failed rows
    are reported and never replaced with guessed timings.
    """
    whisperx, torch = _load_whisperx_modules()
    language = _normalize_language_code(config.source_text_language)
    device = "cuda" if bool(getattr(torch.cuda, "is_available", lambda: False)()) else "cpu"

    entries = _manifest_entries(output_dir)
    wanted = {str(value) for value in (item_ids or []) if str(value)}
    diagnostics = alignment_diagnostics(output_dir)
    valid_ids = {
        str(row.get("id"))
        for row in diagnostics.get("entries", [])
        if isinstance(row, dict) and bool(row.get("valid"))
    }

    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    found_ids: set[str] = set()
    for entry in entries:
        item_id = _entry_id(entry)
        if wanted and item_id not in wanted:
            continue
        found_ids.add(item_id)
        text = _source_text(entry)
        if not text:
            skipped.append({"id": item_id, "reason": "source_text_empty"})
            continue
        if not force and item_id in valid_ids:
            skipped.append({"id": item_id, "reason": "already_valid"})
            continue
        selected.append(entry)

    for missing_id in sorted(wanted - found_ids):
        skipped.append({"id": missing_id, "reason": "entry_not_found"})

    total = len(selected)
    if report_progress:
        report_progress("align-model", "正在加载本地 WhisperX 对齐模型", 0, max(1, total))

    try:
        model_a, metadata = whisperx.load_align_model(
            language_code=language,
            device=device,
        )
    except Exception as exc:
        raise LocalAlignmentUnavailable(
            f"WhisperX alignment model could not be loaded for language '{language}': "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    generated: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for index, entry in enumerate(selected, start=1):
        item_id = _entry_id(entry)
        if report_progress:
            report_progress(
                "align",
                f"正在对齐词级时间 {index}/{total} · 字幕 #{item_id}",
                index - 1,
                max(1, total),
            )
        try:
            audio_path, _ = resolve_reference_audio(config, output_dir, item_id)
            words = _align_one(
                whisperx,
                model_a=model_a,
                metadata=metadata,
                device=device,
                audio_path=audio_path,
                text=_source_text(entry),
            )
            generated.append({
                "id": item_id,
                "provider": PROVIDER_ID,
                "words": words,
            })
        except Exception as exc:
            failed.append({
                "id": item_id,
                "reason": f"{type(exc).__name__}: {exc}",
            })
        if report_progress:
            report_progress(
                "align",
                f"已处理词级时间 {index}/{total}",
                index,
                max(1, total),
            )

    imported: dict[str, Any] | None = None
    if generated:
        imported = import_word_alignments(
            output_dir,
            {"alignments": generated},
        )

    final_diagnostics = alignment_diagnostics(output_dir)
    return {
        "provider": PROVIDER_ID,
        "device": device,
        "language": language,
        "requested_count": total,
        "generated_count": len(generated),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "failed": failed,
        "skipped": skipped,
        "import": imported,
        "diagnostics": {
            key: value
            for key, value in final_diagnostics.items()
            if key != "entries"
        },
    }
