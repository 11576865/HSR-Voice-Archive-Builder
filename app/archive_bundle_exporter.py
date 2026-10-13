"""Single-file archival MKV: audio + selectable ASS + project-owned font attachments.

No hard-burned subtitles: the video is only a background. Do not mutate the
previous Black MKV export, continuous FLAC or the canonical ASS.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .project_fonts import font_asset_directory, list_project_fonts

ProgressCallback = Callable[[int, int, str], None]
MAX_BACKGROUND_BYTES = 4 * 1024 * 1024
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
ATTACHMENT_MIME = {
    ".ttf": "application/x-truetype-font",
    ".ttc": "application/x-truetype-font",
    ".otf": "application/vnd.ms-opentype",
}


def decode_bundle_background(encoded: object) -> bytes | None:
    if not encoded:
        return None
    if not isinstance(encoded, str) or len(encoded) > ((MAX_BACKGROUND_BYTES + 2) // 3) * 4 + 16:
        raise ValueError("Background image exceeds the 4 MiB maximum")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Background image must be valid base64") from exc
    if not 0 < len(raw) <= MAX_BACKGROUND_BYTES:
        raise ValueError("Background image must be 1 byte to 4 MiB")
    image_extension(raw)  # Reject misleading/unknown input before job creation.
    return raw


def image_extension(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("Background image must be PNG, JPEG or WebP")


class ArchiveBundleExporter:
    """Create and independently ffprobe-verify one Matroska archival artifact."""

    def __init__(self, *, width: int = 1280, height: int = 720, fps: int = 1) -> None:
        self.width, self.height, self.fps = width, height, fps
        if not 1 <= width <= 3840 or not 1 <= height <= 2160 or not 1 <= fps <= 30:
            raise ValueError("Unsupported background video dimensions or frame rate")
        self.ffmpeg, self.ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError("完整 MKV 封装需要 FFmpeg 和 FFprobe")

    def _probe(self, path: Path) -> dict:
        p = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=25,
        )
        return json.loads(p.stdout)

    def export(
        self, flac: str | Path, ass: str | Path, output: str | Path,
        *, fonts_dir: Path | None = None, include_fonts: bool = True,
        background_color: str = "#000000",
        background_image: bytes | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, object]:
        audio, subtitles, target = Path(flac), Path(ass), Path(output)
        if not audio.is_file() or not subtitles.is_file():
            raise FileNotFoundError("需先生成 continuous.flac 与 HSR_Voice_Archive.ass")
        if audio.stat().st_size == 0 or subtitles.stat().st_size == 0:
            raise ValueError("连续 FLAC 或 ASS 为空，拒绝生成不完整 MKV")
        if not COLOR.fullmatch(str(background_color)):
            raise ValueError("Background color must be #RRGGBB")
        if background_image is not None:
            if not isinstance(background_image, bytes) or not 0 < len(background_image) <= MAX_BACKGROUND_BYTES:
                raise ValueError("Background image exceeds the 4 MiB maximum")
            suffix = image_extension(background_image)
        else:
            suffix = None
        audio_probe = self._probe(audio)
        duration = float(audio_probe.get("format", {}).get("duration") or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("无法验证连续 FLAC 时长")
        if not any(s.get("codec_type") == "audio" and s.get("codec_name") == "flac"
                   for s in audio_probe.get("streams", [])):
            raise ValueError("continuous.flac 不是有效的 FLAC 音轨")
        fonts: list[tuple[Path, str]] = []
        if include_fonts and fonts_dir is not None:
            root = Path(fonts_dir)
            # Restrict to importer-validated files in the project's own directory.
            if root.name != "subtitle_fonts":
                raise ValueError("Only project-owned subtitle_fonts can be attached")
            for record in list_project_fonts(root.parent):
                font = font_asset_directory(root.parent) / str(record["filename"])
                if font.is_symlink() or not font.is_file():
                    raise ValueError("Invalid font attachment")
                fonts.append((font, ATTACHMENT_MIME[font.suffix.lower()]))
        target.parent.mkdir(parents=True, exist_ok=True)
        total_ms = max(1, round(duration * 1000))
        with tempfile.TemporaryDirectory(prefix=".hsr-mkv-bundle-", dir=target.parent) as tmp:
            root = Path(tmp)
            temporary = root / "bundle.mkv"
            color = "0x" + background_color[1:]
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
            if background_image is None:
                cmd += ["-f", "lavfi", "-i",
                        f"color=c={color}:s={self.width}x{self.height}:r={self.fps}"]
                video_filter = None
            else:
                img = root / ("background" + str(suffix))
                img.write_bytes(background_image)
                image_probe = self._probe(img)
                frames = [stream for stream in image_probe.get("streams", [])
                          if stream.get("codec_type") == "video"]
                if not frames:
                    raise ValueError("Background image is not decodable")
                frame = frames[0]
                width, height = int(frame.get("width") or 0), int(frame.get("height") or 0)
                if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 24_000_000):
                    raise ValueError("Background image dimensions exceed safe limits")
                cmd += ["-loop", "1", "-framerate", str(self.fps), "-i", str(img)]
                video_filter = (
                    f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
                    f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2:color={color},"
                    "setsar=1"
                )
            cmd += [
                "-i", str(audio), "-i", str(subtitles),
                "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-tune", "stillimage", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-c:s", "ass",
                "-metadata:s:a:0", "title=HSR continuous voice",
                "-metadata:s:s:0", "title=HSR ASS subtitles",
                "-disposition:s:0", "default",
            ]
            if video_filter:
                cmd += ["-vf", video_filter]
            for index, (font, mime) in enumerate(fonts):
                cmd += ["-attach", str(font),
                        f"-metadata:s:t:{index}", f"mimetype={mime}",
                        f"-metadata:s:t:{index}", f"filename={font.name}"]
            cmd += ["-t", f"{duration:.3f}", "-progress", "pipe:1", "-nostats", str(temporary)]
            if progress:
                progress(0, total_ms, "正在封装连续 FLAC、可开关 ASS 字幕与字体附件")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert proc.stdout is not None
            for line in proc.stdout:
                key, sep, value = line.strip().partition("=")
                if sep and key in {"out_time_us", "out_time_ms"}:
                    try:
                        position = min(total_ms, max(0, int(value) // 1000))
                    except ValueError:
                        continue
                    if progress:
                        progress(position, total_ms, "正在生成完整 MKV")
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            if proc.wait() != 0:
                raise RuntimeError("完整 MKV 封装失败：" + stderr.strip()[-1500:])
            self.validate(temporary, expected_fonts=len(fonts), expected_duration=duration)
            temporary.replace(target)
        if progress:
            progress(total_ms, total_ms, "完整 MKV 已通过音频／视频／软字幕／字体附件验收")
        return {
            "output": str(target), "size_bytes": target.stat().st_size,
            "duration_seconds": duration,
            "audio_codec": "flac", "subtitles_embedded": True,
            "subtitles_burned_in": False, "font_attachments": len(fonts),
            "background": "image" if background_image is not None else background_color,
        }

    def validate(self, output: Path, *, expected_fonts: int, expected_duration: float) -> None:
        probe = self._probe(output)
        rows = probe.get("streams", [])
        types = [str(row.get("codec_type")) for row in rows]
        if types.count("video") != 1 or types.count("audio") != 1 or types.count("subtitle") != 1:
            raise RuntimeError("MKV 校验失败：应恰好包含一个视频、FLAC 音频及 ASS 软字幕轨")
        audio = next(row for row in rows if row.get("codec_type") == "audio")
        subtitle = next(row for row in rows if row.get("codec_type") == "subtitle")
        if audio.get("codec_name") != "flac" or subtitle.get("codec_name") != "ass":
            raise RuntimeError("MKV 校验失败：FLAC 或 ASS 轨道编码不正确")
        attachments = [row for row in rows if row.get("codec_type") == "attachment"]
        if len(attachments) != expected_fonts or types.count("attachment") != expected_fonts:
            raise RuntimeError("MKV 校验失败：字体附件数量不一致")
        for row in attachments:
            if not (row.get("tags") or {}).get("mimetype"):
                raise RuntimeError("MKV 字体附件缺少 MIME 类型")
        duration = float(probe.get("format", {}).get("duration") or 0)
        if not math.isfinite(duration) or abs(duration - expected_duration) > 1.0:
            raise RuntimeError("MKV 校验失败：媒体时长与连续 FLAC 不一致")
