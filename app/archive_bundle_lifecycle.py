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
from .subtitle_export_status import read_subtitle_export_health, _inputs as _subtitle_inputs
from .archive_bundle_backgrounds import MAX_IMAGE_BYTES, MODES, normalize_bundle_background, load_default_red_cloud_background
from .chapter_audio_tracks import chapter_audio_tracks

RECEIPT = "archive_bundle_receipt.json"
PREFERENCES = "archive_bundle_preferences.json"
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


def _sources(output: Path, fonts: bool, chapters: bool = False) -> dict[str, Any]:
    sources = {
        "flac": _signature(output / "continuous.flac"),
        "ass": _signature(output / "HSR_Voice_Archive.ass"),
        "srt": _signature(output / "HSR_Voice_Archive.srt"),
        "subtitle_receipt": _signature(output / "subtitle_export_receipt.json"),
        "fonts": _font_signatures(output, fonts),
    }
    if chapters:
        paths = chapter_audio_tracks(output, verify_hashes=False)
        sources["chapter_audio"] = {
            "manifest": _signature(output / "chapter_flac" / "chapter_flac_manifest.json"),
            "tracks": [{"name": path.name, "group_id": group,
                        "file": _signature(path)} for path, group in paths],
        }
    return sources


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
    # Stream the content-addressed asset rather than allocating another
    # full copy for every project dashboard refresh (especially on Termux).
    hasher = hashlib.sha256()
    with image.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.hexdigest() != digest:
        raise ValueError("保存的 MKV 背景图片校验不通过，请重新选择图片")
    return image




def _background_preferences(output: Path) -> dict[str, Any] | None:
    # Preferences are not a publication certificate. They preserve a custom
    # backdrop when the user temporarily switches to FLAC-only and retires MKV.
    path = output / PREFERENCES
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("schema_version") != 1
                or not isinstance(data.get("background"), dict)
                or not isinstance(data.get("attach_project_fonts"), bool)):
            return None
        bg = data["background"]
        if bg.get("kind") not in MODES:
            return None
        normalize_bundle_background(bg.get("spec") or {"mode": bg["kind"]})
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None

def previous_bundle_settings(output: Path) -> dict[str, Any]:
    receipt = _read_receipt(output) or _background_preferences(output)
    if receipt is None:
        # The packaged asset is an actual available image, not a remembered
        # project upload. It is snapshotted into the first verified MKV receipt.
        return {"known": False, "background_color": "#000000",
                "background_kind": "image", "image_saved": True,
                "bundled_default": True, "attach_project_fonts": True,
                "background_spec": normalize_bundle_background({"mode": "image"})}
    bg = receipt["background"]
    image_saved = False
    if bg.get("kind") == "image":
        try:
            image_saved = _image_asset(output, receipt) is not None
        except (OSError, ValueError):
            image_saved = False
    return {
        "known": True,
        "background_color": str(bg.get("color") or "#000000"),
        "background_kind": bg.get("kind") or "color",
        "image_saved": image_saved,
        "attach_project_fonts": receipt["attach_project_fonts"],
        "background_spec": normalize_bundle_background(bg.get("spec") or {
            "mode": bg.get("kind") or "color",
        }),
    }


def load_previous_bundle_image(output: Path) -> bytes:
    receipt = _read_receipt(output) or _background_preferences(output)
    if receipt is None:
        return load_default_red_cloud_background()
    if receipt["background"].get("kind") != "image":
        raise ValueError("该项目没有可复用的 MKV 背景图，请上传图片或改用纯色")
    image = _image_asset(output, receipt)
    assert image is not None
    return image.read_bytes()


def bundle_audio_source_status(output: Path) -> dict[str, Any]:
    """Identify a reusable archive audio source without conflating MKV stale/missing.

    This is a cheap, provenance-based prerequisite check; the actual exporter
    additionally validates stream codecs with FFprobe before publication.
    Never certify a loose legacy MKV from its filename alone.
    """
    flac_sig = _signature(output / "continuous.flac")
    if flac_sig is not None and flac_sig["size"] > 0:
        return {"available": True, "source": "independent-flac", "reason": ""}
    bundle = output / OUTPUT
    if bundle.is_symlink():
        reason = ("旧 MKV 是符号链接，无法作为可信音频来源；"
                  "请恢复原始项目音频再完成档案构建")
    elif not bundle.is_file():
        reason = ("当前项目既没有可用的 continuous.flac，也没有完整 MKV 成品。"
                  "必须先完成至少一次档案音频构建，才能使用「生成 ASS（内）」")
    else:
        receipt = _read_receipt(output)
        if receipt is None:
            reason = ("发现已有 MKV 文件，但缺少有效的发布校验回执；"
                      "不能仅凭文件存在就提取音轨或覆盖它。请先核验或恢复原始档案")
        elif receipt.get("sealed") is not True:
            reason = ("发现已有 MKV 及发布回执，但尚未封存验证；"
                      "不能将它当作已校验的音频来源。请先恢复档案构建")
        elif _signature(bundle) != receipt.get("artifact"):
            reason = ("发现已有 MKV，但其文件身份与封存回执不一致；"
                      "为防止替换错误音轨，拒绝复用。请检查是否移动或修改了旧成品")
        else:
            return {"available": True, "source": "sealed-mkv", "reason": ""}
    return {"available": False, "source": "none", "reason": reason}


