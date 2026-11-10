"""Verify ASS subtitles from native Matroska packets, not lossy ASS text exports.

Some FFmpeg versions clamp textual ASS output at 9:59:59.99 even when
the MKV packets still have correct timestamps above ten hours.
"""
from __future__ import annotations

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path

_FIELDS = ("Layer", "Start", "End", "Style", "Name", "MarginL", "MarginR",
           "MarginV", "Effect", "Text")


def _decode_ffprobe_hex(dump: object) -> bytes:
    """Decode the hex column from ffprobe -show_data, never the ASCII preview."""
    if not isinstance(dump, str) or not dump.strip():
        raise RuntimeError("MKV ASS packet or codec private data is missing")
    out = bytearray()
    for line in dump.splitlines():
        if not line.strip():
            continue
        head, marker, tail = line.partition(":")
        if not marker or not re.fullmatch(r"[0-9A-Fa-f]{8}", head.strip()):
            raise RuntimeError("MKV ASS packet has invalid ffprobe hex data")
        if int(head, 16) != len(out):
            raise RuntimeError("MKV ASS packet hex data is discontinuous")
        hex_part = re.split(r" {2,}", tail.strip(), maxsplit=1)[0]
        if not hex_part or not re.fullmatch(r"[0-9a-fA-F ]+", hex_part):
            raise RuntimeError("MKV ASS packet hex data is invalid")
        try:
            out.extend(bytes.fromhex(hex_part))
        except ValueError as exc:
            raise RuntimeError("MKV ASS packet hex data is malformed") from exc
    return bytes(out)


def _checked_centiseconds(ticks: object, time_base: Fraction) -> int:
    try:
        if not isinstance(ticks, int):
            raise ValueError("timestamp must be an integer tick count")
        time = ticks * time_base * 100
    except (ValueError, TypeError) as exc:
        raise RuntimeError("MKV ASS packet timestamp is invalid") from exc
    if time.denominator != 1 or time < 0:
        raise RuntimeError("MKV ASS packet timestamp lost centisecond precision")
    return int(time)


def _checked_integer(value: str) -> str:
    stripped = value.strip()
    if not re.fullmatch(r"\d+", stripped):
        raise RuntimeError("MKV ASS packet contains invalid layer or margin")
    return str(int(stripped))


def read_native_ass_signature(
    ffprobe: str, mkv: Path, *, timeout: int = 180,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Read first MKV ASS stream using integer PTS/duration and payload.

    ASS packet layout: ReadOrder, Layer, Style, Name, MarginL, MarginR,
    MarginV, Effect, Text. Start and End live in the container timebase.
    Every timestamp must be exactly representable in ASS centiseconds.
    """
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "s:0",
             "-show_streams", "-show_packets", "-show_data", "-show_entries",
             "stream=codec_name,time_base,extradata:packet=pts,duration,data",
             "-of", "json", str(mkv)],
            capture_output=True, text=True, encoding="utf-8",
            errors="strict", check=True, timeout=timeout,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError) as exc:
        raise RuntimeError("MKV 内嵌 ASS 原生数据包回读失败，保留旧 MKV") from exc
    streams = data.get("streams")
    if (not isinstance(streams, list) or len(streams) != 1
            or streams[0].get("codec_name") != "ass"):
        raise RuntimeError("MKV 未包含可核验的独立 ASS 字幕轨")
    stream = streams[0]
    try:
        base = Fraction(str(stream["time_base"]))
        if base <= 0:
            raise ValueError("timebase not positive")
        header = _decode_ffprobe_hex(stream["extradata"]).decode("utf-8-sig")
    except (ValueError, KeyError, UnicodeError, ZeroDivisionError) as exc:
        raise RuntimeError("MKV ASS 轨道格式元数据无效") from exc
    styles = tuple(sorted(
        line.strip() for line in header.splitlines() if line.startswith("Style: ")
    ))
    packets = data.get("packets", [])
    if not isinstance(packets, list):
        raise RuntimeError("MKV ASS 字幕数据包列表无效")
    events: list[tuple[str, ...]] = []
    read_orders: set[int] = set()
    for packet in packets:
        if not isinstance(packet, dict):
            raise RuntimeError("MKV ASS 字幕数据包无效")
        try:
            start = _checked_centiseconds(packet["pts"], base)
            duration = _checked_centiseconds(packet["duration"], base)
            raw = _decode_ffprobe_hex(packet["data"]).decode("utf-8")
            fields = raw.split(",", 8)
            if len(fields) != 9 or duration <= 0:
                raise RuntimeError("MKV ASS 字幕数据包内容或时长无效")
            order_text = fields[0].strip()
            if not re.fullmatch(r"\d+", order_text):
                raise RuntimeError("MKV ASS ReadOrder 字段无效")
            order = int(order_text)
            if order in read_orders:
                raise RuntimeError("MKV ASS ReadOrder 重复")
            read_orders.add(order)
            events.append((
                _checked_integer(fields[1]), str(start), str(start + duration),
                fields[2], fields[3], _checked_integer(fields[4]),
                _checked_integer(fields[5]), _checked_integer(fields[6]),
                fields[7], fields[8],
            ))
        except (KeyError, UnicodeError) as exc:
            raise RuntimeError("MKV ASS 字幕数据包字段不完整或编码无效") from exc
    return styles, tuple(sorted(events))


def assert_native_ass_matches(
    ffprobe: str, mkv: Path,
    expected: tuple[tuple[str, ...], tuple[tuple[str, ...], ...]],
) -> None:
    actual = read_native_ass_signature(ffprobe, mkv)
    if actual == expected:
        return
    changed_styles = actual[0] != expected[0]
    source, embedded = (
        (expected[0], actual[0]) if changed_styles else (expected[1], actual[1])
    )
    first = next(
        (i for i, (a, b) in enumerate(zip(source, embedded)) if a != b),
        min(len(source), len(embedded)),
    )
    field = "Style" if changed_styles else (
        next((_FIELDS[i] for i, (a, b) in enumerate(
            zip(source[first], embedded[first])) if a != b), "unknown")
        if first < min(len(source), len(embedded)) else "count"
    )
    raise RuntimeError(
        "MKV 内的 ASS 字幕文本、时间、样式或特效与最新生成的 ASS 不一致；"
        f"{'样式' if changed_styles else '字幕事件'}第 {first + 1} 项不同（字段 {field}）；"
        f"源样式 {len(expected[0])}/内嵌 {len(actual[0])}，"
        f"源事件 {len(expected[1])}/内嵌 {len(actual[1])}；拒绝覆盖旧 MKV"
    )
