from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

SRT_TS = re.compile(r"^(\d+):(\d+):(\d+),(\d+)$")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def stem_of(name: str) -> str:
    return Path(name).stem


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def collect_labs(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for p in root.rglob("*.lab"):
        text = p.read_text(encoding="utf-8-sig").strip()
        key = p.stem
        if key in result and result[key] != text:
            raise ValueError(f"LAB duplicate with conflicting text: {key}")
        result[key] = text
    return result


def collect_wavs(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for p in root.rglob("*.wav"):
        key = p.name
        if key in result and sha256_file(result[key]) != sha256_file(p):
            raise ValueError(f"WAV duplicate filename with conflicting data: {key}")
        result[key] = p
    return result


def extract_archive(path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            z.extractall(dest)
        return dest
    if suffix == ".7z":
        try:
            import py7zr  # type: ignore
        except ImportError as e:
            raise RuntimeError("Reading .7z requires py7zr: pip install py7zr") from e
        with py7zr.SevenZipFile(path, mode="r") as z:
            z.extractall(path=dest)
        return dest
    raise ValueError(f"Unsupported archive: {path}")


def ensure_dir_or_extract(source: Path, workdir: Path, label: str) -> Path:
    if source.is_dir():
        return source
    if source.is_file() and source.suffix.lower() in {".zip", ".7z"}:
        dest = workdir / label
        if dest.exists():
            shutil.rmtree(dest)
        return extract_archive(source, dest)
    raise FileNotFoundError(source)


def srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def clock_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    m, rem = divmod(ms, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{m:02d}:{s:02d}.{ms:03d}"


@dataclass
class Entry:
    index: int
    group: str
    filename: str
    source: str
    source_detail: str
    english: str
    chinese: str
    chinese_source: str
    sample_rate: int
    channels: int
    sample_width_bits: int
    source_frames: int
    source_duration_seconds: float
    start_sample: int
    audio_end_sample: int
    next_start_sample: int
    start_seconds: float
    audio_end_seconds: float
    display_end_seconds: float
    sha256: str


def build_entries(
    full_index_csv: Path,
    bilingual_csv: Path,
    chs_lab_root: Path,
    wav_root: Path,
    same_group_gap: float = 0.40,
    group_gap: float = 1.20,
) -> tuple[list[Entry], dict[str, object]]:
    full = read_csv_rows(full_index_csv)
    bi = read_csv_rows(bilingual_csv)
    if len(full) != len(bi):
        raise ValueError(f"Row count differs: full={len(full)}, bilingual={len(bi)}")

    bi_by_name = {r["文件名"]: r for r in bi}
    if len(bi_by_name) != len(bi):
        raise ValueError("Duplicate 文件名 in bilingual CSV")

    labs = collect_labs(chs_lab_root)
    wavs = collect_wavs(wav_root)

    sample_rate: int | None = None
    channels: int | None = None
    sample_width: int | None = None
    cursor = 0
    entries: list[Entry] = []
    mismatched_hashes: list[str] = []
    missing_wavs: list[str] = []
    official_count = 0
    translated_count = 0

    # First pass: source audio and start positions.
    raw: list[dict[str, object]] = []
    for i, row in enumerate(full):
        filename = row["文件名"]
        b = bi_by_name.get(filename)
        if not b:
            raise ValueError(f"Missing bilingual row: {filename}")
        wav = wavs.get(filename)
        if not wav:
            missing_wavs.append(filename)
            continue

        with wave.open(str(wav), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            frames = wf.getnframes()

        if sample_rate is None:
            sample_rate, channels, sample_width = sr, ch, sw
        if (sr, ch, sw) != (sample_rate, channels, sample_width):
            raise ValueError(
                f"Mixed PCM format at {filename}: {(sr, ch, sw)} != {(sample_rate, channels, sample_width)}"
            )

        got_hash = sha256_file(wav)
        expected_hash = row.get("SHA-256", "").strip().lower()
        if expected_hash and got_hash.lower() != expected_hash:
            mismatched_hashes.append(filename)

        stem = stem_of(filename)
        if stem in labs:
            chinese = labs[stem]
            chinese_source = "official_chs_lab"
            official_count += 1
        else:
            chinese = b.get("中文", "").strip()
            chinese_source = "translated_existing" if chinese else "missing"
            if chinese:
                translated_count += 1

        raw.append(
            {
                "index": int(row["序号"]),
                "group": row["分组"],
                "filename": filename,
                "source": row.get("来源", ""),
                "source_detail": row.get("来源细分", ""),
                "english": row.get("英文文本", b.get("ENGLISH", "")),
                "chinese": chinese,
                "chinese_source": chinese_source,
                "sample_rate": sr,
                "channels": ch,
                "sample_width_bits": sw * 8,
                "source_frames": frames,
                "source_duration_seconds": frames / sr,
                "sha256": got_hash,
                "wav_path": wav,
            }
        )

    if missing_wavs:
        raise FileNotFoundError(f"Missing {len(missing_wavs)} WAVs, first: {missing_wavs[0]}")
    if mismatched_hashes:
        raise ValueError(f"SHA-256 mismatch for {len(mismatched_hashes)} WAVs, first: {mismatched_hashes[0]}")
    assert sample_rate is not None and channels is not None and sample_width is not None

    same_gap_samples = round(same_group_gap * sample_rate)
    group_gap_samples = round(group_gap * sample_rate)

    for i, r in enumerate(raw):
        start = cursor
        audio_end = start + int(r["source_frames"])
        if i + 1 < len(raw):
            next_group = raw[i + 1]["group"]
            gap = group_gap_samples if next_group != r["group"] else same_gap_samples
            next_start = audio_end + gap
            display_end = max(audio_end, next_start - round(0.001 * sample_rate))
        else:
            next_start = audio_end
            display_end = audio_end
        entries.append(
            Entry(
                index=int(r["index"]),
                group=str(r["group"]),
                filename=str(r["filename"]),
                source=str(r["source"]),
                source_detail=str(r["source_detail"]),
                english=str(r["english"]),
                chinese=str(r["chinese"]),
                chinese_source=str(r["chinese_source"]),
                sample_rate=int(r["sample_rate"]),
                channels=int(r["channels"]),
                sample_width_bits=int(r["sample_width_bits"]),
                source_frames=int(r["source_frames"]),
                source_duration_seconds=float(r["source_duration_seconds"]),
                start_sample=start,
                audio_end_sample=audio_end,
                next_start_sample=next_start,
                start_seconds=start / sample_rate,
                audio_end_seconds=audio_end / sample_rate,
                display_end_seconds=display_end / sample_rate,
                sha256=str(r["sha256"]),
            )
        )
        cursor = next_start

    report = {
        "count_total": len(entries),
        "count_official_chs_lab": official_count,
        "count_translated_existing": translated_count,
        "count_missing_chinese": sum(not e.chinese for e in entries),
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bits": sample_width * 8,
        "same_group_gap_seconds": same_group_gap,
        "group_gap_seconds": group_gap,
        "group_boundaries": sum(entries[i].group != entries[i + 1].group for i in range(len(entries) - 1)),
        "total_samples": cursor,
        "duration_continuous_seconds": cursor / sample_rate,
        "all_source_hashes_match": not mismatched_hashes,
    }
    return entries, report


def write_manifest(entries: list[Entry], report: dict[str, object], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    js = [asdict(e) for e in entries]
    (out_dir / "manifest.json").write_text(
        json.dumps({"report": report, "entries": js}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fields = list(asdict(entries[0]).keys()) if entries else []
    write_csv_rows(out_dir / "manifest.csv", js, fields)

    with (out_dir / "bilingual.srt").open("w", encoding="utf-8-sig", newline="\n") as f:
        for i, e in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{srt_time(e.start_seconds)} --> {srt_time(e.display_end_seconds)}\n")
            f.write(e.english.strip() + "\n")
            f.write(e.chinese.strip() + "\n\n")

    timeline_fields = [
        "index", "start", "audio_end", "display_end", "group", "filename",
        "chinese_source", "chinese", "english", "source_duration_seconds", "sha256"
    ]
    timeline_rows: list[dict[str, object]] = []
    for e in entries:
        timeline_rows.append({
            "index": e.index,
            "start": clock_time(e.start_seconds),
            "audio_end": clock_time(e.audio_end_seconds),
            "display_end": clock_time(e.display_end_seconds),
            "group": e.group,
            "filename": e.filename,
            "chinese_source": e.chinese_source,
            "chinese": e.chinese,
            "english": e.english,
            "source_duration_seconds": f"{e.source_duration_seconds:.6f}",
            "sha256": e.sha256,
        })
    write_csv_rows(out_dir / "bilingual_index_corrected.csv", timeline_rows, timeline_fields)
    (out_dir / "build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_continuous_flac(entries: list[Entry], wav_root: Path, out_flac: Path, compression_level: int = 8) -> dict[str, object]:
    wavs = collect_wavs(wav_root)
    if not entries:
        raise ValueError("No entries")
    sr = entries[0].sample_rate
    ch = entries[0].channels
    sw = entries[0].sample_width_bits // 8
    silence_frame = b"\x00" * (ch * sw)

    out_flac.parent.mkdir(parents=True, exist_ok=True)
    pcm_hash = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="hsr_voice_") as td:
        temp_wav = Path(td) / "continuous.wav"
        with wave.open(str(temp_wav), "wb") as out:
            out.setnchannels(ch)
            out.setsampwidth(sw)
            out.setframerate(sr)
            for i, e in enumerate(entries):
                src = wavs[e.filename]
                with wave.open(str(src), "rb") as wf:
                    while True:
                        frames = wf.readframes(65536)
                        if not frames:
                            break
                        out.writeframesraw(frames)
                        pcm_hash.update(frames)
                if i + 1 < len(entries):
                    gap_frames = e.next_start_sample - e.audio_end_sample
                    remaining = gap_frames
                    block_frames = 65536
                    block = silence_frame * block_frames
                    while remaining > 0:
                        n = min(block_frames, remaining)
                        data = block if n == block_frames else silence_frame * n
                        out.writeframesraw(data)
                        pcm_hash.update(data)
                        remaining -= n

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(temp_wav), "-c:a", "flac", "-compression_level", str(compression_level), str(out_flac)
        ]
        subprocess.run(cmd, check=True)

        # Decode once and hash the PCM to verify lossless identity.
        pcm_formats = {
            1: ("u8", "pcm_u8"),
            2: ("s16le", "pcm_s16le"),
            3: ("s24le", "pcm_s24le"),
            4: ("s32le", "pcm_s32le"),
        }
        if sw not in pcm_formats:
            raise RuntimeError(f"Unsupported PCM sample width for verification: {sw} bytes")
        pcm_format, pcm_codec = pcm_formats[sw]
        decoded = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(out_flac),
                "-f", pcm_format, "-acodec", pcm_codec, "-",
            ],
            stdout=subprocess.PIPE,
        )
        decoded_hash = hashlib.sha256()
        assert decoded.stdout is not None
        while True:
            chunk = decoded.stdout.read(1024 * 1024)
            if not chunk:
                break
            decoded_hash.update(chunk)
        rc = decoded.wait()
        if rc != 0:
            raise RuntimeError(f"ffmpeg decode verification failed: {rc}")

    source_pcm_hash = pcm_hash.hexdigest()
    decoded_pcm_hash = decoded_hash.hexdigest()
    if source_pcm_hash != decoded_pcm_hash:
        raise RuntimeError("FLAC decoded PCM does not match assembled source PCM")

    return {
        "flac_path": str(out_flac),
        "flac_size_bytes": out_flac.stat().st_size,
        "assembled_pcm_sha256": source_pcm_hash,
        "decoded_flac_pcm_sha256": decoded_pcm_hash,
        "lossless_pcm_verified": True,
    }


def build_project(
    full_index_csv: Path,
    bilingual_csv: Path,
    chs_source: Path,
    wav_source: Path,
    out_dir: Path,
    same_group_gap: float = 0.40,
    group_gap: float = 1.20,
    make_flac: bool = True,
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hsr_inputs_") as td:
        work = Path(td)
        chs_root = ensure_dir_or_extract(chs_source, work, "chs")
        wav_root = ensure_dir_or_extract(wav_source, work, "wavs")
        entries, report = build_entries(
            full_index_csv, bilingual_csv, chs_root, wav_root,
            same_group_gap=same_group_gap, group_gap=group_gap,
        )
        write_manifest(entries, report, out_dir)
        if make_flac:
            audio_report = build_continuous_flac(entries, wav_root, out_dir / "continuous.flac")
            report.update(audio_report)
            (out_dir / "build_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            manifest_path = out_dir / "manifest.json"
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["report"] = report
            manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="HSR character voice archive builder")
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--bilingual", type=Path, required=True)
    p.add_argument("--chs", type=Path, required=True, help="Chinese LAB directory / zip / 7z")
    p.add_argument("--wavs", type=Path, required=True, help="WAV directory / zip / 7z")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--same-gap", type=float, default=0.40)
    p.add_argument("--group-gap", type=float, default=1.20)
    p.add_argument("--no-flac", action="store_true")
    a = p.parse_args()
    print(json.dumps(build_project(
        a.index, a.bilingual, a.chs, a.wavs, a.out,
        same_group_gap=a.same_gap, group_gap=a.group_gap,
        make_flac=not a.no_flac,
    ), ensure_ascii=False, indent=2))
