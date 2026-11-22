"""Lossless FLAC clip adapter for the existing PCM-WAV archive pipeline.

Only the Quick/Create *source* is converted. The source tree or ZIP/7z archive
never gets modified. Derived PCM WAVs live in an isolated disposable staging
directory, with an explicit original-FLAC digest and member-ID ledger.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

PROVENANCE_FILENAME = ".flac_input_provenance.json"
PCM_AUDIO_SUFFIXES = {".wav", ".flac"}
MAX_DECODED_CLIP_BYTES = 4 * 1024**3
MAX_DECODED_TOTAL_BYTES = 32 * 1024**3


def load_flac_provenance(wav_root: Path) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(
            (Path(wav_root) / PROVENANCE_FILENAME).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Invalid FLAC input provenance file")
    entries = data.get("entries", {})
    if not isinstance(entries, dict):
        raise ValueError("Invalid FLAC input provenance entries")
    return entries


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_flac(source: Path, *, ffprobe: str) -> tuple[int, int, int, int]:
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries",
             "stream=codec_name,channels,sample_rate,bits_per_raw_sample,duration_ts",
             "-of", "json", str(source)],
            capture_output=True, text=True, check=True, timeout=60,
        )
        streams = json.loads(proc.stdout).get("streams", [])
        if len(streams) != 1 or streams[0].get("codec_name") != "flac":
            raise ValueError("Expected exactly one FLAC audio track")
        stream = streams[0]
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        bits = int(stream.get("bits_per_raw_sample") or 0)
        sample_count = int(stream.get("duration_ts") or 0)
        if not (8000 <= sample_rate <= 384000 and 1 <= channels <= 8):
            raise ValueError("Unsupported FLAC sample rate or channels")
        if bits not in {8, 16, 24, 32}:
            raise ValueError(
                f"Unsupported FLAC bit depth {bits}; expected 8/16/24/32"
            )
        if sample_count <= 0:
            raise ValueError("FLAC sample count must be available for lossless verification")
        return sample_rate, channels, bits, sample_count
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"无法验证原始 FLAC 音频格式：{source.name}：{exc}"
        ) from exc


def decode_flac_clip(
    source: Path,
    target: Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> None:
    """Decode one FLAC clip losslessly and atomically into PCM WAV.

    Verify the exact FLAC STREAMINFO sample count, original bit depth and
    source-byte identity before/after decoding; never accept clipped output.
    """
    from .wavpcm import parse_wav_pcm

    sr, channels, bits, samples = _probe_flac(source, ffprobe=ffprobe)
    estimated_pcm_bytes = samples * channels * (bits // 8)
    if estimated_pcm_bytes + 4096 > MAX_DECODED_CLIP_BYTES:
        raise RuntimeError("FLAC decoded PCM clip exceeds the 4 GiB safety limit")
    original_hash = _sha256(source)
    codec = {8: "pcm_u8", 16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}[bits]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".decoding")
    tmp.unlink(missing_ok=True)
    try:
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-nostdin", "-i", str(source), "-map", "0:a:0",
             "-vn", "-sn", "-dn", "-map_metadata", "-1",
             "-flags", "+bitexact", "-c:a", codec,
             "-f", "wav", "-fs", str(MAX_DECODED_CLIP_BYTES), str(tmp)],
            capture_output=True, check=True, timeout=600,
        )
        info = parse_wav_pcm(tmp)
        if (info.sample_rate, info.channels, info.bits_per_sample) != (
            sr, channels, bits
        ):
            raise RuntimeError("FLAC decoder altered the original PCM format")
        if info.frames != samples:
            raise RuntimeError(
                f"FLAC decoded sample count mismatch: {info.frames} != {samples}"
            )
        if _sha256(source) != original_hash:
            raise RuntimeError("Original FLAC changed during decoding")
        tmp.replace(target)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(f"FLAC lossless decoding failed: {source.name}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def materialize_flac_as_wav(
    source_root: Path,
    staging_root: Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> Path:
    """Create a WAV view of a mixed WAV/FLAC clip tree.

    No FLAC: return the existing tree unchanged (no extra copies).
    FLAC present: copy WAV/LAB and convert FLAC to matching virtual WAV
    paths in an isolated workspace, refusing name collisions.
    """
    source_root = Path(source_root)
    staging_root = Path(staging_root)
    media: list[tuple[Path, Path, str]] = []
    seen_names: set[str] = set()
    has_flac = False
    for source in sorted(source_root.rglob("*")):
        if source.is_symlink():
            raise ValueError(f"Audio source contains a symbolic link: {source}")
        if not source.is_file():
            continue
        ext = source.suffix.casefold()
        if ext not in PCM_AUDIO_SUFFIXES and ext != ".lab":
            continue
        rel = source.relative_to(source_root)
        target_rel = rel.with_suffix(".wav") if ext == ".flac" else rel
        if ext in PCM_AUDIO_SUFFIXES:
            folded = target_rel.as_posix().casefold()
            if folded in seen_names:
                raise ValueError(
                    f"同目录下的 WAV/FLAC 导入路径冲突：{target_rel.as_posix()}"
                )
            seen_names.add(folded)
        media.append((source, target_rel, ext))
        has_flac |= ext == ".flac"
    if not has_flac:
        return source_root
    if staging_root.is_relative_to(source_root):
        raise ValueError("Audio staging directory must not be inside input source")
    staging_root.mkdir(parents=True, exist_ok=True)
    root = staging_root.resolve()
    total_size = 0
    provenance: dict[str, dict[str, str]] = {}
    from .wavpcm import parse_wav_pcm

    for source, rel, ext in media:
        target = staging_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(root):
            raise ValueError(f"Unsafe FLAC staging member: {rel}")
        if ext != ".flac":
            shutil.copy2(source, target)
            continue
        try:
            decode_flac_clip(source, target, ffmpeg=ffmpeg, ffprobe=ffprobe)
            total_size += target.stat().st_size
            if total_size > MAX_DECODED_TOTAL_BYTES:
                target.unlink(missing_ok=True)
                raise RuntimeError("Decoded FLAC corpus exceeds 32 GiB limit")
            provenance[rel.as_posix()] = {
                "original_member": source.relative_to(source_root).as_posix(),
                "original_sha256": _sha256(source),
                "decoded_sha256": _sha256(target),
                "codec": "flac",
            }
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise RuntimeError(
                f"FLAC 无损解码失败（{source.relative_to(source_root)}）：{exc}"
            ) from exc
    (staging_root / PROVENANCE_FILENAME).write_text(
        json.dumps({"schema_version": 1, "entries": provenance},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return staging_root
