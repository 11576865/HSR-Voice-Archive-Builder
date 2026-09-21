from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable

ProgressCallback = Callable[[int, int, str], None]


class BlackVideoExporter:
    """Add a lightweight black video track to FLAC without embedding subtitles."""

    def __init__(self, *, width: int = 1920, height: int = 1080, fps: int = 1,
                 preset: str = "ultrafast", crf: int = 28) -> None:
        self.width, self.height, self.fps = width, height, fps
        self.preset, self.crf = preset, crf
        self.ffmpeg, self.ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError("生成黑屏 MKV 需要 FFmpeg 和 FFprobe")

    def get_duration(self, audio: str | Path) -> float:
        result = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
            capture_output=True, text=True, check=True,
        )
        duration = float(result.stdout.strip())
        if duration <= 0:
            raise RuntimeError("无法取得连续 FLAC 的有效时长")
        return duration

    def export(self, flac: str | Path, output: str | Path,
               progress: ProgressCallback | None = None) -> dict[str, object]:
        source, target = Path(flac), Path(output)
        if not source.is_file():
            raise FileNotFoundError(f"连续 FLAC 不存在：{source}")
        duration = self.get_duration(source)
        total_ms = max(1, round(duration * 1000))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.stem + ".part" + target.suffix)
        temporary.unlink(missing_ok=True)
        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=black:s={self.width}x{self.height}:r={self.fps}",
            "-i", str(source), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf),
            "-tune", "stillimage", "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-shortest", "-progress", "pipe:1", "-nostats", str(temporary),
        ]
        if progress:
            progress(0, total_ms, "正在生成黑屏视频轨并封装 FLAC")
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert process.stdout is not None
        for raw in process.stdout:
            key, separator, value = raw.strip().partition("=")
            if not separator or key not in {"out_time_ms", "out_time_us"}:
                continue
            try:
                current_ms = min(total_ms, max(0, int(value) // 1000))
            except ValueError:
                continue
            if progress:
                progress(current_ms, total_ms, "正在生成黑屏视频轨并封装 FLAC")
        stderr = process.stderr.read() if process.stderr is not None else ""
        return_code = process.wait()
        if return_code:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg 生成黑屏 MKV 失败：" + (stderr.strip() or f"exit {return_code}"))
        self.validate(temporary)
        temporary.replace(target)
        if progress:
            progress(total_ms, total_ms, "黑屏 MKV 已生成")
        return {"output": str(target), "duration_seconds": duration,
                "size_bytes": target.stat().st_size, "subtitles_embedded": False}

    def validate(self, output: str | Path) -> None:
        path = Path(output)
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("生成的黑屏 MKV 不存在或为空")
        result = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        )
        types = {str(row.get("codec_type")) for row in json.loads(result.stdout).get("streams", [])}
        if not {"video", "audio"}.issubset(types):
            raise RuntimeError("黑屏 MKV 校验失败：缺少视频轨或音频轨")


def main() -> None:
    parser = argparse.ArgumentParser(description="为连续 FLAC 生成不含字幕的黑屏 MKV")
    parser.add_argument("--input", required=True)
    parser.add_argument("--black-video", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if not args.black_video:
        print("未启用黑屏视频生成")
        return
    output = Path(args.output) if args.output else Path(args.input).with_suffix(".mkv")
    print(json.dumps(BlackVideoExporter().export(args.input, output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
