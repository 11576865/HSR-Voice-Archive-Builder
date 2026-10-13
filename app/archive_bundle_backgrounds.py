"""Validated, deterministic FFmpeg-native gradient backgrounds for HSR bundles.

Prior art: FFmpeg gradients lavfi, Kdenlive cairogradient, Shotcut gradient.
All source/filter parameters are generated from enumerated modes and validated
hex colors. Never interpolate untrusted filtergraph instructions.
"""
from __future__ import annotations

import re
from typing import Any

COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
MODES = {"color", "image", "linear", "radial", "soft"}
DIRECTIONS = {"diagonal", "horizontal", "vertical"}
RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080)}
QUALITIES = {"standard": 28, "high": 19}

DEFAULT_COLORS = ("#243350", "#0d1729", "#3e4269")


def normalize_bundle_background(raw: object) -> dict[str, str]:
    """Project-persistable spec; rejects filter injection and bad shape."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("MKV background configuration must be an object")
    mode = raw.get("mode", "color")
    direction = raw.get("direction", "diagonal")
    resolution = raw.get("resolution", "720p")
    quality = raw.get("quality", "standard")
    colors = (
        raw.get("first", DEFAULT_COLORS[0]),
        raw.get("second", DEFAULT_COLORS[1]),
        raw.get("third", DEFAULT_COLORS[2]),
    )
    if mode not in MODES:
        raise ValueError("Unsupported MKV background mode")
    if direction not in DIRECTIONS:
        raise ValueError("Unsupported gradient direction")
    if resolution not in RESOLUTIONS or quality not in QUALITIES:
        raise ValueError("Unsupported MKV video resolution or quality")
    if any(not isinstance(c, str) or not COLOR.fullmatch(c) for c in colors):
        raise ValueError("Background colors must be #RRGGBB")
    return {
        "mode": mode, "direction": direction,
        "first": colors[0].lower(), "second": colors[1].lower(),
        "third": colors[2].lower(),
        "resolution": resolution, "quality": quality,
    }


def gradient_source(spec: dict[str, str], *, width: int, height: int, fps: int) -> tuple[str, str | None]:
    """Build a lavfi gradients source with optional soft Gaussian blur."""
    mode = spec["mode"]
    if mode not in {"linear", "radial", "soft"}:
        raise ValueError("Gradient source requires a gradient background")
    if mode == "linear":
        direction = spec["direction"]
        x1, y1 = {
            "diagonal": (width, height),
            "horizontal": (width, 0),
            "vertical": (0, height),
        }[direction]
        coords = f"x0=0:y0=0:x1={x1}:y1={y1}"
        gradient_type, n = "linear", 2
    else:
        coords = f"x0={width // 2}:y0={height // 2}:x1={width}:y1={height // 2}"
        gradient_type, n = "radial", 3 if mode == "soft" else 2
    colors = ":".join(f"c{i}={spec[key][1:]}" for i, key in enumerate(("first", "second", "third")[:n]))
    # Every stop, point, seed and speed is explicit: no default random colors,
    # nondeterministic seed or slowly rotating static archive background.
    src = (f"gradients=s={width}x{height}:r={fps}:type={gradient_type}:"
           f"{colors}:n={n}:{coords}:seed=1:speed=0")
    # gblur is available across more FFmpeg builds than optional frei0r effects.
    return src, "gblur=sigma=24" if mode == "soft" else None


def render_background_preview(
    spec: dict[str, str], *, color: str, image: bytes | None = None,
) -> bytes:
    """Render one authoritative 640x360 FFmpeg frame (not a CSS approximation)."""
    import json
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    spec = normalize_bundle_background(spec)
    if not COLOR.fullmatch(color):
        raise ValueError("Invalid background color")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("背景真实预览需要 FFmpeg 和 FFprobe")
    width, height = 640, 360
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    with tempfile.TemporaryDirectory(prefix=".hsr-bg-preview-") as td:
        if spec["mode"] == "image":
            if not image or len(image) > 4 * 1024 * 1024:
                raise ValueError("Image preview requires a valid image under 4 MiB")
            from .archive_bundle_exporter import image_extension
            path = Path(td) / ("background" + image_extension(image))
            path.write_bytes(image)
            check = subprocess.run(
                [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
                capture_output=True, text=True, check=True, timeout=15,
            )
            frames = [s for s in json.loads(check.stdout).get("streams", [])
                      if s.get("codec_type") == "video"]
            if not frames:
                raise ValueError("Image could not be decoded")
            iw, ih = int(frames[0].get("width") or 0), int(frames[0].get("height") or 0)
            if not (0 < iw <= 8192 and 0 < ih <= 8192 and iw * ih <= 24_000_000):
                raise ValueError("Image dimensions exceed safe limits")
            cmd += ["-i", str(path)]
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:"
                  f"color=0x{color[1:]},setsar=1")
        elif spec["mode"] == "color":
            if image is not None:
                raise ValueError("Color preview cannot include an image")
            cmd += ["-f", "lavfi", "-i",
                    f"color=c=0x{color[1:]}:s={width}x{height}:r=1"]
            vf = None
        else:
            if image is not None:
                raise ValueError("Gradient preview cannot include an image")
            source, vf = gradient_source(spec, width=width, height=height, fps=1)
            cmd += ["-f", "lavfi", "-i", source]
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"]
        result = subprocess.run(
            cmd, capture_output=True, check=False, timeout=25,
        )
        if result.returncode != 0 or not result.stdout.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
            detail = result.stderr.decode("utf-8", "replace")[-600:]
            raise RuntimeError("FFmpeg 背景预览失败（确认当前 FFmpeg 支持 gradients/gblur）：" + detail)
        if len(result.stdout) > 3 * 1024 * 1024:
            raise ValueError("Rendered preview exceeds 3 MiB")
        return result.stdout
