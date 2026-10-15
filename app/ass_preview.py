from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from xml.sax.saxutils import escape as escape_xml

from subtitle_layout.ass_writer import write_ass
from subtitle_layout.config import SubtitleRenderConfig
from subtitle_layout.fonts import validate_ass_font_name
from .archive_bundle_exporter import image_extension, MAX_BACKGROUND_BYTES
from .project_fonts import project_font_for_family, project_font_face_names


@dataclass
class PreviewEntry:
    english: str
    chinese: str
    start_seconds: float = 0.0
    display_end_seconds: float = 3.0
    word_alignments: list[object] | None = None


def _escape_subtitles_filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    return value.replace(":", "\\:").replace("'", "\\'")


def preview_config_from_payload(data: dict[str, object]) -> SubtitleRenderConfig:
    def as_bool(name: str, default: bool) -> bool:
        value = data.get(name, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    return SubtitleRenderConfig(
        enable_karaoke=as_bool("enable_karaoke", False),
        karaoke_mode=(str(data.get("karaoke_mode", "k") or "k").lower() if str(data.get("karaoke_mode", "k") or "k").lower() in {"k", "kf", "clip"} else "k"),
        enable_frosted_glass=as_bool("enable_translucent_card", False),
        enable_multi_layer_outline=as_bool("enable_multi_layer_outline", False),
        enable_kinetic=as_bool("enable_kinetic", True),
        fade_in_ms=max(0, min(2000, int(data.get("fade_in_ms", 200)))),
        fade_out_ms=max(0, min(2000, int(data.get("fade_out_ms", 200)))),
        use_audio_aware_fade=as_bool("use_audio_aware_fade", True),
        outline_width=max(0.0, min(12.0, float(data.get("outline_width", 3.0)))),
        shadow_depth=max(0.0, min(12.0, float(data.get("shadow_depth", 2.0)))),
        blur_radius=max(0.0, min(5.0, float(data.get("blur_radius", 0.0)))),
        card_opacity=max(0.0, min(1.0, float(data.get("card_opacity", 0.62)))),
        enable_soft_entry=as_bool("enable_soft_entry", False),
        soft_entry_scale_percent=max(
            90.0, min(100.0, float(data.get("soft_entry_scale_percent", 98.0)))
        ),
        soft_entry_blur=max(0.0, min(5.0, float(data.get("soft_entry_blur", 1.5)))),
        soft_entry_ms=max(0, min(1000, int(data.get("soft_entry_ms", 160)))),
        enable_archive_hud=as_bool("enable_archive_hud", False),
        archive_character=str(data.get("archive_character", "") or "").strip(),
        archive_hud_font_size=max(12, min(48, int(data.get("archive_hud_font_size", 22)))),
        archive_hud_opacity=max(0.1, min(1.0, float(data.get("archive_hud_opacity", 0.72)))),
        chs_font=validate_ass_font_name(data.get("chs_font", "汉仪旗黑")),
        primary_font=validate_ass_font_name(data.get("primary_font", "Noto Sans")),
        base_chs_size=max(12, min(120, int(data.get("base_chs_size", 52)))),
        base_primary_size=max(12, min(120, int(data.get("base_primary_size", 42)))),
        margin_horizontal_percent=max(
            0.0, min(0.40, float(data.get("margin_horizontal_percent", 0.03)))
        ),
        margin_vertical_percent=max(
            0.0, min(0.40, float(data.get("margin_vertical_percent", 0.05)))
        ),
        min_central_gap=max(0.0, min(200.0, float(data.get("min_central_gap", 20.0)))),
        primary_region_share=max(0.20, min(0.80, float(data.get("primary_region_share", 0.60)))),
    )


STUDIO_REFERENCE_BACKGROUND = Path(__file__).resolve().parent / "static" / "assets" / "studio-red-clouds.webp"


def load_studio_reference_background() -> bytes:
    """Bundled diagnostic image, never an input to project export."""
    image = STUDIO_REFERENCE_BACKGROUND
    if not image.is_file() or image.is_symlink():
        raise RuntimeError("工作台默认参考背景文件缺失")
    data = image.read_bytes()
    if not data or len(data) > 1_048_576 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise RuntimeError("工作台默认参考背景损坏")
    return data


def _project_font_config(config: SubtitleRenderConfig, font_dir: Path | None) -> SubtitleRenderConfig:
    """Resolve uploaded font files BEFORE measuring subtitle layout geometry."""
    if font_dir is None or not font_dir.is_dir():
        return config
    chs = project_font_for_family(font_dir.parent, config.chs_font)
    primary = project_font_for_family(font_dir.parent, config.primary_font)
    return replace(
        config,
        chs_font_path=str(chs) if chs else config.chs_font_path,
        primary_font_path=str(primary) if primary else config.primary_font_path,
    )


def _project_font_environment(config: SubtitleRenderConfig, font_dir: Path | None,
                              temporary_dir: Path, *, english: str, chinese: str) -> dict[str, str] | None:
    """Prefer project fonts in an isolated Fontconfig configuration."""
    if font_dir is None or not font_dir.is_dir():
        return None
    imported = [
        project_font_for_family(font_dir.parent, family)
        for text, family in ((chinese, config.chs_font), (english, config.primary_font))
        if str(text or "").strip()
    ]
    if not any(path is not None for path in imported):
        return None
    candidates = [
        os.environ.get("FONTCONFIG_FILE", ""),
        str(Path(os.environ.get("PREFIX", "/nonexistent")) / "etc/fonts/fonts.conf"),
        "/etc/fonts/fonts.conf", "/usr/local/etc/fonts/fonts.conf",
    ]
    original = next((Path(name) for name in candidates if name and Path(name).is_file()), None)
    includes = (f'<include ignore_missing="yes">{escape_xml(str(original))}</include>'
                if original else "")
    config_path = temporary_dir / "project-fontconfig.xml"
    config_path.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        '<fontconfig><dir>' + escape_xml(str(font_dir.resolve()))
        + '</dir>' + includes + '</fontconfig>', encoding="utf-8",
    )
    env = os.environ.copy()
    env["FONTCONFIG_FILE"] = str(config_path)
    return env