def bundle_export_status(output: Path) -> dict[str, Any]:
    """Validate the sealed MKV without requiring redundant standalone media files.

    Legacy (unsealed) bundles continue to require their original inputs. Sealed
    bundles instead bind their subtitle receipt's source hashes and the current
    font identities to the verified container identity.
    """
    settings = previous_bundle_settings(output)
    target = output / OUTPUT
    # An empty file exists, but cannot be a usable FLAC or subtitle product.
    # This is only a cheap dashboard preflight, not an FFprobe codec guarantee.
    receipt = _read_receipt(output)
    sealed = bool(receipt and receipt.get("sealed") is True)
    bundle_intact = bool(receipt and _signature(target) == receipt.get("artifact"))
    flac_sig = _signature(output / "continuous.flac")
    ass_sig = _signature(output / "HSR_Voice_Archive.ass")
    srt_sig = _signature(output / "HSR_Voice_Archive.srt")
    flac = bool(flac_sig and flac_sig["size"] > 0)
    ass = bool(ass_sig and ass_sig["size"] > 0)
    srt = bool(srt_sig and srt_sig["size"] > 0)
    subtitle = read_subtitle_export_health(output)
    # SRT has independent provenance even if someone edited/deleted ASS.
    # The full MKV still requires the joint subtitle receipt to be current.
    srt_current = bool(srt and subtitle.get("srt_current"))
    ass_current = bool(ass and subtitle.get("ass_current"))
    subtitles_current = bool(srt_current and ass_current
                             and subtitle.get("artifacts_current"))
    seal_inputs_current = False
    if sealed and bundle_intact:
        try:
            record = json.loads((output / "subtitle_export_receipt.json").read_text("utf-8"))
            seal_inputs_current = bool(
                record.get("state") == "current"
                and record.get("ass_required") is True
                and record.get("inputs") == _subtitle_inputs(output)
                and _signature(output / "subtitle_export_receipt.json")
                    == receipt["sources"].get("subtitle_receipt")
            )
        except (OSError, TypeError, ValueError, KeyError):
            seal_inputs_current = False
        if seal_inputs_current:
            # Missing standalone outputs are intentional in MKV-only mode.
            # An *existing* edited output is checked against its publication
            # identity and is not silently accepted as a new verified source.
            srt_current = (srt_current if srt else True)
            ass_current = (ass_current if ass else True)
            subtitles_current = bool(srt_current and ass_current)
    audio_source = bundle_audio_source_status(output)
    audio_available = bool(audio_source["available"])
    build_busy = (output / ".build_in_progress").exists()
    prerequisites = {
        "continuous_flac": audio_available,
        "independent_flac": flac,
        "embedded_flac": bool(sealed and bundle_intact),
        "audio_source": audio_source["source"],
        "audio_source_error": audio_source["reason"],
        "ass": ass,
        "srt": srt,
        "srt_current": srt_current,
        "ass_current": ass_current,
        "subtitles_current": subtitles_current,
        "build_idle": not build_busy,
    }
    blockers = []
    if not audio_available:
        blockers.append(audio_source["reason"])
    if not srt_current:
        blockers.append("独立 SRT 缺失、为空或已过期，请先更新字幕")
    if not ass_current:
        blockers.append("ASS 未导出或未通过最新字幕回执校验，请先生成 ASS")
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
    if receipt is None:
        result.update(state="unverified", message="存在旧 MKV，但缺少本次来源校验记录；建议重新生成。")
        return result
    try:
        if blockers:
            raise ValueError("MKV 生成前提不满足：" + "；".join(blockers))
        if (output / ".build_in_progress").exists():
            raise ValueError("项目构建进行中，旧 MKV 不代表最新结果")
        if _signature(target) != receipt["artifact"]:
            raise ValueError("MKV 文件已被修改或替换")
        if sealed:
            current = _sources(output, receipt["attach_project_fonts"],
                               bool(receipt.get("include_chapter_flac")))
            original = receipt["sources"]
            if current["fonts"] != original["fonts"]:
                raise ValueError("字体发生变化")
            if current.get("chapter_audio") != original.get("chapter_audio"):
                raise ValueError("已封装章节音轨来源发生变化")
            if not seal_inputs_current:
                raise ValueError("字幕来源或导出回执发生变化")
            for name in ("flac", "srt", "ass"):
                if current[name] is not None and current[name] != original[name]:
                    # A materialized FLAC extracted from the MKV can have a
                    # different file header yet the same compressed stream;
                    # it becomes an input only after a fresh export receipt.
                    raise ValueError("额外独立来源与已封装版本不一致：" + name)
        else:
            if _sources(output, receipt["attach_project_fonts"],
                        bool(receipt.get("include_chapter_flac"))) != receipt["sources"]:
                raise ValueError("连续 FLAC、ASS、SRT 或字体已变化")
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
                  message=("MKV 已验证；独立 FLAC/SRT/ASS 按需导出。"
                           if sealed else "完整 MKV 已通过来源一致性检查。"))
    return result


def record_bundle_publication(
    output: Path, *, background_color: str, background_image: bytes | None,
    image_extension: str | None, include_fonts: bool,
    artifact: Path | None = None,
    background_spec: dict[str, str] | None = None,
    include_chapter_flac: bool = False,
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
        "sources": _sources(output, include_fonts, include_chapter_flac),
        "include_chapter_flac": bool(include_chapter_flac),
        "background": bg,
        "attach_project_fonts": bool(include_fonts),
    }
    if (not receipt["artifact"] or not receipt["sources"]["flac"]
            or not receipt["sources"]["ass"] or not receipt["sources"]["srt"]):
        raise ValueError("Cannot certify a missing MKV or source file")
    atomic_write_text(output / RECEIPT, json.dumps(receipt, ensure_ascii=False, indent=2))
