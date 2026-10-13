"""Matroska soft-archive packaging: independent video/audio/ASS/font streams.

This is a muxing product, not a hard-subtitle transcode. The source FLAC is
copied bit-for-bit as the audio stream; the ASS remains switchable text.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Iterable

from .black_video_exporter import BlackVideoExporter

MAX_BACKGROUND_BYTES = 8 * 1024 * 1024
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def decode_background_image(encoded: object) -> tuple[bytes, str] | None:
    if encoded in (None, ""):
        return None
    if not isinstance(encoded, str) or len(encoded) > ((MAX_BACKGROUND_BYTES + 2) // 3) * 4 + 16:
        raise ValueError("Background image exceeds 8 MiB")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 background image") from exc
    if not 0 < len(content) <= MAX_BACKGROUND_BYTES:
        raise ValueError("Background image must be 1–8 MiB")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return content, ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return content, ".jpg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return content, ".webp"
    raise ValueError("Background image must be PNG, JPEG or WebP")


class ArchiveMkvExporter(BlackVideoExporter):
    """Build a single MKV containing still-picture video, FLAC, ASS and fonts."""

    def _probe_background(self, image: Path) -> None:
        result = subprocess.run(
            [self.ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(image)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode:
            raise ValueError("Background image could not be decoded")
        try:
            video = json.loads(result.stdout)["streams"][0]
            width, height = int(video["width"]), int(video["height"])
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ValueError("Background image dimensions could not be verified") from exc
        if not 16 <= width <= 3840 or not 16 <= height <= 2160:
            raise ValueError("Background image dimensions must be 16–3840 by 16–2160")

    def export_archive(
        self,
        source_flac: Path,
        target_mkv: Path,
        *,
        subtitles_ass: Path,
        background_color: str = "#101820",
        background_image: tuple[bytes, str] | None = None,
        font_files: Iterable[Path] = (),
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, object]:
        audio, target, subtitle = Path(source_flac), Path(target_mkv), Path(subtitles_ass)
        if not audio.is_file() or not subtitle.is_file():
            raise FileNotFoundError("Archive MKV requires existing continuous.flac and HSR_Voice_Archive.ass")
        if not COLOR.fullmatch(background_color):
            raise ValueError("Background color must be a six-digit #RRGGBB")
        fonts = [Path(x) for x in font_files]
        if len(fonts) > 32:
            raise ValueError("At most 32 font attachments may be embedded")
        for font in fonts:
            if not font.is_file() or font.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
                raise ValueError("Font attachment must be an existing TTF/OTF/TTC")
        seconds = self.get_duration(audio)
        duration_ms = max(1, round(seconds * 1000))
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.stem + ".part-" + uuid.uuid4().hex[:12] + ".mkv")
        try:
            with tempfile.TemporaryDirectory(prefix="hsr-mkv-image-") as td:
                if background_image is None:
                    source_args = ["-f", "lavfi", "-i",
                                   f"color=c=0x{background_color[1:]}:s={self.width}x{self.height}:r=1"]
                    picture_filter = "format=yuv420p"
                    background_kind = "color"
                else:
                    payload, suffix = background_image
                    image_path = Path(td) / ("background" + suffix)
                    image_path.write_bytes(payload)
                    self._probe_background(image_path)
                    source_args = ["-loop", "1", "-framerate", "1", "-i", str(image_path)]
                    picture_filter = (
                        f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
                        f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2,"
                        "setsar=1,format=yuv420p"
                    )
                    background_kind = "image"
                command = [
                    self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    *source_args, "-i", str(audio), "-i", str(subtitle),
                    "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
                    "-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf),
                    "-tune", "stillimage", "-r", "1", "-vf", picture_filter,
                    "-c:a", "copy", "-c:s", "ass",
                    "-metadata:s:s:0", "language=zho",
                    "-disposition:s:0", "default",
                ]
                for index, font in enumerate(fonts):
                    mime = ("application/vnd.ms-opentype" if font.suffix.lower() == ".otf"
                            else "application/x-truetype-font")
                    command.extend([
                        "-attach", str(font),
                        f"-metadata:s:t:{index}", f"mimetype={mime}",
                        f"-metadata:s:t:{index}", f"filename={font.name}",
                    ])
                command.extend(["-t", f"{seconds:.3f}", "-progress", "pipe:1",
                                "-nostats", str(part)])
                if progress:
                    progress(0, duration_ms, "正在封装 FLAC、软字幕与字体附件")
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    name, sep, value = line.strip().partition("=")
                    if sep and name in {"out_time_ms", "out_time_us"}:
                        try:
                            current = min(duration_ms, max(0, int(value) // 1000))
                            if progress:
                                progress(current, duration_ms, "正在封装项目 MKV")
                        except ValueError:
                            pass
                stderr = process.stderr.read() if process.stderr else ""
                result = process.wait()
                if result:
                    raise RuntimeError("FFmpeg MKV packaging failed: " + (stderr.strip() or f"exit {result}")[:1200])
                self.validate_archive(part, attachment_count=len(fonts))
                part.replace(target)
        finally:
            part.unlink(missing_ok=True)
        if progress:
            progress(duration_ms, duration_ms, "完整软封装 MKV 已验收")
        return {
            "output": str(target), "duration_seconds": seconds,
            "size_bytes": target.stat().st_size, "audio_codec": "flac",
            "subtitles_embedded": True, "subtitle_format": "ass",
            "font_attachment_count": len(fonts), "background": background_kind,
            "hard_subtitles": False,
        }

    def validate_archive(self, path: Path, *, attachment_count: int) -> None:
        result = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode:
            raise RuntimeError("FFprobe could not inspect resulting MKV")
        streams = json.loads(result.stdout).get("streams", [])
        def has_type(kind: str, codec: str) -> bool:
            return any(s.get("codec_type") == kind and s.get("codec_name") == codec for s in streams)
        if not (has_type("video", "h264") and has_type("audio", "flac") and has_type("subtitle", "ass")):
            raise RuntimeError("Archive MKV lacks H.264 video, copied FLAC, or ASS subtitle track")
        attached = [s for s in streams if s.get("codec_type") == "attachment"]
        if len(attached) != attachment_count:
            raise RuntimeError("Archive MKV font attachments are incomplete")