def _assert_project_font_selected(
    log: str, config: SubtitleRenderConfig, font_dir: Path | None,
    *, english: str, chinese: str, env: dict[str, str] | None = None,
) -> None:
    """Verify Fontconfig resolves the uploaded file, not a system fallback."""
    if font_dir is None or not font_dir.is_dir():
        return
    selections = [line.casefold() for line in log.splitlines()
                  if "fontselect:" in line.casefold()]
    for used, family in ((chinese, config.chs_font), (english, config.primary_font)):
        if not str(used or "").strip():
            continue
        expected = project_font_for_family(font_dir.parent, family)
        if expected is None:
            continue
        matcher = shutil.which("fc-match")
        if not matcher:
            raise RuntimeError("无法确认项目字体：缺少 fontconfig 的 fc-match。Termux 请运行 pkg install fontconfig 后重试。")
        completed = subprocess.run(
            [matcher, "-f", "%{file}\\n", "--", family],
            env=env, capture_output=True, text=True, timeout=8, check=False,
        )
        actual = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
        if (completed.returncode != 0 or not actual
                or Path(actual).resolve() != expected.resolve()):
            raise RuntimeError(
                f"导入字体 {family} 被回退为 {actual or '未知字体'}，"
                f"而非导入文件 {expected.name}；请检查字体族名、字体文件和字形。"
            )
        # Fontconfig may resolve the requested face correctly and libass may
        # still fetch a *different* face for a missing glyph. That is not a
        # successful uploaded-font render. Detect it using the libass log,
        # rather than reporting success based only on fc-match's family.
        missing_glyphs = []
        for match in re.finditer(r"Glyph\s+0x([0-9a-fA-F]+)\s+not\s+found", log, re.IGNORECASE):
            try:
                character = chr(int(match.group(1), 16))
            except (ValueError, OverflowError):
                continue
            if character in str(used) and character.isprintable() and not character.isspace():
                missing_glyphs.append(f"U+{ord(character):04X}")
        if missing_glyphs:
            raise RuntimeError(
                f"已导入字体 {family} 缺少当前字幕中的字形 "
                + ", ".join(sorted(set(missing_glyphs))[:12])
                + "，libass 可能自动回退其他字体；请换用具有这些字形的字体。"
            )
        # libass has two legitimate fontselect log formats:
        #   (Family, 400, 0) -> /resolved/font-file.ttf, 0, ...
        #   (Family, 400, 0) -> PostScriptFaceName, 0, ...
        # A SHA-named project asset cannot appear in the second format; never
        # require its path there. fc-match above is the file-identity proof.
        matched = [line for line in selections
                   if f"fontselect: ({family.casefold()}," in line]
        expected_path = str(expected.resolve()).casefold()
        normalized_family = "".join(ch for ch in family.casefold() if ch.isalnum())
        sfnt_faces = {
            "".join(ch for ch in name.casefold() if ch.isalnum())
            for name in project_font_face_names(expected)
        }
        def libass_face_ok(line: str) -> bool:
            if "->" not in line:
                return False
            selected = line.split("->", 1)[1].split(",", 1)[0].strip().strip('"')
            if "/" in selected or "\\" in selected:
                return expected_path == str(Path(selected).resolve()).casefold()
            normalized_face = "".join(ch for ch in selected.casefold() if ch.isalnum())
            # Accept the actual face/full/PostScript names from the imported
            # font, including non-standard weight suffixes such as "-55W".
            # Keep conventional suffixes for hosts exposing synthetic faces.
            return bool(normalized_face and (
                normalized_face in sfnt_faces or
                normalized_face == normalized_family or
                any(normalized_face == normalized_family + suffix
                    for suffix in ("regular", "bold", "italic", "medium",
                                   "semibold", "light", "black"))
            ))
        if not matched or not all(libass_face_ok(line) for line in matched):
            selections_info = " / ".join(line[:260] for line in matched[:2]) or "无对应字体日志"
            raise RuntimeError(
                f"libass 对 {family} 使用了不匹配的字体面，而非已导入的 {expected.name}："
                f"{selections_info}。请检查字体族名、字形覆盖和 Fontconfig 配置。"
            )


