from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from subtitle_layout.ass_writer import write_ass
from subtitle_layout.config import SubtitleRenderConfig
from subtitle_layout.fonts import validate_ass_font_name
from subtitle_layout.safe_area import SafeArea
from subtitle_layout.rendered_ink import visible_ink_bbox


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
        primary_region_ratio=max(0.20, min(0.80, float(data.get("primary_region_ratio", 0.60)))),
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
) -> bytes:
    """Render one representative subtitle frame through FFmpeg/libass."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("实际 ASS 预览需要 FFmpeg")

    duration, render_timestamp = _preview_times(duration_seconds, timestamp)
    entry = PreviewEntry(
        english=str(english_text or ""),
        chinese=str(chinese_text or ""),
        display_end_seconds=duration,
        word_alignments=word_alignments,
    )
    with tempfile.TemporaryDirectory(prefix="hsr-ass-preview-") as td:
        root = Path(td)
        ass_path = root / "preview.ass"
        png_path = root / "preview.png"
        _write_preview_ass(ass_path, entry, config, source_language, target_language)
        escaped_ass = _escape_subtitles_filter_path(ass_path)
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1920x1080:r=1:d=1",
            "-vf",
            f"settb=AVTB,setpts=PTS+{render_timestamp:.3f}/TB,subtitles='{escaped_ass}'",
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
        )
        if completed.returncode != 0 or not png_path.is_file() or png_path.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "unknown FFmpeg error").strip()
            raise RuntimeError(f"FFmpeg/libass 预览失败: {detail[:1200]}")
        return png_path.read_bytes()


def inspect_rendered_ink_interval(
    *, english_text: str, chinese_text: str,
    source_language: str, target_language: str,
    config: SubtitleRenderConfig,
    word_alignments: list[object] | None = None,
    duration_seconds: float = 3.0,
    sample_limit: int = 8,
) -> dict[str, object]:
    """Inspect a bounded selection of actual libass frames.

    Successful sampled frames NEVER certify every timestamp or the published ASS.
    FFmpeg errors fail the whole action: partial renders are not labeled success.
    """
    from subtitle_layout.temporal_samples import plan_temporal_samples

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Temporal pixel sampling requires FFmpeg/libass")
    duration, _ = _preview_times(duration_seconds, 0)
    # Reject abusive/unbounded or silently coerced sample counts.
    plan = plan_temporal_samples(
        duration,
        word_alignments=word_alignments,
        enable_karaoke=bool(config.enable_karaoke),
        enable_kinetic=bool(config.enable_kinetic),
        fade_in_ms=config.fade_in_ms, fade_out_ms=config.fade_out_ms,
        enable_soft_entry=bool(config.enable_soft_entry),
        soft_entry_ms=config.soft_entry_ms,
        sample_limit=sample_limit,
    )
    entry = PreviewEntry(
        english=str(english_text or ""), chinese=str(chinese_text or ""),
        display_end_seconds=duration, word_alignments=word_alignments,
    )
    h = max(0.0, min(0.40, float(config.margin_horizontal_percent)))
    v = max(0.0, min(0.40, float(config.margin_vertical_percent)))
    area = SafeArea(
        margin_left_percent=h, margin_right_percent=h,
        margin_top_percent=v, margin_bottom_percent=v,
    )
    safe = {"x_min": area.x_min, "x_max": area.x_max,
            "y_min": area.y_min, "y_max": area.y_max}
    samples: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="hsr-ass-time-ink-") as td:
        ass_path = Path(td) / "span.ass"
        _write_preview_ass(ass_path, entry, config, source_language, target_language)
        for item in plan["samples"]:
            stamp = float(item["timestamp_seconds"])
            black = _render_raw_rgb(ffmpeg, ass_path, timestamp=stamp, background="black")
            white = _render_raw_rgb(ffmpeg, ass_path, timestamp=stamp, background="white")
            box = visible_ink_bbox(black, white, 1920, 1080, threshold=16)
            if box is None:
                outcome = "no_visible_ink"
            elif safe["x_min"] <= box[0] and box[2] <= safe["x_max"] and safe["y_min"] <= box[1] and box[3] <= safe["y_max"]:
                outcome = "inside_safe_area"
            else:
                outcome = "outside_safe_area"
            samples.append({
                "timestamp_seconds": stamp,
                "reasons": item["reasons"],
                "bbox": list(box) if box else None,
                "status": outcome,
            })
    violations = sum(row["status"] == "outside_safe_area" for row in samples)
    empty = sum(row["status"] == "no_visible_ink" for row in samples)
    observed = len(samples) - empty
    status = "sampled_violation" if violations else "no_visible_evidence" if not observed else "sampled_no_violation"
    return {
        "ok": True,
        "renderer": "ffmpeg-libass",
        "evidence": "bounded_dual_background_rgb_samples",
        "scope": "bounded_sampled_timestamps_only",
        "full_interval_verified": False,
        "status": status,
        "duration_seconds": duration,
        "threshold_rgb": 16,
        "safe_area": safe,
        "sample_count": len(samples),
        "visible_sample_count": observed,
        "no_visible_ink_count": empty,
        "violation_count": violations,
        "samples": samples,
        "candidate_count": plan["candidate_count"],
        "omitted_candidate_count": plan["omitted_candidate_count"],
        "recognized_word_count": plan["recognized_word_count"],
        "word_alignment_input_truncated": plan["word_alignment_input_truncated"],
        "max_unsampled_gap_seconds": plan["max_unsampled_gap_seconds"],
        "sample_limit": plan["sample_limit"],
    }

def _preview_times(duration_seconds: float, timestamp: float) -> tuple[float, float]:
    duration = float(duration_seconds)
    instant = float(timestamp)
    if not math.isfinite(duration) or not math.isfinite(instant):
        raise ValueError("Preview duration and timestamp must be finite")
    duration = max(0.25, min(120.0, duration))
    return duration, max(0.0, min(duration - 0.01, instant))


def _write_preview_ass(
    path: Path, entry: PreviewEntry, config: SubtitleRenderConfig,
    source_language: str, target_language: str,
) -> None:
    write_ass(
        [entry], path,
        source_language=source_language or "en",
        target_language=target_language or "zh-CN",
        config=config, overflow_report_path=path.with_suffix(".overflow.json"),
    )


def _render_raw_rgb(
    ffmpeg: str, ass_path: Path, *, timestamp: float, background: str,
    width: int = 1920, height: int = 1080,
) -> bytes:
    if background not in {"black", "white"}:
        raise ValueError("Only canonical black and white analysis backgrounds are allowed")
    # Input cadence is deliberately one frame; the microsecond timebase
    # must be set BEFORE offsetting PTS, otherwise fractional timing rounds
    # to the 1 fps clock and Karaoke/\\move snapshots are incorrect.
    filter_graph = (
        f"settb=AVTB,setpts=PTS+{timestamp:.3f}/TB,"
        f"subtitles='{_escape_subtitles_filter_path(ass_path)}'"
    )
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i",
        f"color=c={background}:s={width}x{height}:r=1:d=1",
        "-vf", filter_graph, "-frames:v", "1",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]
    completed = subprocess.run(command, capture_output=True, timeout=20, check=False)
    expected_bytes = width * height * 3
    if completed.returncode or len(completed.stdout) != expected_bytes:
        detail = completed.stderr.decode("utf-8", errors="replace")[:900]
        raise RuntimeError(f"FFmpeg/libass RGB frame unavailable: {detail}")
    return completed.stdout


def inspect_rendered_ink_frame(
    *, english_text: str, chinese_text: str,
    source_language: str, target_language: str,
    config: SubtitleRenderConfig,
    word_alignments: list[object] | None = None,
    duration_seconds: float = 3.0,
    timestamp: float = 1.0,
) -> dict[str, object]:
    """Check visible subtitle ink at ONE real libass instant, never a whole timeline."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Real rendered-pixel safety inspection requires FFmpeg/libass")
    duration, instant = _preview_times(duration_seconds, timestamp)
    entry = PreviewEntry(
        english=str(english_text or ""), chinese=str(chinese_text or ""),
        display_end_seconds=duration, word_alignments=word_alignments,
    )
    width, height = 1920, 1080
    with tempfile.TemporaryDirectory(prefix="hsr-ass-ink-") as td:
        ass_path = Path(td) / "ink.ass"
        _write_preview_ass(ass_path, entry, config, source_language, target_language)
        on_black = _render_raw_rgb(ffmpeg, ass_path, timestamp=instant, background="black")
        on_white = _render_raw_rgb(ffmpeg, ass_path, timestamp=instant, background="white")

    # Blended shadows can be invisible on black even when text is visible,
    # and light text can be invisible on white. Both reference backgrounds
    # are necessary to retain the dark and light effect coverage.
    box = visible_ink_bbox(on_black, on_white, width, height, threshold=16)
    horizontal = max(0.0, min(0.40, float(config.margin_horizontal_percent)))
    vertical = max(0.0, min(0.40, float(config.margin_vertical_percent)))
    area = SafeArea(
        margin_left_percent=horizontal, margin_right_percent=horizontal,
        margin_top_percent=vertical, margin_bottom_percent=vertical,
    )
    allowed = {"x_min": area.x_min, "x_max": area.x_max,
               "y_min": area.y_min, "y_max": area.y_max}
    if box is None:
        status = "no_visible_ink"
    elif not (
        allowed["x_min"] <= box[0] and box[2] <= allowed["x_max"]
        and allowed["y_min"] <= box[1] and box[3] <= allowed["y_max"]
    ):
        status = "outside_safe_area"
    else:
        status = "inside_safe_area"
    return {
        "ok": True,
        "renderer": "ffmpeg-libass",
        "evidence": "single_frame_dual_background_rgb",
        "scope": "single_timestamp_only",
        "timestamp_seconds": instant,
        "duration_seconds": duration,
        "threshold_rgb": 16,
        "bbox": None if box is None else list(box),
        "safe_area": allowed,
        "status": status,
    }
