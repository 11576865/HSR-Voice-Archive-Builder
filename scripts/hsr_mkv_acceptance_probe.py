#!/usr/bin/env python3
"""Read-only final-MKV technical evidence for Windows/Linux/Android Termux.

This is NOT a player compatibility, perceptual banding, or Android device pass.
Requires only Python stdlib, FFmpeg and FFprobe. It does not modify the MKV.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PNG_HEADER = bytes.fromhex("89504e470d0a1a0a")
FONT_MIME = {
    ".ttf": {"application/x-truetype-font", "application/x-font-ttf", "font/ttf"},
    ".ttc": {"application/x-truetype-font", "application/x-font-ttf", "font/collection"},
    ".otf": {"application/vnd.ms-opentype", "font/otf"},
}


def _run(command: list[str], *, timeout: int = 45) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, timeout=timeout, check=False)


def _version(executable: str) -> str:
    proc = _run([executable, "-version"], timeout=15)
    return proc.stdout.decode("utf-8", "replace").splitlines()[0] if proc.returncode == 0 and proc.stdout else "unavailable"


def _filters(executable: str) -> dict[str, bool]:
    proc = _run([executable, "-hide_banner", "-filters"], timeout=15)
    if proc.returncode != 0:
        return {"gradients": False, "gblur": False, "ass": False}
    names = set()
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) >= 2 and re.fullmatch(r"[.TSC|]{3}", fields[0]):
            names.add(fields[1])
    return {name: name in names for name in ("gradients", "gblur", "ass")}


def _device_info() -> dict[str, str]:
    """Opt-in-free anonymous Android runtime metadata, no serial/user IDs."""
    getprop = shutil.which("getprop")
    if not getprop:
        return {"environment": "desktop-or-unknown"}
    result = {"environment": "android-getprop"}
    for name, prop in (("android_release", "ro.build.version.release"),
                       ("device_model", "ro.product.model")):
        try:
            output = _run([getprop, prop], timeout=5)
            if output.returncode == 0:
                result[name] = output.stdout.decode("utf-8", "replace").strip()[:100]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_streams(probe: dict) -> tuple[list[str], dict]:
    errors: list[str] = []
    rows = probe.get("streams") or []
    if not isinstance(rows, list):
        return ["FFprobe streams is not a list"], {}
    grouped = {kind: [row for row in rows if isinstance(row, dict) and row.get("codec_type") == kind]
               for kind in ("video", "audio", "subtitle", "attachment")}
    for kind, codec in (("video", "h264"), ("audio", "flac"), ("subtitle", "ass")):
        if len(grouped[kind]) != 1 or grouped[kind][0].get("codec_name") != codec:
            errors.append(f"Expected exactly one {kind} stream encoded as {codec}")
    if len(grouped["video"]) == 1:
        v = grouped["video"][0]
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        if not (1 <= w <= 3840 and 1 <= h <= 2160 and w * h <= 8_294_400):
            errors.append("Video dimensions are outside the bounded HSR export range")
    else:
        w, h = 0, 0
    attachment_names: list[str] = []
    for stream in grouped["attachment"]:
        tags = stream.get("tags") or {}
        name, mime = str(tags.get("filename") or ""), str(tags.get("mimetype") or "")
        allowed = FONT_MIME.get(Path(name).suffix.lower())
        if not name or not allowed or mime not in allowed or Path(name).name != name:
            errors.append("Unexpected attachment identity/MIME; expected project font")
        else:
            attachment_names.append(name)
    if len(set(attachment_names)) != len(attachment_names):
        errors.append("Duplicate font attachment names")
    fmt = probe.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (ValueError, TypeError, OverflowError):
        duration = 0
    if not (math.isfinite(duration) and duration > 0):
        errors.append("Invalid or absent container duration")
    return errors, {
        "container": fmt.get("format_name"),
        "duration_seconds": duration if math.isfinite(duration) else 0,
        "video_width": w, "video_height": h,
        "video_codec": grouped["video"][0].get("codec_name") if len(grouped["video"]) == 1 else None,
        "audio_codec": grouped["audio"][0].get("codec_name") if len(grouped["audio"]) == 1 else None,
        "subtitle_codec": grouped["subtitle"][0].get("codec_name") if len(grouped["subtitle"]) == 1 else None,
        "attachment_names": attachment_names,
    }


def _decode_png(executable: str, artifact: Path, second: float) -> tuple[bytes, float]:
    cmd = [
        executable, "-hide_banner", "-loglevel", "error", "-ss", f"{second:.3f}",
        "-i", str(artifact), "-map", "0:v:0", "-frames:v", "1",
        "-an", "-sn", "-f", "image2pipe", "-c:v", "png", "pipe:1",
    ]
    before = time.perf_counter()
    p = _run(cmd, timeout=90)
    elapsed = round(time.perf_counter() - before, 4)
    if p.returncode != 0 or not p.stdout.startswith(PNG_HEADER):
        stderr = p.stderr.decode("utf-8", "replace")[-500:]
        raise RuntimeError("Encoded H.264 frame decode failed: " + stderr)
    if len(p.stdout) > 32 * 1024 * 1024:
        raise ValueError("Decoded PNG exceeds safety limit")
    return p.stdout, elapsed


def inspect(artifact: Path, *, ffmpeg: str, ffprobe: str) -> tuple[dict, dict[str, bytes]]:
    """Return auditable technical report and decoded final-encoded PNG samples."""
    report: dict = {
        "schema_version": 1,
        "scope": "technical_streams_and_decoded_h264_only",
        "status": "technical_fail",
        "device_playback": "unverified",
        "visual_banding": "unverified",
        "long_duration_performance": "unverified",
        "errors": [],
        "environment": _device_info(),
        "ffmpeg_version": _version(ffmpeg),
        "ffprobe_version": _version(ffprobe),
        "filter_availability": _filters(ffmpeg),
        "artifact_name": artifact.name,
        "samples": [],
    }
    images: dict[str, bytes] = {}
    if not artifact.is_file() or artifact.is_symlink():
        report["errors"].append("MKV file is absent, or a symlink")
        return report, images
    stat = artifact.stat()
    report["size_bytes"] = stat.st_size
    report["mtime_ns"] = stat.st_mtime_ns
    proc = _run([ffprobe, "-v", "error", "-show_format", "-show_streams",
                 "-of", "json", str(artifact)], timeout=45)
    if proc.returncode != 0:
        report["errors"].append("FFprobe failed: " + proc.stderr.decode("utf-8", "replace")[-500:])
        return report, images
    try:
        metadata = json.loads(proc.stdout)
        errors, summary = _verify_streams(metadata)
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        report["errors"].append(f"Invalid FFprobe metadata: {exc}")
        return report, images
    report["media"] = summary
    report["errors"].extend(errors)
    if report["errors"]:
        return report, images
    duration = summary["duration_seconds"]
    times = [0.0, min(duration / 2, max(0.0, duration - 0.25))]
    if times[1] < 0.25:
        times = times[:1]
    for index, t in enumerate(times):
        name = f"decoded_h264_{index}.png"
        try:
            png, seconds = _decode_png(ffmpeg, artifact, t)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            report["errors"].append(str(exc))
            break
        images[name] = png
        report["samples"].append({
            "requested_time_seconds": round(t, 3), "png_name": name,
            "decoded_png_sha256": _sha256(png),
            "decoded_png_bytes": len(png),
            "decode_wall_seconds": seconds,
        })
    if not report["errors"]:
        report["status"] = "technical_pass"
    return report, images


def _save_evidence(directory: Path, report: dict, images: dict[str, bytes]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, png in images.items():
        target = directory / name
        target.write_bytes(png)
    out = directory / "report.json"
    tmp = directory / "report.json.part"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only final-MKV technical acceptance; device playback remains unverified."
    )
    parser.add_argument("mkv", type=Path, help="Existing HSR_Voice_Archive_Bundle.mkv")
    parser.add_argument("--evidence-dir", type=Path, help="Where to save decoded encoded-frame PNGs and report.json")
    args = parser.parse_args(argv)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        print("FFmpeg and FFprobe are required; no file was altered.", file=sys.stderr)
        return 2
    try:
        report, images = inspect(args.mkv, ffmpeg=ffmpeg, ffprobe=ffprobe)
        if args.evidence_dir:
            _save_evidence(args.evidence_dir, report, images)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("Probe failed: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "technical_pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