def render_ass_preview_png(
    *,
    english_text: str,
    chinese_text: str,
    source_language: str,
    target_language: str,
    config: SubtitleRenderConfig,
    word_alignments: list[object] | None = None,
    duration_seconds: float = 3.0,
    timestamp: float = 1.0,
    font_dir: Path | None = None,
    use_studio_reference_background: bool = False,
) -> bytes:
    """Render one representative subtitle frame through FFmpeg/libass."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("实际 ASS 预览需要 FFmpeg")

    duration = max(0.25, min(120.0, float(duration_seconds)))
    render_timestamp = max(0.0, min(duration - 0.01, float(timestamp)))
    entry = PreviewEntry(
        english=str(english_text or ""),
        chinese=str(chinese_text or ""),
        display_end_seconds=duration,
        word_alignments=word_alignments,
    )
    config = _project_font_config(config, font_dir)
    with tempfile.TemporaryDirectory(prefix="hsr-ass-preview-") as td:
        root = Path(td)
        ass_path = root / "preview.ass"
        png_path = root / "preview.png"
        write_ass(
            [entry],
            ass_path,
            source_language=source_language or "en",
            target_language=target_language or "zh-CN",
            config=config,
            overflow_report_path=root / "overflow.json",
        )
        escaped_ass = _escape_subtitles_filter_path(ass_path)
        font_env = _project_font_environment(
            config, font_dir, root, english=english_text, chinese=chinese_text,
        )
        if use_studio_reference_background:
            image_path = root / "reference.webp"
            image_path.write_bytes(load_studio_reference_background())
            source_args = ["-loop", "1", "-framerate", "1", "-i", str(image_path)]
            picture_filter = ("scale=1920:1080:force_original_aspect_ratio=decrease,"
                              "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,")
        else:
            source_args = ["-f", "lavfi", "-i",
                           f"color=c=black:s=1920x1080:r=1:d={max(3.0, duration):.3f}"]
            picture_filter = ""
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "info",
            *source_args,
            "-ss",
            f"{render_timestamp:.3f}",
            "-vf",
            picture_filter + f"subtitles='{escaped_ass}'" + (f":fontsdir='{_escape_subtitles_filter_path(font_dir)}'" if font_dir and font_dir.is_dir() else ""),
            "-frames:v",
            "1",
            str(png_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=font_env,
        )
        if completed.returncode != 0 or not png_path.is_file() or png_path.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "unknown FFmpeg error").strip()
            raise RuntimeError(f"FFmpeg/libass 预览失败: {detail[:1200]}")
        _assert_project_font_selected(completed.stderr, config, font_dir,
                                      english=english_text, chinese=chinese_text,
                                      env=font_env)
        return png_path.read_bytes()


MAX_PREVIEW_SOURCE_BYTES = 24 * 1024 * 1024


def decode_preview_source_video(value: object) -> bytes | None:
    """Decode a bounded 24 MiB opt-in local clip, never a path or URL."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Source video must be a base64 string")
    if len(value) > 4 * ((MAX_PREVIEW_SOURCE_BYTES + 2) // 3):
        raise ValueError("Source video exceeds 24 MiB")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid source video encoding") from exc
    if not decoded or len(decoded) > MAX_PREVIEW_SOURCE_BYTES:
        raise ValueError("Source video must contain 1–24 MiB of bytes")
    return decoded


def _validate_preview_source(path: Path, start: float, duration: float) -> None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("Local footage preview requires FFprobe")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Source video probe timed out") from exc
    if result.returncode != 0:
        raise ValueError("Source video could not be probed")
    try:
        obj = json.loads(result.stdout)
        streams = [s for s in obj.get("streams", []) if s.get("codec_type") == "video"]
        total = float(obj.get("format", {}).get("duration"))
        width = int(streams[0]["width"])
        height = int(streams[0]["height"])
    except (IndexError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Source lacks verified video duration/dimensions") from exc
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Source duration is invalid")
    if width < 16 or height < 16 or width > 3840 or height > 2160:
        raise ValueError("Source exceeds supported video dimensions (3840×2160)")
    if start + duration > total + 0.04:
        raise ValueError("Selected preview window extends beyond source")


def normalize_preview_background_color(value: object) -> str:
    """Return a safe #RRGGBB background; reject arbitrary FFmpeg expressions."""
    if value is None or value == "":
        return "#000000"
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Background color must be #RRGGBB")
    return value.lower()


def _validate_preview_background_image(path: Path) -> None:
    """Validate actual decoded picture dimensions before passing to FFmpeg."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("Background image preview requires FFprobe")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type,width,height",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        info = json.loads(result.stdout) if result.returncode == 0 else {}
        video = (info.get("streams") or [])[0]
        width, height = int(video["width"]), int(video["height"])
    except (IndexError, KeyError, TypeError, ValueError, OverflowError,
            subprocess.TimeoutExpired) as exc:
        raise ValueError("Background image cannot be decoded") from exc
    # Still images are downscaled to a 960×540 preview. The former
    # 3840×2160 source cap rejected ordinary 4032×3024 phone photos even
    # when the encoded file was well below the advertised 16 MiB limit.
    # Match the existing complete-MKV image preflight's bounded pixel budget.
    if not (16 <= width <= 8192 and 16 <= height <= 8192 and width * height <= 24_000_000):
        raise ValueError(
            f"Background image {width}×{height} exceeds the safe limit: "
            "16–8192 pixels per side and at most 24 million pixels"
        )


def render_ass_preview_video(
    *,
    english_text: str,
    chinese_text: str,
    source_language: str,
    target_language: str,
    config: SubtitleRenderConfig,
    word_alignments: list[object] | None = None,
    duration_seconds: float = 3.0,
    container_format: str = "mp4",
    source_video_bytes: bytes | None = None,
    source_start_seconds: float = 0.0,
    include_source_audio: bool = True,
    background_color: str = "#000000",
    background_image_bytes: bytes | None = None,
    font_dir: Path | None = None,
) -> bytes:
    """Render genuine ASS motion over color, validated image, or local video.

    Source bytes are ephemeral and never written to project storage. When
    requested, the source audio track is trimmed together with source picture.
    The result is a preview clip, not a production media export.
    """
    background_color = normalize_preview_background_color(background_color)
    ffmpeg_color = "0x" + background_color[1:]
    duration = float(duration_seconds)
    if not math.isfinite(duration) or not 0.5 <= duration <= 8.0:
        raise ValueError("Dynamic preview duration must be between 0.5 and 8 seconds")
    start = float(source_start_seconds)
    if not math.isfinite(start) or not 0 <= start <= 600:
        raise ValueError("Preview source offset must be between 0 and 600 seconds")
    if background_image_bytes is not None:
        if source_video_bytes is not None:
            raise ValueError("Choose one video or one background image, not both")
        if (not isinstance(background_image_bytes, bytes)
                or not 0 < len(background_image_bytes) <= MAX_BACKGROUND_BYTES):
            raise ValueError("Background image exceeds the 64 MiB maximum")
        image_extension(background_image_bytes)
    if source_video_bytes is not None and (
        not isinstance(source_video_bytes, bytes)
        or not 0 < len(source_video_bytes) <= MAX_PREVIEW_SOURCE_BYTES
    ):
        raise ValueError("Source video must be 1–24 MiB of local bytes")
    fmt = str(container_format).lower()
    if fmt not in {"mp4", "webm"}:
        raise ValueError("Unsupported preview container: use mp4 or webm")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Dynamic ASS preview requires FFmpeg")
    entry = PreviewEntry(
        english=str(english_text or ""),
        chinese=str(chinese_text or ""),
        display_end_seconds=duration,
        word_alignments=word_alignments,
    )
    config = _project_font_config(config, font_dir)
    with tempfile.TemporaryDirectory(prefix="hsr-effect-video-") as td:
        root = Path(td)
        ass_path = root / "preview.ass"
        result_path = root / ("effect-preview." + fmt)
        write_ass(
            [entry], ass_path,
            source_language=source_language or "en",
            target_language=target_language or "zh-CN",
            config=config,
            overflow_report_path=root / "overflow.json",
        )
        escaped_ass = _escape_subtitles_filter_path(ass_path)
        font_env = _project_font_environment(
            config, font_dir, root, english=english_text, chinese=chinese_text,
        )
        if source_video_bytes is not None:
            source_path = root / "selected-local-footage"
            source_path.write_bytes(source_video_bytes)
            _validate_preview_source(source_path, start, duration)
            source_args = ["-ss", f"{start:.3f}", "-i", str(source_path)]
            # Normalize the user image without stretching its aspect ratio.
            video_filters = ("scale=960:540:force_original_aspect_ratio=decrease,"
                             f"pad=960:540:(ow-iw)/2:(oh-ih)/2:color={ffmpeg_color},setsar=1,fps=24,")
        elif background_image_bytes is not None:
            image_path = root / ("selected-background" + image_extension(background_image_bytes))
            image_path.write_bytes(background_image_bytes)
            _validate_preview_background_image(image_path)
            source_args = ["-loop", "1", "-framerate", "24", "-i", str(image_path)]
            video_filters = ("scale=960:540:force_original_aspect_ratio=decrease,"
                             f"pad=960:540:(ow-iw)/2:(oh-ih)/2:color={ffmpeg_color},"
                             "setsar=1,fps=24,")
        else:
            source_args = [
                "-f", "lavfi", "-i",
                f"color=c={ffmpeg_color}:s=960x540:r=24:d={duration:.3f}",
            ]
            video_filters = ""
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "info",
            *source_args, "-vf",
            video_filters + f"subtitles='{escaped_ass}':original_size=1920x1080" +
            (f":fontsdir='{_escape_subtitles_filter_path(font_dir)}'" if font_dir and font_dir.is_dir() else "") +
            ",format=yuv420p",
        ]
        if source_video_bytes is not None:
            # Explicit stream map prevents attached pictures/subtitles or a
            # second source audio from leaking into the preview.
            command += ["-map", "0:v:0"]
            if include_source_audio:
                command += ["-map", "0:a:0?"]
            else:
                command += ["-an"]
        else:
            command += ["-an"]
        if fmt == "webm":
            command += ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
                        "-b:v", "1200k"]
            if source_video_bytes is not None and include_source_audio:
                command += ["-c:a", "libopus", "-b:a", "96k"]
        else:
            command += ["-c:v", "libx264", "-preset", "ultrafast",
                        "-crf", "22", "-movflags", "+faststart"]
            if source_video_bytes is not None and include_source_audio:
                command += ["-c:a", "aac", "-b:a", "128k"]
        command += ["-t", f"{duration:.3f}", str(result_path)]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=50, check=False,
                env=font_env
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("FFmpeg/libass preview timed out") from exc
        if (completed.returncode != 0 or not result_path.is_file()
                or result_path.stat().st_size == 0):
            detail = (completed.stderr or completed.stdout or "unknown FFmpeg error").strip()
            raise RuntimeError(f"FFmpeg/libass preview failed: {detail[:1200]}")
        _assert_project_font_selected(completed.stderr, config, font_dir,
                                      english=english_text, chinese=chinese_text,
                                      env=font_env)
        if result_path.stat().st_size > 16 * 1024 * 1024:
            raise RuntimeError("Dynamic preview exceeds 16 MiB output limit")
        return result_path.read_bytes()
