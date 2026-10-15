"""Bounded, real FFmpeg/libass ink oracle for bilingual ASS layout candidates.

Never treats Pillow bounding boxes, SVG outlines or successful FFmpeg exit status
as actual glyph occupancy. Each layer is isolated with the selected ASS family,
size, position, border and shadow before computing its visible pixel envelope.
This is a still-frame layout oracle; dynamic motion/animation require additional
sampled-frame acceptance.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .config import SubtitleRenderConfig
from .layout_solver import SolvedLayout
from .measure import measure_text_width
from .safe_area import SafeArea

try:
    from PIL import Image, ImageChops
except ImportError:
    Image = ImageChops = None


class InkProbeUnavailable(RuntimeError):
    """Explicit rejection when an authentic font-ink oracle is unavailable."""


@dataclass(frozen=True)
class InkBounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class InkVerdict:
    ok: bool
    reason: str | None
    primary: InkBounds | None
    chinese: InkBounds | None
    primary_width_ratio: float
    chinese_width_ratio: float


def _ass_number(value: float) -> str:
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def _ass_path(path: Path) -> str:
    # Quote-safe FFmpeg filter path; only trusted tempfile paths are permitted.
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _font_env(cfg: SubtitleRenderConfig, workdir: Path) -> dict[str, str]:
    env = os.environ.copy()
    dirs = sorted({
        str(Path(p).resolve().parent)
        for p in (cfg.chs_font_path, cfg.primary_font_path)
        if p and Path(p).is_file()
    })
    if not dirs:
        return env
    existing = next(
        (Path(s) for s in (
            env.get("FONTCONFIG_FILE", ""),
            str(Path(env.get("PREFIX", "/none")) / "etc/fonts/fonts.conf"),
            "/etc/fonts/fonts.conf",
        ) if s and Path(s).is_file()),
        None,
    )
    include = (f'<include ignore_missing="yes">{xml_escape(str(existing))}</include>'
               if existing else "")
    xml = workdir / "fontconfig.xml"
    xml.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        "<fontconfig>" +
        "".join(f"<dir>{xml_escape(d)}</dir>" for d in dirs) +
        include + "</fontconfig>",
        encoding="utf-8",
    )
    env["FONTCONFIG_FILE"] = str(xml)
    return env


def _layer_ass(lines: list[str], *, family: str, size: int, x: int, y: int,
               alignment: int, outline: float, shadow: float, blur: float,
               multi_outline: bool) -> str:
    # ASS WrapStyle=2: only the solver's explicit \N may create line breaks.
    # Probe uses the same style, \fs, border/shadow and an/pos semantics as
    # the real writer, without HUD/cards/temporal animation.
    if not lines:
        raise ValueError("No subtitle lines to probe")
    family = family.replace(",", " ").replace("\n", " ").replace("\r", " ")
    styles = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Ink,{family},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H00000000,0,0,0,0,100,100,0,0,1,{_ass_number(outline)},"
        f"{_ass_number(shadow)},8,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )
    content = r"\N".join(lines)
    if any("{" in t or "}" in t or "\n" in t for t in lines):
        # Untrusted overrides must never be interpreted by the pixel oracle.
        raise InkProbeUnavailable("Cannot independently measure ASS override/drawing text")
    tags = rf"{{\an{alignment}\pos({x},{y})\fs{size}\blur{_ass_number(blur)}}}"
    events = ""
    if multi_outline:
        outer_border = max(outline + 3.0, outline * 1.8)
        outer_shadow = max(shadow, 3.0)
        outer_blur = max(blur, 0.6)
        outer = (rf"{{\an{alignment}\pos({x},{y})\fs{size}"
                 rf"\bord{_ass_number(outer_border)}\3c&H000000&\3a&H40&"
                 rf"\shad{_ass_number(outer_shadow)}\4c&H000000&"
                 rf"\blur{_ass_number(outer_blur)}}}")
        events += f"Dialogue: 0,0:00:00.00,0:00:02.00,Ink,,0,0,0,,{outer}{content}\n"
    events += f"Dialogue: 1,0:00:00.00,0:00:02.00,Ink,,0,0,0,,{tags}{content}\n"
    return styles + events


def _raw_rgb24_bbox(raw: bytes, width: int, height: int,
                    background: bytes) -> tuple[int, int, int, int] | None:
    if len(raw) != width * height * 3 or len(background) != 3:
        raise InkProbeUnavailable("Invalid raw RGB frame for libass ink measurement")
    bg_row = background * width
    stride = width * 3
    x0, y0, x1, y1 = width, height, 0, 0
    view = memoryview(raw)
    for y in range(height):
        row = view[y * stride:(y + 1) * stride]
        if row == bg_row:
            continue
        for x in range(width):
            at = 3 * x
            delta = (abs(row[at] - background[0]) +
                     abs(row[at + 1] - background[1]) +
                     abs(row[at + 2] - background[2]))
            if delta > 36:
                x0 = min(x0, x)
                x1 = max(x1, x + 1)
                y0 = min(y0, y)
                y1 = max(y1, y + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _raster_bounds(ass_text: str, cfg: SubtitleRenderConfig, ffmpeg: str) -> InkBounds | None:
    with tempfile.TemporaryDirectory(prefix="hsr-libass-ink-") as td:
        root = Path(td)
        ass = root / "probe.ass"
        ass.write_text(ass_text, "utf-8-sig")
        font_dirs = sorted({
            str(Path(p).resolve().parent) for p in
            (cfg.chs_font_path, cfg.primary_font_path)
            if p and Path(p).is_file()
        })
        fontsdir = (f":fontsdir='{_ass_path(Path(font_dirs[0]))}'"
                    if len(font_dirs) == 1 else "")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i",
            "color=c=0x617283:s=1920x1080:r=1:d=2",
            "-vf", f"ass='{_ass_path(ass)}'{fontsdir}",
            "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=15, check=False,
                                  env=_font_env(cfg, root))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InkProbeUnavailable(f"libass ink probe unavailable: {exc}") from exc
        if proc.returncode or len(proc.stdout) != 1920 * 1080 * 3:
            message = proc.stderr.decode("utf-8", errors="replace")[:600]
            raise InkProbeUnavailable(f"libass ink probe failed: {message}")
        bg = proc.stdout[:3]
        if Image is not None and ImageChops is not None:
            img = Image.frombytes("RGB", (1920, 1080), proc.stdout)
            delta = ImageChops.difference(img, Image.new("RGB", img.size, tuple(bg)))
            mask = delta.convert("L").point(lambda v: 255 if v > 12 else 0)
            box = mask.getbbox()
        else:
            # Pillow is an optional accelerator, not a reason to reject a
            # functioning FFmpeg/libass installation (notably minimal CI).
            # Inspect only non-background rows; a uniform FFmpeg color frame
            # skips ~90% of pixels without costly per-pixel Python iteration.
            box = _raw_rgb24_bbox(proc.stdout, 1920, 1080, bg)
        return InkBounds(*box) if box else None


def validate_real_ink(layout: SolvedLayout, cfg: SubtitleRenderConfig,
                      safe_area: SafeArea) -> InkVerdict:
    """Measure actual libass colored-background raster bounds of each language.

    If any true font ink leaves the safe region or central separation, reject
    that candidate so solve_subtitle_layout can try a smaller font scale.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise InkProbeUnavailable("FFmpeg is required for actual subtitle layout validation")
    results: dict[str, InkBounds | None] = {}
    for key, lines, family in (
        ("primary", layout.primary_lines, cfg.primary_font),
        ("chinese", layout.chs_lines, cfg.chs_font),
    ):
        if not lines:
            results[key] = None
            continue
        first = lines[0]
        ass = _layer_ass(
            [p.text for p in lines], family=family, size=first.font_size,
            x=first.x, y=first.y, alignment=first.alignment,
            outline=cfg.outline_width, shadow=cfg.shadow_depth,
            blur=cfg.blur_radius, multi_outline=cfg.enable_multi_layer_outline,
        )
        # Cache on actual emitted probe bytes, font content/mtime and chosen
        # runtime. A complete archive may need thousands of unique bilingual
        # layouts; retain enough signatures to accelerate re-export after edits.
        fingerprint = []
        for value in (cfg.primary_font_path, cfg.chs_font_path):
            if value and Path(value).is_file():
                st = Path(value).stat()
                fingerprint.append((str(Path(value).resolve()), st.st_size, st.st_mtime_ns))
        signature = hashlib.sha256(repr((ass, fingerprint, ffmpeg)).encode()).hexdigest()
        cached = _INK_CACHE.get(signature)
        if cached is None:
            box = _raster_bounds(ass, cfg, ffmpeg)
            if len(_INK_CACHE) >= MAX_INK_CACHE_ENTRIES:
                _INK_CACHE.pop(next(iter(_INK_CACHE)))
            _INK_CACHE[signature] = (box,)
        else:
            box = cached[0]
        if box is None:
            return InkVerdict(False, "LIBASS_NO_VISIBLE_GLYPHS", None, None, 1.0, 1.0)
        results[key] = box

    primary = results.get("primary")
    chinese = results.get("chinese")
    reason = None
    for bound in (primary, chinese):
        if bound and (bound.left < safe_area.x_min or bound.right > safe_area.x_max):
            reason = "LIBASS_HORIZONTAL_OVERFLOW"
            break
        if bound and (bound.top < safe_area.y_min or bound.bottom > safe_area.y_max):
            reason = "LIBASS_VERTICAL_OVERFLOW"
            break
    if not reason and primary and chinese:
        if chinese.top - primary.bottom < cfg.min_central_gap:
            reason = "LIBASS_CENTRAL_COLLISION"

    def ratio(items: list, bounds: InkBounds | None, fontpath: str | None) -> float:
        if not items or bounds is None:
            return 1.0
        width = max(measure_text_width(line.text, line.font_size, fontpath)
                    for line in items)
        return max(0.35, min(2.5, bounds.width / width)) if width > 0 else 1.0

    return InkVerdict(
        ok=reason is None, reason=reason,
        primary=primary, chinese=chinese,
        primary_width_ratio=ratio(layout.primary_lines, primary, cfg.primary_font_path),
        chinese_width_ratio=ratio(layout.chs_lines, chinese, cfg.chs_font_path),
    )


MAX_INK_CACHE_ENTRIES = 16384
_INK_CACHE: dict[str, tuple[InkBounds | None]] = {}
