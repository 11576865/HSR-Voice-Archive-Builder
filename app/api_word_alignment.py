"""Explicit user-paid, opt-in word timestamps through a transcription API.

This is NOT forced alignment: ASR may disagree with the project's authoritative
source text. Never persist such a response unless every word covers that text
in order and the bounds pass the existing word-alignment validator.
"""
from __future__ import annotations

import json
import math
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .credentials import load_translation_credentials
from .local_word_alignment import (
    _entry_id, _manifest_entries, _normalize_language_code, _source_text,
)
from .project import ProjectConfig
from .reference_workbench import resolve_reference_audio
from .word_alignment import (
    alignment_diagnostics, import_word_alignments, validate_word_alignment,
)

MAX_REQUEST_ITEMS = 5
MAX_AUDIO_BYTES = 24 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PROVIDER_ID = "whisper-api-words"


def api_word_alignment_status() -> dict[str, Any]:
    """Configuration is necessary but NOT evidence that transcription works."""
    creds = load_translation_credentials()
    return {
        "provider": PROVIDER_ID, "configured": bool(creds.api_key),
        "base_url": creds.base_url, "model": "whisper-1",
        "verified": False, "uploads_audio": True, "potential_cost": True,
        "supports_exact_text": False,
        "note": "按需将原音频上传到当前 API 服务商；只有与原文完整匹配的词时间才会被接受。",
    }


def _transcribe_words(
    audio_path: Path, source_text: str, language: str,
    *, api_key: str, base_url: str, timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """One bounded multipart transcription, with no silent fallback or retry."""
    size = audio_path.stat().st_size
    if not 0 < size <= MAX_AUDIO_BYTES:
        raise ValueError("单条语音必须非空且不超过 24 MiB")
    audio = audio_path.read_bytes()
    boundary = "HSR" + uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in (
        ("model", "whisper-1"),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"),
        ("language", language),
        ("prompt", source_text[:800]),
    ):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n".encode("utf-8")
        )
    name = audio_path.name.replace("\r", "").replace("\n", "").replace('"', "")
    mime = mimetypes.guess_type(name)[0] or "audio/wav"
    if mime not in {"audio/wav", "audio/x-wav", "audio/flac", "audio/mpeg",
                    "audio/ogg", "audio/mp4", "audio/webm", "video/mp4"}:
        raise ValueError("音频格式不受转写接口支持")
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode("utf-8") + audio + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    request = Request(
        base_url.rstrip("/") + "/audio/transcriptions",
        data=b"".join(parts),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            # An API error or oversized JSON must never poison the local cache.
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        try:
            msg = exc.read(1024).decode("utf-8", "replace")
        except Exception:
            msg = ""
        raise RuntimeError(
            f"转写服务返回 HTTP {exc.code}；请检查供应商是否支持 whisper-1 逐词时间。"
            + (" " + msg[:160] if msg else "")
        ) from None
    except (OSError, URLError) as exc:
        raise RuntimeError(f"转写请求失败：{type(exc).__name__}") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("转写服务响应超过大小限制")
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise ValueError("转写服务未返回有效 JSON") from exc
    words = payload.get("words") if isinstance(payload, dict) else None
    if not isinstance(words, list) or not words:
        raise ValueError("转写服务未提供逐词时间；需要 whisper-1 verbose_json word timestamps")
    result: list[dict[str, Any]] = []
    for row in words:
        if not isinstance(row, dict):
            raise ValueError("转写词条格式错误")
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("转写词条缺少有效时间范围") from exc
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start:
            raise ValueError("转写词条出现非法时间范围")
        result.append({"word": str(row.get("word") or ""), "start": start, "end": end})
    return result


def generate_api_word_alignments(
    config: ProjectConfig, output_dir: Path, *,
    item_ids: list[str] | None,
    report_progress: Callable[[str, str, int, int], None] | None = None,
    transcribe: Callable[..., list[dict[str, Any]]] = _transcribe_words,
) -> dict[str, Any]:
    if not item_ids or len(item_ids) > MAX_REQUEST_ITEMS:
        raise ValueError("API 逐词生成必须明确选择 1–5 条语音，不允许默认批量上传整个项目")
    creds = load_translation_credentials()
    if not creds.api_key:
        raise ValueError("未配置自己的 API Key；无法启动逐词转写")
    language = _normalize_language_code(config.source_text_language)
    lookup = {_entry_id(row): row for row in _manifest_entries(output_dir)}
    generated: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for index, item_id in enumerate(item_ids, 1):
        if report_progress:
            report_progress("api-word-align", f"正在上传并校验语音 {index}/{len(item_ids)}", index - 1, len(item_ids))
        try:
            entry = lookup.get(str(item_id))
            if not entry:
                raise ValueError("当前条目不在项目清单中")
            source = _source_text(entry)
            if not source:
                raise ValueError("原文为空，无法验证转写")
            audio_path, _ = resolve_reference_audio(config, output_dir, str(item_id))
            words = transcribe(
                audio_path, source, language,
                api_key=creds.api_key, base_url=creds.base_url,
            )
            # Do not silently project ASR timestamps onto words it did not say.
            duration = float(
                entry.get("source_duration_seconds") or entry.get("audio_duration_seconds")
                or entry.get("duration_seconds") or 0
            )
            verdict = validate_word_alignment(source, duration, words)
            if not verdict["valid"]:
                raise ValueError("转写结果不能安全用于当前字幕：" + str(verdict["reason"]))
            generated.append({"id": str(item_id), "provider": PROVIDER_ID, "words": words})
        except Exception as exc:
            failed.append({"id": str(item_id), "reason": f"{type(exc).__name__}: {exc}"[:500]})
        if report_progress:
            report_progress("api-word-align", f"已处理 {index}/{len(item_ids)}", index, len(item_ids))
    saved = import_word_alignments(output_dir, {"alignments": generated}) if generated else None
    return {
        "provider": PROVIDER_ID, "requested_count": len(item_ids),
        "generated_count": len(generated), "failed_count": len(failed),
        "failed": failed, "import": saved,
        "diagnostics": {k: v for k, v in alignment_diagnostics(output_dir).items() if k != "entries"},
    }
