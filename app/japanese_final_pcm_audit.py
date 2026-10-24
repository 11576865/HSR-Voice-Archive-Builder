"""Read-only, FULL-stream output PCM verification for isolated Japanese recovery.

This verifies exact bytes in the *final* lossless media against the builder's
recorded assembly hash. Source package provenance is checked separately. It
does not recognize language, inspect translation meaning, or prove every
original source member was mapped to the correct semantic row.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .builder import _pcm_format
from .japanese_listening_pack import _flac_stream, _media
from .japanese_source_recovery import (
    verify_japanese_recovery_build_inputs,
    verify_japanese_recovery_build_manifest,
)
from .project import ProjectConfig, resolve_project_path

_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_japanese_final_audio_pcm(
    config: ProjectConfig,
    *,
    report_progress: Callable[[str, str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Stream-decode the exact current FLAC/MKV, matching its *entire* PCM.

    Neither the finished media nor the original package is edited. A mismatch
    raises (the owning background job must not end as a successful job).
    """
    initial = verify_japanese_recovery_build_inputs(config)
    if initial is None:
        raise ValueError("仅支持具备独立日语恢复来源证明的工程")
    verified = verify_japanese_recovery_build_manifest(config, initial)
    if verified is None:
        raise ValueError("日语恢复成品来源成员验收尚未完成")
    output = resolve_project_path(config, config.output_dir)
    if output is None:
        raise ValueError("恢复工程输出路径缺失")
    manifest = output / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("恢复成品 Manifest 缺失")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        raise ValueError("恢复成品 Manifest 结构不正确")
    report = document.get("report")
    if not isinstance(report, dict):
        raise ValueError("Manifest 缺少构建器音频校验报告")
    original_pcm = str(report.get("assembled_pcm_sha256") or "").lower()
    builder_decoded = str(report.get("decoded_flac_pcm_sha256") or "").lower()
    if (
        not _SHA.fullmatch(original_pcm)
        or builder_decoded != original_pcm
        or report.get("lossless_pcm_verified") is not True
    ):
        raise ValueError("Manifest 无有效的编码前 PCM 与构建时 FLAC 一致性证据")

    entries = document["entries"]
    if not entries or not isinstance(entries[0], dict):
        raise ValueError("Manifest 音频条目为空")
    try:
        sample_rate = int(entries[0]["sample_rate"])
        channels = int(entries[0]["channels"])
        bit_width = int(entries[0]["sample_width_bits"])
        expected_frames = int(report["pcm_frames_written"])
        expected_end = int(entries[-1]["next_start_sample"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Manifest 缺少可信的音频格式、时间线或总帧数") from exc
    if (
        not 1 <= channels <= 8 or not 8000 <= sample_rate <= 192000
        or bit_width not in (8, 16, 24, 32)
        or expected_frames < 1 or expected_end != expected_frames
    ):
        raise ValueError("Manifest 的 PCM 格式或总帧数不可信")
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("Manifest 含无效音频条目")
        try:
            if (
                int(row["sample_rate"]) != sample_rate
                or int(row["channels"]) != channels
                or int(row["sample_width_bits"]) != bit_width
            ):
                raise ValueError("Manifest 含跨条目混合 PCM 格式，拒绝错误验证")
        except (TypeError, KeyError) as exc:
            raise ValueError("Manifest 中音频格式不完整") from exc

    bytes_per_frame = channels * (bit_width // 8)
    expected_bytes = expected_frames * bytes_per_frame
    if expected_bytes <= 0:
        raise ValueError("Manifest 记录的期望 PCM 字节数无效")
    manifest_sha_before = _sha256(manifest)
    media, _kind = _media(config, output)
    stream_id = _flac_stream(media, _kind)
    media_stat_before = media.stat()
    if media_stat_before.st_size <= 0:
        raise ValueError("最终成品没有媒体字节")

    pcm_format, pcm_codec = _pcm_format(bit_width // 8)
    progress_step = 64 * 1024 * 1024
    next_progress = progress_step
    total_bytes = 0
    digest = hashlib.sha256()
    if report_progress:
        report_progress("verify-full-pcm", "开始解码当前最终成品全部音频帧", 0, expected_bytes)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(media), "-map", stream_id, "-vn", "-sn", "-dn",
             "-f", pcm_format, "-acodec", pcm_codec, "-"],
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        try:
            assert process.stdout is not None
            while True:
                data = process.stdout.read(1024 * 1024)
                if not data:
                    break
                digest.update(data)
                total_bytes += len(data)
                if total_bytes >= next_progress:
                    if report_progress:
                        report_progress(
                            "verify-full-pcm",
                            f"已解码并核验 {total_bytes // (1024 * 1024)} MiB PCM",
                            min(total_bytes, expected_bytes),
                            expected_bytes,
                        )
                    next_progress += progress_step
            process.stdout.close()
            exit_code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if exit_code != 0:
            errors.seek(0)
            message = errors.read(4096).decode("utf-8", "replace").strip()
            raise RuntimeError(f"成品 FLAC/MKV 解码失败（{exit_code}）：{message}")

    if total_bytes % bytes_per_frame:
        raise RuntimeError("成品 PCM 字节数无法按声道帧宽整除")
    frames = total_bytes // bytes_per_frame
    decoded_pcm = digest.hexdigest()

    # A concurrent mutation or source replacement cannot retroactively
    # validate a stream that was only partially observed.
    verify_japanese_recovery_build_inputs(config)
    if _sha256(manifest) != manifest_sha_before:
        raise RuntimeError("解码过程中成品 Manifest 发生变化，不能宣称完整核验")
    media_stat_after = media.stat()
    if (
        media_stat_before.st_size != media_stat_after.st_size
        or media_stat_before.st_mtime_ns != media_stat_after.st_mtime_ns
    ):
        raise RuntimeError("解码过程中最终媒体文件发生变化，不能宣称完整核验")
    if total_bytes != expected_bytes or decoded_pcm != original_pcm:
        raise RuntimeError(
            "最终音轨完整 PCM 与编码前来源不一致："
            f" expected_frames={expected_frames}, actual_frames={frames}; "
            f"expected_sha256={original_pcm}, actual_sha256={decoded_pcm}"
        )
    if report_progress:
        report_progress("complete", "最终 FLAC/MKV 全音轨 PCM 验证通过", expected_bytes, expected_bytes)
    return {
        "schema_version": 1,
        "status": "final-pcm-lossless-verified",
        "coverage": "all-decoded-final-audio-frames",
        "media_file": media.name,
        "media_stream": stream_id,
        "pcm_format": pcm_format,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bits": bit_width,
        "verified_frames": frames,
        "verified_bytes": total_bytes,
        "expected_pcm_sha256": original_pcm,
        "decoded_final_pcm_sha256": decoded_pcm,
        "source_package_fingerprint": initial["source_fingerprint"],
        "manifest_sha256": manifest_sha_before,
        "original_members": verified["wav_members"],
        "spoken_japanese_verified": False,
        "translation_semantics_verified": False,
        "human_listening_required": True,
    }
