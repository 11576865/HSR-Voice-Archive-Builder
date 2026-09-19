from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
PCM_SUBFORMAT_GUID_LE = bytes.fromhex("0100000000001000800000aa00389b71")


@dataclass(frozen=True)
class WavPCMInfo:
    sample_rate: int
    channels: int
    sample_width_bytes: int
    bits_per_sample: int
    valid_bits_per_sample: int
    block_align: int
    frames: int
    data_offset: int
    data_size: int
    extensible: bool


def _parse_fmt(payload: bytes, path: Path) -> tuple[int, int, int, int, int, bool]:
    if len(payload) < 16:
        raise ValueError(f"WAV fmt chunk is too short: {path}")
    tag, channels, sample_rate, _avg, block_align, bits = struct.unpack_from("<HHIIHH", payload, 0)
    valid_bits = bits
    extensible = False

    if tag == WAVE_FORMAT_EXTENSIBLE:
        extensible = True
        if len(payload) < 40:
            raise ValueError(f"WAVE_FORMAT_EXTENSIBLE fmt chunk is too short: {path}")
        cb_size = struct.unpack_from("<H", payload, 16)[0]
        if cb_size < 22:
            raise ValueError(f"Invalid WAVE_FORMAT_EXTENSIBLE cbSize={cb_size}: {path}")
        valid_bits = struct.unpack_from("<H", payload, 18)[0] or bits
        subformat = payload[24:40]
        if subformat != PCM_SUBFORMAT_GUID_LE:
            raise ValueError(f"WAVE_FORMAT_EXTENSIBLE is not PCM: {path}")
    elif tag != WAVE_FORMAT_PCM:
        raise ValueError(f"Unsupported WAV format tag {tag}: {path}")

    if channels < 1 or sample_rate < 1 or bits < 1 or bits % 8:
        raise ValueError(f"Invalid PCM WAV format fields: {path}")
    sample_width = bits // 8
    expected_align = channels * sample_width
    if block_align != expected_align:
        raise ValueError(
            f"Unsupported PCM block alignment in {path}: {block_align} != {expected_align}"
        )
    if valid_bits < 1 or valid_bits > bits:
        raise ValueError(f"Invalid valid-bits field in {path}: {valid_bits}/{bits}")

    return sample_rate, channels, sample_width, valid_bits, block_align, extensible


def parse_wav_pcm(path: Path) -> WavPCMInfo:
    """Parse uncompressed PCM RIFF/RF64 WAV without relying on Python's wave module.

    This intentionally supports WAVE_FORMAT_EXTENSIBLE PCM on Python 3.11,
    where the standard-library wave module does not.
    """
    path = Path(path)
    with path.open("rb") as f:
        header = f.read(12)
        if len(header) != 12:
            raise ValueError(f"Truncated WAV header: {path}")
        riff_id, _riff_size, wave_id = struct.unpack("<4sI4s", header)
        if riff_id not in {b"RIFF", b"RF64"} or wave_id != b"WAVE":
            raise ValueError(f"Not a RIFF/RF64 WAVE file: {path}")

        rf64_data_size: int | None = None
        fmt: tuple[int, int, int, int, int, bool] | None = None
        data_offset: int | None = None
        data_size: int | None = None

        while True:
            chunk_header = f.read(8)
            if not chunk_header:
                break
            if len(chunk_header) != 8:
                raise ValueError(f"Truncated WAV chunk header: {path}")
            chunk_id, size32 = struct.unpack("<4sI", chunk_header)
            payload_offset = f.tell()

            if chunk_id == b"ds64":
                payload = f.read(size32)
                if len(payload) != size32 or size32 < 28:
                    raise ValueError(f"Invalid RF64 ds64 chunk: {path}")
                _riff64, rf64_data_size, _sample_count, table_len = struct.unpack_from("<QQQI", payload, 0)
                minimum = 28 + table_len * 12
                if size32 < minimum:
                    raise ValueError(f"Truncated RF64 ds64 table: {path}")
            elif chunk_id == b"fmt ":
                payload = f.read(size32)
                if len(payload) != size32:
                    raise ValueError(f"Truncated WAV fmt chunk: {path}")
                fmt = _parse_fmt(payload, path)
            elif chunk_id == b"data":
                effective_size = size32
                if riff_id == b"RF64" and size32 == 0xFFFFFFFF:
                    if rf64_data_size is None:
                        raise ValueError(f"RF64 data encountered before ds64 size: {path}")
                    effective_size = rf64_data_size
                data_offset = payload_offset
                data_size = effective_size
                if fmt is not None:
                    break
                f.seek(effective_size, 1)
            else:
                f.seek(size32, 1)

            # RIFF chunks are word-aligned. The padding byte is not part of size.
            skip_size = data_size if chunk_id == b"data" and data_size is not None else size32
            if skip_size % 2:
                f.seek(1, 1)

        if fmt is None:
            raise ValueError(f"WAV has no fmt chunk: {path}")
        if data_offset is None or data_size is None:
            raise ValueError(f"WAV has no data chunk: {path}")

        sample_rate, channels, sample_width, valid_bits, block_align, extensible = fmt
        if data_size % block_align:
            raise ValueError(
                f"WAV data size is not frame-aligned in {path}: {data_size} % {block_align}"
            )
        frames = data_size // block_align
        return WavPCMInfo(
            sample_rate=sample_rate,
            channels=channels,
            sample_width_bytes=sample_width,
            bits_per_sample=sample_width * 8,
            valid_bits_per_sample=valid_bits,
            block_align=block_align,
            frames=frames,
            data_offset=data_offset,
            data_size=data_size,
            extensible=extensible,
        )


def iter_pcm_chunks(
    path: Path,
    info: WavPCMInfo | None = None,
    *,
    chunk_frames: int = 65536,
) -> Iterator[bytes]:
    if chunk_frames < 1:
        raise ValueError("chunk_frames must be >= 1")
    info = info or parse_wav_pcm(path)
    remaining = info.data_size
    chunk_bytes = max(info.block_align, chunk_frames * info.block_align)
    with Path(path).open("rb") as f:
        f.seek(info.data_offset)
        while remaining:
            data = f.read(min(chunk_bytes, remaining))
            if not data:
                raise ValueError(f"Unexpected EOF in WAV data: {path}")
            remaining -= len(data)
            yield data
