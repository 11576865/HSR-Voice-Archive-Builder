#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
HSR Voice Archive Builder

Black Video Exporter

功能:
FLAC -> 黑屏 MKV

特点:
- FLAC 音频 copy
- 黑屏视频生成
- FFmpeg进度显示
- 输出验证
"""

from pathlib import Path
import subprocess
import argparse
import json
import shutil
import re
import time

from tqdm import tqdm



class BlackVideoExporter:


    def __init__(
        self,
        width=1920,
        height=1080,
        fps=24,
        preset="medium",
        crf=23
    ):

        self.width = width
        self.height = height
        self.fps = fps
        self.preset = preset
        self.crf = crf

        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")

        if not self.ffmpeg:
            raise RuntimeError(
                "FFmpeg 未安装"
            )

        if not self.ffprobe:
            raise RuntimeError(
                "FFprobe 未安装"
            )


    def get_duration(self, audio):

        cmd = [
            self.ffprobe,
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(audio)
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )

        return float(
            result.stdout.strip()
        )


    def export(
        self,
        flac,
        output
    ):

        flac = Path(flac)
        output = Path(output)


        duration = self.get_duration(
            flac
        )


        output.parent.mkdir(
            parents=True,
            exist_ok=True
        )


        cmd = [

            self.ffmpeg,

            "-y",

            "-f",
            "lavfi",

            "-i",
            (
                f"color="
                f"black:"
                f"s={self.width}x{self.height}:"
                f"r={self.fps}"
            ),


            "-i",
            str(flac),


            "-c:v",
            "libx264",

            "-preset",
            self.preset,

            "-crf",
            str(self.crf),

            "-tune",
            "stillimage",

            "-pix_fmt",
            "yuv420p",


            "-c:a",
            "copy",


            "-shortest",


            "-progress",
            "pipe:1",

            "-nostats",


            str(output)
        ]


        print(
            f"\n生成: {output.name}"
        )


        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )


        bar = tqdm(
            total=duration,
            unit="s",
            desc="编码"
        )


        current = 0


        for line in process.stdout:


            if "out_time_ms" in line:

                value = (
                    line
                    .split("=")[1]
                    .strip()
                )


                try:

                    current_time = (
                        int(value)
                        /
                        1000000
                    )


                    delta = (
                        current_time
                        -
                        current
                    )


                    if delta > 0:

                        bar.update(
                            delta
                        )

                        current = current_time


                except:

                    pass



        process.wait()

        bar.close()


        if process.returncode != 0:

            raise RuntimeError(
                "FFmpeg 编码失败"
            )


        self.validate(
            output,
            duration
        )


    def validate(
        self,
        output,
        duration
    ):

        if not output.exists():

            raise RuntimeError(
                "输出文件不存在"
            )


        size = output.stat().st_size


        if size == 0:

            raise RuntimeError(
                "输出文件为空"
            )


        print(
            "\n验证完成:"
        )

        print(
            f"文件大小: {size/1024/1024:.2f} MB"
        )

        print(
            "✓ MKV生成成功"
        )



def main():

    parser = argparse.ArgumentParser()


    parser.add_argument(
        "--input",
        required=True
    )


    parser.add_argument(
        "--black-video",
        action="store_true",
        help="生成黑屏MKV"
    )


    parser.add_argument(
        "--output"
    )


    args = parser.parse_args()



    if not args.black_video:

        print(
            "未启用黑屏视频生成"
        )

        return



    output = args.output

    if not output:

        output = (
            Path(args.input)
            .with_suffix(".mkv")
        )



    exporter = BlackVideoExporter()


    exporter.export(
        args.input,
        output
    )



if __name__ == "__main__":

    main()
