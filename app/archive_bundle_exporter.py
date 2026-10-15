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
from .chapter_audio_tracks import chapter_audio_tracks
from .subtitle_export_status import read_subtitle_export_health
from .archive_bundle_lifecycle import RECEIPT, record_bundle_publication
from .archive_bundle_backgrounds import (
    QUALITIES, MAX_IMAGE_BYTES, gradient_source, normalize_bundle_background,
)

ProgressCallback = Callable[[int, int, str], None]
MAX_BACKGROUND_BYTES = MAX_IMAGE_BYTES
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
        raise ValueError("Background image exceeds the 64 MiB maximum")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Background image must be valid base64") from exc
    if not 0 < len(raw) <= MAX_BACKGROUND_BYTES:
        raise ValueError("Background image must be 1 byte to 64 MiB")
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


def require_current_bundle_subtitles(output_dir: Path) -> None:
    """Reject stale/unverified ASS before creating or publishing an archive.

    Existence of an ASS file is not evidence that it contains the latest text,
    timing overrides and layout for the selected project.
    """
    health = read_subtitle_export_health(output_dir)
    if health.get("state") != "current" or not health.get("artifacts_current") or not health.get("ass_required"):
        detail = str(health.get("artifact_error") or health.get("state") or "unverified")
        raise ValueError(
            "ASS 字幕未通过当前项目的导出一致性校验，请先重新生成 ASS/SRT 后再封装 MKV："
            + detail
        )


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

    @staticmethod
    def _ass_content_signature(content: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Compare the actual selectable ASS styles and events, not just codec.

        Matroska/FFmpeg may rewrite headers and reorder event packets, but must
        preserve every style and rendered event, including original text and
        karaoke/animation override tags.
        """
        lines = content.lstrip("\ufeff").splitlines()
        styles = sorted(line.strip() for line in lines if line.startswith("Style: "))
        events = sorted(line.strip() for line in lines if line.startswith("Dialogue: "))
        return tuple(styles), tuple(events)

    def _verify_embedded_ass(self, mkv: Path, source: Path) -> None:
        expected = self._ass_content_signature(source.read_text(encoding="utf-8-sig"))
        if not expected[1]:
            raise ValueError("Cannot certify an MKV with no source ASS Dialogue events")
        try:
            probe = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-i", str(mkv), "-map", "0:s:0", "-c:s", "copy",
                 "-f", "ass", "-"],
                capture_output=True, check=True, timeout=180,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError("MKV 内嵌 ASS 回读失败；保留上一个成品") from exc
        actual = self._ass_content_signature(probe.stdout.decode("utf-8-sig"))
        if actual != expected:
            raise RuntimeError(
                "MKV 内的 ASS 字幕文本、时间、样式或特效与最新生成的 ASS 不一致；"
                "拒绝覆盖旧 MKV"
            )

    def export(
        self, flac: str | Path, ass: str | Path, output: str | Path,
        *, fonts_dir: Path | None = None, include_fonts: bool = True,
        background_color: str = "#000000",
        background_image: bytes | None = None,
        background_spec: dict[str, str] | None = None,
        include_chapter_flac: bool = False,
        progress: ProgressCallback | None = None,
        require_current_ass: bool = False,
    ) -> dict[str, object]:
        audio, subtitles, target = Path(flac), Path(ass), Path(output)
        if not audio.is_file() or not subtitles.is_file():
            raise FileNotFoundError("需先生成 continuous.flac 与 HSR_Voice_Archive.ass")
        if require_current_ass:
            require_current_bundle_subtitles(target.parent)
        # Full deliveries carry both selectable subtitle representations.
        # Keep the low-level non-certified legacy exporter compatible with
        # archived ASS-only callers; certified production bundles require SRT.
        srt = subtitles.with_suffix(".srt")
        has_srt = srt.is_file() and not srt.is_symlink()
        if require_current_ass and not has_srt:
            raise FileNotFoundError("完整 MKV 需要同时存在 ASS 与 SRT 字幕")
        if audio.stat().st_size == 0 or subtitles.stat().st_size == 0:
            raise ValueError("连续 FLAC 或 ASS 为空，拒绝生成不完整 MKV")
        if not COLOR.fullmatch(str(background_color)):
            raise ValueError("Background color must be #RRGGBB")
        spec = normalize_bundle_background(background_spec or {
            "mode": "image" if background_image is not None else "color",
        })
        if background_image is not None:
            if not isinstance(background_image, bytes) or not 0 < len(background_image) <= MAX_BACKGROUND_BYTES:
                raise ValueError("Background image exceeds the 64 MiB maximum")
            suffix = image_extension(background_image)
        else:
            suffix = None
        if (spec["mode"] == "image") != (background_image is not None):
            raise ValueError("Image background requires an uploaded or saved image; other modes cannot use one")
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
            if root.name != "subtitle_fonts" or root.is_symlink():
                raise ValueError("字体附件必须来自真实项目目录 subtitle_fonts，不能是符号链接")
            for record in list_project_fonts(root.parent):
                font = font_asset_directory(root.parent) / str(record["filename"])
                if font.is_symlink() or not font.is_file():
                    raise ValueError("Invalid font attachment")
                fonts.append((font, ATTACHMENT_MIME[font.suffix.lower()]))
        chapters = chapter_audio_tracks(target.parent) if include_chapter_flac else []
        # Capture exact input identities before FFmpeg opens them: external
        # FLAC, ASS, or font changes can overlap even serialized output jobs.
        source_inputs = (audio, subtitles, *((srt,) if has_srt else ()),
                         *(track for track, _ in chapters),
                         *(font for font, _ in fonts))

        def source_snapshot(path: Path) -> tuple[int, int, int, int]:
            stat = path.stat()
            return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino

        source_before = {path: source_snapshot(path) for path in source_inputs}

        def assert_sources_unchanged() -> None:
            for path, expected in source_before.items():
                try:
                    current = source_snapshot(path)
                except OSError as exc:
                    raise RuntimeError(
                        f"MKV 源文件在封装期间消失，旧成品已保留：{path.name}"
                    ) from exc
                if current != expected:
                    raise RuntimeError(
                        f"MKV 源文件在封装期间发生变化，旧成品已保留：{path.name}"
                    )

        target.parent.mkdir(parents=True, exist_ok=True)
        total_ms = max(1, round(duration * 1000))
        with tempfile.TemporaryDirectory(prefix=".hsr-mkv-bundle-", dir=target.parent) as tmp:
            root = Path(tmp)
            temporary = root / "bundle.mkv"
            color = "0x" + background_color[1:]
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
            if spec["mode"] in {"linear", "radial", "soft"}:
                gradient, still_filter = gradient_source(
                    spec, width=self.width, height=self.height, fps=self.fps,
                )
                # FFmpeg's gradient is static. Render its pixels ONCE rather
                # than running a Gaussian blur on every frame of a long archive.
                still = root / "generated-background.png"
                render = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                          "-f", "lavfi", "-i", gradient]
                if still_filter:
                    render += ["-vf", still_filter]
                render += ["-frames:v", "1", str(still)]
                try:
                    subprocess.run(render, capture_output=True, text=True,
                                   check=True, timeout=90)
                except subprocess.CalledProcessError as exc:
                    raise RuntimeError(
                        "当前 FFmpeg 无法生成渐变背景，请检查 gradients/gblur 支持："
                        + (exc.stderr or "")[-900:]
                    ) from exc
                cmd += ["-loop", "1", "-framerate", str(self.fps), "-i", str(still)]
                video_filter = None
            elif spec["mode"] == "color":
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
            # Main FLAC stays the default; each generated chapter is a separate
            # selectable, stream-copied FLAC rather than a mixed audio bed.
            cmd += ["-i", str(audio), "-i", str(subtitles)]
            if has_srt:
                cmd += ["-i", str(srt)]
            for chapter_path, _ in chapters:
                cmd += ["-i", str(chapter_path)]
            cmd += [
                "-map", "0:v:0", "-map", "1:a:0",
                *(part for n, _ in enumerate(chapters, start=3 + int(has_srt))
                  for part in ("-map", f"{n}:a:0")),
                "-map", "2:s:0",
                *(['-map', '3:s:0'] if has_srt else []),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", str(QUALITIES[spec["quality"]]),
                "-tune", "stillimage", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-c:s:0", "ass",
                "-metadata:s:a:0", "title=HSR continuous voice",
                "-disposition:a:0", "default",
                "-metadata:s:s:0", "title=HSR ASS subtitles",
                "-disposition:s:0", "default",
            ]
            if has_srt:
                cmd += [
                    "-c:s:1", "srt",
                    "-metadata:s:s:1", "title=HSR SRT subtitles",
                    "-disposition:s:1", "0",
                ]
            for index, (_, group_id) in enumerate(chapters, start=1):
                title = group_id.replace("\\r", " ").replace("\\n", " ")[:120]
                cmd += [f"-metadata:s:a:{index}", f"title=HSR {title}",
                        f"-disposition:a:{index}", "0"]
            if video_filter:
                cmd += ["-vf", video_filter]
            for index, (font, mime) in enumerate(fonts):
                cmd += ["-attach", str(font),
                        f"-metadata:s:t:{index}", f"mimetype={mime}",
                        f"-metadata:s:t:{index}", f"filename={font.name}"]
            cmd += ["-t", f"{duration:.3f}", "-progress", "pipe:1", "-nostats", str(temporary)]
            if progress:
                progress(0, total_ms, "正在封装连续 FLAC、可开关 ASS 字幕与字体附件")
            # Read FFmpeg progress on stdout. Its stderr must NOT be a pipe
            # left unread until stdout reaches EOF: if the pipe buffer fills,
            # FFmpeg blocks and the UI sees a permanently frozen job.
            # A temporary log file preserves diagnostics without that deadlock.
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errors, \
                 subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                  stderr=errors, text=True) as proc:
                try:
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
                    code = proc.wait()
                    errors.seek(0)
                    stderr = errors.read()
                    if code != 0:
                        raise RuntimeError("完整 MKV 封装失败：" + stderr.strip()[-1500:])
                except BaseException:
                    # Never leave a background FFmpeg writer running after an
                    # aborted job or callback failure.
                    if proc.poll() is None:
                        proc.terminate()
                    raise
            self.validate(
                temporary, expected_fonts=len(fonts), expected_duration=duration,
                expected_font_names={font.name for font, _ in fonts},
                expected_subtitles=2 if has_srt else 1,
                expected_audio=1 + len(chapters),
            )
            if require_current_ass:
                # A codec/stream-count check cannot detect an old subtitle
                # track accidentally carried into a new container.
                self._verify_embedded_ass(temporary, subtitles)
            assert_sources_unchanged()
            # A user may change the authoritative subtitle text during a long
            # mux. Reject the now-stale temporary MKV rather than replacing the
            # last successfully validated archive.
            if require_current_ass:
                require_current_bundle_subtitles(target.parent)
            if require_current_ass:
                # Prepare project image and receipt BEFORE replacing the old MKV.
                # The temporary inode keeps its size/mtime across os.replace().
                # If publication fails, restore the small previous receipt;
                # the previous MKV's bytes have not been touched.
                previous_receipt = target.parent / RECEIPT
                receipt_backup = root / "previous-receipt.json"
                had_receipt = previous_receipt.is_file()
                if previous_receipt.is_symlink():
                    raise ValueError("Unsafe existing archive bundle receipt")
                if had_receipt:
                    shutil.copyfile(previous_receipt, receipt_backup)
                try:
                    record_bundle_publication(
                        target.parent, background_color=background_color,
                        background_image=background_image, image_extension=suffix,
                        include_fonts=include_fonts, artifact=temporary,
                        background_spec=spec, include_chapter_flac=include_chapter_flac,
                    )
                    require_current_bundle_subtitles(target.parent)
                    assert_sources_unchanged()
                    temporary.replace(target)
                except BaseException:
                    if had_receipt:
                        receipt_backup.replace(previous_receipt)
                    else:
                        previous_receipt.unlink(missing_ok=True)
                    raise
            else:
                temporary.replace(target)
        if progress:
            progress(total_ms, total_ms, "完整 MKV 已通过音频／视频／软字幕／字体附件验收")
        return {
            "output": str(target), "size_bytes": target.stat().st_size,
            "duration_seconds": duration,
            "audio_codec": "flac", "audio_track_count": 1 + len(chapters),
            "chapter_audio_track_count": len(chapters), "subtitles_embedded": True,
            "subtitle_codecs": ["ass", "subrip"] if has_srt else ["ass"],
            "subtitles_burned_in": False, "font_attachments": len(fonts),
            "background": spec["mode"] if spec["mode"] != "color" else background_color,
            "background_spec": spec, "resolution": f"{self.width}x{self.height}",
            "video_quality": spec["quality"],
        }

    def validate(
        self, output: Path, *, expected_fonts: int, expected_duration: float,
        expected_font_names: set[str] | None = None,
        expected_subtitles: int = 1,
        expected_audio: int = 1,
    ) -> None:
        probe = self._probe(output)
        rows = probe.get("streams", [])
        types = [str(row.get("codec_type")) for row in rows]
        if (types.count("video") != 1 or types.count("audio") != expected_audio
                or types.count("subtitle") != expected_subtitles):
            raise RuntimeError("MKV 校验失败：视频、FLAC 音轨或独立字幕轨数量不一致")
        video = next(row for row in rows if row.get("codec_type") == "video")
        if (video.get("codec_name") != "h264"
                or int(video.get("width") or 0) != self.width
                or int(video.get("height") or 0) != self.height):
            raise RuntimeError("MKV 校验失败：H.264 背景视频轨的编码或尺寸不匹配")
        audio_tracks = [row for row in rows if row.get("codec_type") == "audio"]
        subtitles = [row for row in rows if row.get("codec_type") == "subtitle"]
        expected_codecs = ["ass", "subrip"] if expected_subtitles == 2 else ["ass"]
        if (any(row.get("codec_name") != "flac" for row in audio_tracks)
                or [row.get("codec_name") for row in subtitles] != expected_codecs):
            raise RuntimeError("MKV 校验失败：FLAC、ASS 或 SRT 轨道编码不正确")
        attachments = [row for row in rows if row.get("codec_type") == "attachment"]
        if len(attachments) != expected_fonts or types.count("attachment") != expected_fonts:
            raise RuntimeError("MKV 校验失败：字体附件数量不一致")
        attached_names: set[str] = set()
        for row in attachments:
            tags = row.get("tags") or {}
            name = str(tags.get("filename") or "")
            mime = str(tags.get("mimetype") or "")
            if not name or not mime or ATTACHMENT_MIME.get(Path(name).suffix.lower()) != mime:
                raise RuntimeError("MKV 字体附件名称或 MIME 类型不匹配")
            if name in attached_names:
                raise RuntimeError("MKV 字体附件名称重复")
            attached_names.add(name)
        if expected_font_names is not None and attached_names != expected_font_names:
            raise RuntimeError("MKV 校验失败：内嵌字体名称与实际项目字体不一致")
        duration = float(probe.get("format", {}).get("duration") or 0)
        if not math.isfinite(duration) or abs(duration - expected_duration) > 1.0:
            raise RuntimeError("MKV 校验失败：媒体时长与连续 FLAC 不一致")
