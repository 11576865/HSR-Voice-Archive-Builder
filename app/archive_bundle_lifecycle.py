"""Source-aware status and reusable project-owned background for complete MKV rebuilds.

The sealed MKV is a derived artifact. Rebuilding FLAC or ASS never mutates it;
an old file is retained but must not be labelled current once its inputs change.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .builder import atomic_write_text
from .project_fonts import font_asset_directory
from .subtitle_export_status import read_subtitle_export_health
from .archive_bundle_backgrounds import MAX_IMAGE_BYTES, MODES, normalize_bundle_background

RECEIPT = "archive_bundle_receipt.json"
OUTPUT = "HSR_Voice_Archive_Bundle.mkv"
IMAGE_PREFIX = ".hsr-bundle-bg-"
IMAGE_EXTENSIONS = {".png", ".jpg", ".webp"}


def _signature(path: Path) -> dict[str, int] | None:
    if path.is_symlink() or not path.is_file():
        return None
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _font_signatures(output: Path, enabled: bool) -> list[dict[str, Any]]:
    if not enabled:
        return []
    directory = font_asset_directory(output)
    if directory.is_symlink():
        raise ValueError("Project font directory must not be a symlink")
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
            continue
        signature = _signature(path)
        if signature is not None:
            records.append({"name": path.name, **signature})
    return records


def _sources(output: Path, fonts: bool) -> dict[str, Any]:
    return {
        "flac": _signature(output / "continuous.flac"),
        "ass": _signature(output / "HSR_Voice_Archive.ass"),
        "subtitle_receipt": _signature(output / "subtitle_export_receipt.json"),
        "fonts": _font_signatures(output, fonts),
    }


def _read_receipt(output: Path) -> dict[str, Any] | None:
    path = output / RECEIPT
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or not isinstance(data.get("background"), dict)
                or not isinstance(data.get("sources"), dict)
                or not isinstance(data.get("artifact"), dict)
                or not isinstance(data.get("attach_project_fonts"), bool)):
            return None
        bg = data["background"]
        if bg.get("kind") not in MODES:
            return None
        if bg.get("spec") is not None:
            if normalize_bundle_background(bg["spec"])["mode"] != bg["kind"]:
                return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _image_asset(output: Path, receipt: dict[str, Any]) -> Path | None:
    bg = receipt.get("background") or {}
    if bg.get("kind") != "image":
        return None
    name = bg.get("image_name")
    digest = bg.get("sha256")
    if (not isinstance(name, str) or not isinstance(digest, str)
            or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest)
            or name != IMAGE_PREFIX + digest + str(bg.get("extension") or "")
            or Path(name).suffix not in IMAGE_EXTENSIONS):
        raise ValueError("保存的 MKV 背景图片身份无效")
    image = output / name
    if image.is_symlink() or not image.is_file() or not 0 < image.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError("保存的 MKV 背景图片缺失或无效，请重新选择图片")
    if hashlib.sha256(image.read_bytes()).hexdigest() != digest:
        raise ValueError("保存的 MKV 背景图片校验不通过，请重新选择图片")
    return image


def previous_bundle_settings(output: Path) -> dict[str, Any]:
    receipt = _read_receipt(output)
    if receipt is None:
        return {"known": False, "background_color": "#000000",
                "background_kind": "color", "image_saved": False,
                "attach_project_fonts": True,
                "background_spec": normalize_bundle_background(None)}
    bg = receipt["background"]
    return {
        "known": True,
        "background_color": str(bg.get("color") or "#000000"),
        "background_kind": bg.get("kind") or "color",
        "image_saved": bg.get("kind") == "image",
        "attach_project_fonts": receipt["attach_project_fonts"],
        "background_spec": normalize_bundle_background(bg.get("spec") or {
            "mode": bg.get("kind") or "color",
        }),
    }


def load_previous_bundle_image(output: Path) -> bytes:
    receipt = _read_receipt(output)
    if receipt is None or receipt["background"].get("kind") != "image":
        raise ValueError("该项目没有可复用的 MKV 背景图，请上传图片或改用纯色")
    image = _image_asset(output, receipt)
    assert image is not None
    return image.read_bytes()


def bundle_export_status(output: Path) -> dict[str, Any]:
    """Only the combined MKV is derived; independent FLAC and subtitle deliverables remain.

    A file existing on disk does not imply readiness for a *new* bundle build.
    Surface source prerequisites separately from last-published MKV freshness.
    """
    settings = previous_bundle_settings(output)
    target = output / OUTPUT
    flac = _signature(output / "continuous.flac") is not None
    ass = _signature(output / "HSR_Voice_Archive.ass") is not None
    srt = _signature(output / "HSR_Voice_Archive.srt") is not None
    subtitle = read_subtitle_export_health(output)
    subtitles_current = bool(subtitle.get("state") == "current"
                             and subtitle.get("artifacts_current") and subtitle.get("ass_required"))
    build_busy = (output / ".build_in_progress").exists()
    prerequisites = {
        "continuous_flac": flac,
        "ass": ass,
        "srt": srt,
        "subtitles_current": subtitles_current,
        "build_idle": not build_busy,
    }
    blockers = []
    if not flac:
        blockers.append("缺少连续 FLAC，请先完成档案构建")
    if not ass or not srt or not subtitles_current:
        blockers.append("ASS/SRT 未完成或已过期，请先生成最新字幕")
    if build_busy:
        blockers.append("项目正在构建，完成后再封装")
    result: dict[str, Any] = {
        **settings, "state": "missing", "needs_rebuild": True,
        "can_generate": not blockers, "blockers": blockers,
        "prerequisites": prerequisites, "depends_on": ["continuous.flac", "HSR_Voice_Archive.ass",
                                                           "HSR_Voice_Archive.srt", "subtitle_export_receipt.json"],
        "message": "尚未生成完整 MKV。",
    }
    if not target.is_file() or target.is_symlink():
        return result
    receipt = _read_receipt(output)
    if receipt is None:
        result.update(state="unverified", message="存在旧 MKV，但缺少本次来源校验记录；建议重新生成。")
        return result
    try:
        if (output / ".build_in_progress").exists():
            raise ValueError("项目构建进行中，旧 MKV 不代表最新结果")
        if _signature(target) != receipt["artifact"]:
            raise ValueError("MKV 文件已被修改或替换")
        if _sources(output, receipt["attach_project_fonts"]) != receipt["sources"]:
            raise ValueError("连续 FLAC、ASS 或字体已变化")
        health = read_subtitle_export_health(output)
        if not (health.get("state") == "current"
                and health.get("artifacts_current") and health.get("ass_required")):
            raise ValueError("ASS/SRT 的项目导出状态不是最新")
        if receipt["background"].get("kind") == "image":
            _image_asset(output, receipt)
    except (OSError, ValueError, TypeError) as exc:
        result.update(state="stale", message="旧 MKV 已过期：" + str(exc) + "；请重新生成。")
        return result
    result.update(state="current", needs_rebuild=False,
                  message="完整 MKV 已通过来源一致性检查。")
    return result


def record_bundle_publication(
    output: Path, *, background_color: str, background_image: bytes | None,
    image_extension: str | None, include_fonts: bool,
    artifact: Path | None = None,
    background_spec: dict[str, str] | None = None,
) -> None:
    """Persist replayable inputs and provenance after verified atomic MKV publish.

    Saved images use content-addressed names; an earlier referenced image is not
    removed until after any future successful publication (and is safe to retain).
    """
    spec = normalize_bundle_background(background_spec or {
        "mode": "image" if background_image is not None else "color",
    })
    bg: dict[str, Any] = {
        "kind": spec["mode"], "color": background_color, "spec": spec,
    }
    if background_image is not None:
        if image_extension not in IMAGE_EXTENSIONS or not 0 < len(background_image) <= MAX_IMAGE_BYTES:
            raise ValueError("Unsupported image snapshot")
        digest = hashlib.sha256(background_image).hexdigest()
        name = IMAGE_PREFIX + digest + image_extension
        target = output / name
        if target.is_symlink():
            raise ValueError("Unsafe bundle background asset")
        if target.is_file():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("Existing bundle background asset conflicts")
        else:
            temp = output / (name + ".part")
            try:
                with temp.open("xb") as stream:
                    stream.write(background_image)
                    stream.flush()
                    os.fsync(stream.fileno())
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
        bg = {"kind": "image", "color": background_color, "spec": spec,
              "image_name": name, "sha256": digest, "extension": image_extension}
    receipt = {
        "schema_version": 1,
        "artifact": _signature(artifact if artifact is not None else output / OUTPUT),
        "sources": _sources(output, include_fonts),
        "background": bg,
        "attach_project_fonts": bool(include_fonts),
    }
    if not receipt["artifact"] or not receipt["sources"]["flac"] or not receipt["sources"]["ass"]:
        raise ValueError("Cannot certify a missing MKV or source file")
    atomic_write_text(output / RECEIPT, json.dumps(receipt, ensure_ascii=False, indent=2))
