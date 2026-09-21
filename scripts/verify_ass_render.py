#!/usr/bin/env python3
"""Automated Render Verification Script for ASS Subtitles using FFmpeg and stdlib / zlib / Pillow.

Renders ASS subtitles over a neutral 1920x1080 black canvas using FFmpeg,
verifies generated preview screenshots, checks pixel bounding boxes for visual
bleed into outer safe area margins (<54px vertical, <192px horizontal), and
exports keyframe preview screenshots into docs/artifacts/preview_shots/.
"""

import argparse
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path


def _get_png_bbox_stdlib(png_path: Path) -> tuple[int, int, int, int] | None:
    """Read a PNG file using stdlib zlib and return (min_x, min_y, max_x, max_y) of non-black pixels."""
    data = png_path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    offset = 8
    width = height = 0
    bit_depth = color_type = 0
    idat_chunks = []

    while offset < len(data):
        length, chunk_type = struct.unpack(">I4s", data[offset:offset + 8])
        chunk_data = data[offset + 8:offset + 8 + length]
        offset += 12 + length

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if not idat_chunks or width == 0 or height == 0:
        return None

    try:
        decompressed = zlib.decompress(b"".join(idat_chunks))
    except Exception:
        return None

    # Color type 2: RGB (3 bytes/pixel), Color type 6: RGBA (4 bytes/pixel), Color type 0: Grayscale (1 byte/pixel)
    bpp = 3 if color_type == 2 else (4 if color_type == 6 else 1)
    line_bytes = 1 + width * bpp

    if len(decompressed) < line_bytes * height:
        return None

    min_x = width
    min_y = height
    max_x = -1
    max_y = -1

    for y in range(height):
        line_start = y * line_bytes
        # Skip filter byte at line_start
        row = decompressed[line_start + 1:line_start + line_bytes]
        for x in range(width):
            px_start = x * bpp
            pixel = row[px_start:px_start + bpp]
            if any(c > 10 for c in pixel[:3]):  # Non-black threshold
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y

    if max_x == -1 or max_y == -1:
        return None

    return (min_x, min_y, max_x + 1, max_y + 1)


def get_image_bbox(png_path: Path) -> tuple[int, int, int, int] | None:
    try:
        from PIL import Image
        img = Image.open(png_path).convert("RGB")
        return img.getbbox()
    except ImportError:
        return _get_png_bbox_stdlib(png_path)


def verify_ass_render(
    ass_path: Path,
    output_dir: Path,
    num_keyframes: int = 5,
) -> dict[str, object]:
    ass_path = ass_path.resolve()
    if not ass_path.is_file():
        raise FileNotFoundError(f"ASS file not found: {ass_path}")

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return {
            "ok": False,
            "error": "FFmpeg is not installed or not available on PATH",
            "rendered_keyframes": 0,
            "screenshots": [],
            "margin_violations_count": 0,
            "margin_violations": [],
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    shots_dir = output_dir / "preview_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    # 1. Parse Dialogue events from ASS file to get keyframe timestamps
    timestamps: list[float] = []
    with ass_path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if line.startswith("Dialogue:"):
                parts = line.split(",", 9)
                if len(parts) >= 3:
                    start_str = parts[1].strip()
                    # ASS time format: H:MM:SS.cs
                    subparts = start_str.split(":")
                    if len(subparts) == 3:
                        h, m, s = subparts
                        start_sec = int(h) * 3600 + int(m) * 60 + float(s)
                        timestamps.append(start_sec)

    if not timestamps:
        timestamps = [1.0, 3.0, 5.0, 7.0, 9.0]
    else:
        # Pick up to num_keyframes distributed across the timeline
        timestamps = sorted(set(timestamps))
        if len(timestamps) > num_keyframes:
            step = len(timestamps) / num_keyframes
            timestamps = [timestamps[int(i * step)] for i in range(num_keyframes)]

    generated_shots: list[str] = []
    margin_violations: list[dict[str, object]] = []

    # Safe Area margins
    margin_left = 192
    margin_right = 1920 - 192  # 1728
    margin_top = 54
    margin_bottom = 1080 - 54  # 1026

    for idx, ts in enumerate(timestamps, 1):
        shot_path = shots_dir / f"keyframe_{idx:02d}.png"
        escaped_ass = str(ass_path).replace("\\", "/").replace(":", "\\:")

        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", "color=c=black:s=1920x1080:d=1",
            "-ss", f"{ts:.2f}",
            "-vf", f"subtitles={escaped_ass}",
            "-vframes", "1",
            str(shot_path),
        ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if not shot_path.is_file() or shot_path.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg render failed for timestamp {ts}s: {res.stderr}")

        generated_shots.append(str(shot_path))

        # 2. Inspect screenshot for margin visual bleed
        bbox = get_image_bbox(shot_path)

        if bbox is not None:
            min_x, min_y, max_x, max_y = bbox
            if min_x < margin_left or max_x > margin_right or min_y < margin_top or max_y > margin_bottom:
                margin_violations.append({
                    "keyframe": shot_path.name,
                    "timestamp": ts,
                    "bbox": bbox,
                    "allowed_margin": {
                        "x_min": margin_left,
                        "x_max": margin_right,
                        "y_min": margin_top,
                        "y_max": margin_bottom,
                    },
                })

    return {
        "ok": True,
        "rendered_keyframes": len(generated_shots),
        "screenshots": generated_shots,
        "margin_violations_count": len(margin_violations),
        "margin_violations": margin_violations,
    }


def main():
    parser = argparse.ArgumentParser(description="FFmpeg Headless ASS Render Verification")
    parser.add_argument("--ass", type=Path, required=True, help="Path to ASS subtitle file")
    parser.add_argument("--out", type=Path, default=Path("docs/artifacts"), help="Output directory")
    parser.add_argument("--keyframes", type=int, default=5, help="Number of keyframe screenshots")
    args = parser.parse_args()

    result = verify_ass_render(args.ass, args.out, num_keyframes=args.keyframes)
    if not result["ok"]:
        print(f"SKIP / WARN: {result['error']}")
        sys.exit(0)

    print(f"Render verification complete: {result['rendered_keyframes']} keyframes generated.")
    if result["margin_violations_count"] > 0:
        print(f"WARNING: {result['margin_violations_count']} margin bleed violation(s) detected!")
        sys.exit(1)
    else:
        print("PASS: Zero visual bleed into safe area margins.")


if __name__ == "__main__":
    main()
