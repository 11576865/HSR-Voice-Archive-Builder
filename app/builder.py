from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .timeline import (
    resolve_timeline,
    write_ass,
    write_resolved_timeline,
    write_srt,
)
from .wavpcm import iter_pcm_chunks, parse_wav_pcm

SRT_TS = re.compile(r"^(\d+):(\d+):(\d+),(\d+)$")
DEFAULT_MAX_EXTRACT_BYTES = 32 * 1024**3
MAX_ARCHIVE_MEMBERS = 100_000

# Voice packages for different localizations often differ only in the
# character-name component: e.g. chapter5_13_evanescia_103 versus
# chapter5_13_绯英_103. Group plus numeric tail is usable only when it is
# unique on both sides; ordering is never a safe subtitle-matching fallback.
_CROSS_LANGUAGE_KEY_RE = re.compile(
    r"^(?P<group>(?:archive|chapter\d+(?:_\d+)?|companion\d+(?:_\d+)?|side\d+(?:_\w+)?))_.+?_(?P<tail>\d+(?:_[fm])?)$",
    re.IGNORECASE,
)


def cross_language_voice_key(filename: str) -> str:
    stem = Path(filename).stem.casefold()
    match = _CROSS_LANGUAGE_KEY_RE.match(stem)
    if match:
        return f"{match.group('group').casefold()}::{match.group('tail').casefold()}"
    return stem


def map_labs_to_voice_filenames(
    voice_filenames: Iterable[str], labs: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, int]]:
    """Map official LAB text without ever pairing records by position."""
    names = [Path(name).name for name in voice_filenames]
    mapped: dict[str, str] = {}
    exact = 0
    for name in names:
        text = str(labs.get(Path(name).stem, "") or "").strip()
        if text:
            mapped[name] = text
            exact += 1

    mapped_stems = {Path(name).stem for name in mapped}
    names_by_key: dict[str, list[str]] = {}
    for name in names:
        if name not in mapped:
            names_by_key.setdefault(cross_language_voice_key(name), []).append(name)
    labs_by_key: dict[str, list[tuple[str, str]]] = {}
    for stem, raw_text in labs.items():
        text = str(raw_text or "").strip()
        if stem in mapped_stems or not text:
            continue
        labs_by_key.setdefault(cross_language_voice_key(stem), []).append((stem, text))

    structural = 0
    for key, matches in names_by_key.items():
        candidates = labs_by_key.get(key, [])
        if len(matches) == 1 and len(candidates) == 1:
            mapped[matches[0]] = candidates[0][1]
            structural += 1
    return mapped, {
        "exact": exact,
        "structural": structural,
        "total": len(mapped),
        "unmatched": len(names) - len(mapped),
    }


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_csv_rows(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


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


def _max_extract_bytes() -> int:
    raw = os.environ.get("HSR_MAX_EXTRACT_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_EXTRACT_BYTES
    value = int(raw)
    if value < 1:
        raise ValueError("HSR_MAX_EXTRACT_BYTES must be positive")
    return value


def _validate_member_name(name: str, dest: Path) -> None:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("\\"):
        raise ValueError(f"Unsafe archive member path: {name!r}")
    first = normalized.split("/", 1)[0]
    if ":" in first:
        raise ValueError(f"Unsafe archive member drive path: {name!r}")
    target = (dest / normalized).resolve()
    try:
        target.relative_to(dest.resolve())
    except ValueError as exc:
        raise ValueError(f"Archive member escapes extraction directory: {name!r}") from exc


def _extract_zip(path: Path, dest: Path, max_bytes: int) -> None:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"ZIP has too many members: {len(infos)}")
        total = 0
        for info in infos:
            _validate_member_name(info.filename, dest)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ValueError(f"ZIP symbolic links are not accepted: {info.filename}")
            total += max(0, info.file_size)
            if total > max_bytes:
                raise ValueError(
                    f"ZIP uncompressed size exceeds safety limit: {total} > {max_bytes} bytes"
                )
        z.extractall(dest)


def _parse_7z_slt(text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    in_entries = False
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if line.startswith("----------"):
            in_entries = True
            current = {}
            continue
        if not in_entries:
            continue
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key.strip()] = value.strip()
    if current:
        records.append(current)
    return records


def _extract_7z_cli(path: Path, dest: Path, max_bytes: int) -> None:
    exe = shutil.which("7zz") or shutil.which("7z")
    if not exe:
        raise RuntimeError(
            "Reading .7z requires py7zr>=1.1.3 or a native 7zz/7z executable"
        )

    listed = subprocess.run(
        [exe, "l", "-slt", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError(
            f"7-Zip listing failed ({listed.returncode}): {listed.stderr.strip()}"
        )

    records = _parse_7z_slt(listed.stdout)
    if len(records) > MAX_ARCHIVE_MEMBERS:
        raise ValueError(f"7z has too many members: {len(records)}")

    total = 0
    for record in records:
        name = record.get("Path", "")
        if not name:
            continue
        _validate_member_name(name, dest)
        attrs = record.get("Attributes", "")
        if (
            record.get("Symbolic Link")
            or record.get("Hard Link")
            or " l" in f" {attrs.lower()}"
            or attrs.lower().endswith("l")
        ):
            raise ValueError(f"7z links are not accepted: {name}")
        size_text = record.get("Size", "0").strip()
        try:
            size = int(size_text or "0")
        except ValueError as exc:
            raise ValueError(f"Invalid 7z member size for {name}: {size_text!r}") from exc
        total += max(0, size)
        if total > max_bytes:
            raise ValueError(
                f"7z uncompressed size exceeds safety limit: {total} > {max_bytes} bytes"
            )

    extracted = subprocess.run(
        [exe, "x", "-y", f"-o{dest}", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        check=False,
    )
    if extracted.returncode != 0:
        raise RuntimeError(
            f"7-Zip extraction failed ({extracted.returncode}): {extracted.stderr.strip()}"
        )

    root = dest.resolve()
    for item in dest.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"Extracted symbolic links are not accepted: {item}")
        try:
            item.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Extracted path escapes destination: {item}") from exc


def _extract_7z(path: Path, dest: Path, max_bytes: int) -> None:
    try:
        import py7zr  # type: ignore
    except ImportError:
        _extract_7z_cli(path, dest, max_bytes)
        return

    with py7zr.SevenZipFile(path, mode="r", max_extract_size=max_bytes) as z:
        infos = z.list()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"7z has too many members: {len(infos)}")
        total = 0
        for info in infos:
            _validate_member_name(info.filename, dest)
            if getattr(info, "is_symlink", False):
                raise ValueError(f"7z symbolic links are not accepted: {info.filename}")
            total += max(0, int(getattr(info, "uncompressed", 0) or 0))
            if total > max_bytes:
                raise ValueError(
                    f"7z uncompressed size exceeds safety limit: {total} > {max_bytes} bytes"
                )
        z.extractall(path=dest)


def extract_archive(path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    max_bytes = _max_extract_bytes()
    suffix = path.suffix.lower()
    if suffix == ".zip":
        _extract_zip(path, dest, max_bytes)
        return dest
    if suffix == ".7z":
        _extract_7z(path, dest, max_bytes)
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
    reference_text: str = ""
    reference_language: str = "auto"


def build_entries(
    full_index_csv: Path,
    bilingual_csv: Path,
    chs_lab_root: Path,
    wav_root: Path,
    same_group_gap: float = 0.40,
    group_gap: float = 1.20,
    intro_gap: float = 5.0,
    reference_lab_root: Path | None = None,
    reference_language: str = "auto",
    source_text_language: str = "en",
    target_language: str = "zh-CN",
) -> tuple[list[Entry], dict[str, object]]:
    full = read_csv_rows(full_index_csv)
    bi = read_csv_rows(bilingual_csv)
    if len(full) != len(bi):
        raise ValueError(f"Row count differs: full={len(full)}, bilingual={len(bi)}")

    bi_by_name = {r["文件名"]: r for r in bi}
    if len(bi_by_name) != len(bi):
        raise ValueError("Duplicate 文件名 in bilingual CSV")

    labs = collect_labs(chs_lab_root)
    reference_labs = collect_labs(reference_lab_root) if reference_lab_root else {}
    primary_labs = collect_labs(wav_root) if source_text_language != "en" else {}
    if source_text_language == target_language == "zh-CN":
        labs = {**primary_labs, **labs}
    wavs = collect_wavs(wav_root)
    official_labs, official_match = map_labs_to_voice_filenames(wavs.keys(), labs)

    sample_rate: int | None = None
    channels: int | None = None
    sample_width: int | None = None
    cursor = 0
    entries: list[Entry] = []
    mismatched_hashes: list[str] = []
    missing_wavs: list[str] = []
    official_count = 0
    translated_count = 0
    extensible_count = 0

    raw: list[dict[str, object]] = []
    for row in full:
        filename = row["文件名"]
        b = bi_by_name.get(filename)
        if not b:
            raise ValueError(f"Missing bilingual row: {filename}")
        wav = wavs.get(filename)
        if not wav:
            missing_wavs.append(filename)
            continue

        wav_info = parse_wav_pcm(wav)
        sr = wav_info.sample_rate
        ch = wav_info.channels
        sw = wav_info.sample_width_bytes
        frames = wav_info.frames
        if wav_info.extensible:
            extensible_count += 1

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
        if filename in official_labs:
            chinese = official_labs[filename]
            chinese_source = "official_chs_lab"
            official_count += 1
        else:
            chinese = b.get("中文", "").strip()
            chinese_source = "translated_existing" if chinese else "missing"
            if chinese:
                translated_count += 1

        source_text = str(row.get("英文文本", b.get("ENGLISH", "")) or "").strip()
        if source_text_language != "en":
            # A same-stem LAB in the primary package is the closest text to the
            # actual selected voice and therefore wins when present. Otherwise
            # use the selected CHS/JP/KR index text instead of requiring LAB.
            primary_lab_text = str(primary_labs.get(stem, "") or "").strip()
            if primary_lab_text:
                source_text = primary_lab_text
            if not source_text:
                raise ValueError(
                    f"Missing {source_text_language} source text for {filename}; "
                    "the selected index has no text and the primary package has no matching LAB"
                )

        raw.append(
            {
                "index": int(row["序号"]),
                "group": row["分组"],
                "filename": filename,
                "source": row.get("来源", ""),
                "source_detail": row.get("来源细分", ""),
                "english": source_text,
                "chinese": chinese,
                "chinese_source": chinese_source,
                "sample_rate": sr,
                "channels": ch,
                "sample_width_bits": sw * 8,
                "source_frames": frames,
                "source_duration_seconds": frames / sr,
                "sha256": got_hash,
                "reference_text": (
                    reference_labs.get(stem, "")
                    or str(row.get("参考文本", "") or "").strip()
                ),
                "reference_language": (
                    reference_language
                    if reference_labs.get(stem, "")
                    else str(row.get("参考语言", "") or reference_language or "auto")
                ),
            }
        )

    if missing_wavs:
        raise FileNotFoundError(f"Missing {len(missing_wavs)} WAVs, first: {missing_wavs[0]}")
    if mismatched_hashes:
        raise ValueError(f"SHA-256 mismatch for {len(mismatched_hashes)} WAVs, first: {mismatched_hashes[0]}")
    if sample_rate is None or channels is None or sample_width is None:
        raise ValueError("No usable WAV entries")

    resolved = resolve_timeline(
        raw,
        sample_rate,
        intro_gap=intro_gap,
        same_group_gap=same_group_gap,
        group_gap=group_gap,
    )

    for r, timing in zip(raw, resolved["entry_timings"], strict=True):
        start = int(timing["start_sample"])
        audio_end = int(timing["audio_end_sample"])
        next_start = int(timing["next_start_sample"])
        display_end = int(timing["display_end_sample"])
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
                reference_text=str(r.get("reference_text", "")),
                reference_language=str(r.get("reference_language", "auto") or "auto"),
            )
        )
    cursor = int(resolved["total_samples"])

    report = {
        "count_total": len(entries),
        "count_official_chs_lab": official_count,
        "count_official_chs_exact": official_match["exact"],
        "count_official_chs_structural": official_match["structural"],
        "count_official_chs_unmatched": official_match["unmatched"],
        "count_translated_existing": translated_count,
        "count_missing_chinese": sum(not e.chinese for e in entries),
        "count_official_target_lab": official_count,
        "count_existing_target_text": translated_count,
        "count_missing_target_text": sum(not e.chinese for e in entries),
        "count_reference_lab": sum(bool(e.reference_text) for e in entries),
        "count_source_lab": sum(
            bool(primary_labs.get(stem_of(e.filename))) for e in entries
        ) if source_text_language != "en" else 0,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bits": sample_width * 8,
        "intro_gap_seconds": float(resolved["intro_gap_seconds"]),
        "same_group_gap_seconds": float(resolved["same_group_gap_seconds"]),
        "group_gap_seconds": float(resolved["group_gap_seconds"]),
        "group_boundaries": sum(entries[i].group != entries[i + 1].group for i in range(len(entries) - 1)),
        "total_samples": cursor,
        "duration_continuous_seconds": cursor / sample_rate,
        "all_source_hashes_match": not mismatched_hashes,
        "count_wav_extensible": extensible_count,
    }
    return entries, report


def write_manifest(entries: list[Entry], report: dict[str, object], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    js = [asdict(e) for e in entries]
    for row in js:
        row["source_text"] = row.get("english", "")
        row["target_text"] = row.get("chinese", "")
        legacy_source = str(row.get("chinese_source", "") or "")
        row["target_text_source"] = (
            "official_target_lab"
            if legacy_source == "official_chs_lab"
            else legacy_source
        )
    atomic_write_text(
        out_dir / "manifest.json",
        json.dumps({"report": report, "entries": js}, ensure_ascii=False, indent=2),
    )
    fields = list(asdict(entries[0]).keys()) if entries else []
    write_csv_rows(out_dir / "manifest.csv", js, fields)

    # The legacy interim SRT is superseded by outputs rendered from the same
    # sample positions used by the FLAC builder.
    (out_dir / "bilingual.srt").unlink(missing_ok=True)
    write_resolved_timeline(entries, report, out_dir / "timeline_resolved.json")
    write_ass(
        entries,
        out_dir / "HSR_Voice_Archive.ass",
        source_language=str(report.get("source_text_language", "en")),
        target_language=str(report.get("target_language", "zh-CN")),
    )
    write_srt(
        entries,
        out_dir / "HSR_Voice_Archive.srt",
        source_language=str(report.get("source_text_language", "en")),
        target_language=str(report.get("target_language", "zh-CN")),
    )

    timeline_fields = [
        "index", "start", "audio_end", "display_end", "group", "filename",
        "target_text_source", "target_text", "source_text",
        "chinese_source", "chinese", "english",
        "reference_language", "reference_text",
        "source_duration_seconds", "sha256",
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
            "target_text_source": (
                "official_target_lab"
                if e.chinese_source == "official_chs_lab"
                else e.chinese_source
            ),
            "target_text": e.chinese,
            "source_text": e.english,
            "chinese_source": e.chinese_source,
            "chinese": e.chinese,
            "english": e.english,
            "reference_language": e.reference_language,
            "reference_text": e.reference_text,
            "source_duration_seconds": f"{e.source_duration_seconds:.6f}",
            "sha256": e.sha256,
        })
    write_csv_rows(out_dir / "bilingual_index_corrected.csv", timeline_rows, timeline_fields)
    atomic_write_text(
        out_dir / "build_report.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )


def _pcm_format(sample_width_bytes: int) -> tuple[str, str]:
    formats = {
        1: ("u8", "pcm_u8"),
        2: ("s16le", "pcm_s16le"),
        3: ("s24le", "pcm_s24le"),
        4: ("s32le", "pcm_s32le"),
    }
    if sample_width_bytes not in formats:
        raise RuntimeError(f"Unsupported PCM sample width: {sample_width_bytes} bytes")
    return formats[sample_width_bytes]


def build_continuous_flac(
    entries: list[Entry],
    wav_root: Path,
    out_flac: Path,
    compression_level: int = 8,
) -> dict[str, object]:
    """Stream raw PCM directly into FFmpeg.

    This avoids a temporary RIFF/WAV intermediate, so the builder does not hit
    the classic ~4 GiB RIFF size ceiling and does not need a second full-size
    uncompressed copy on disk.
    """
    wavs = collect_wavs(wav_root)
    if not entries:
        raise ValueError("No entries")

    sr = entries[0].sample_rate
    ch = entries[0].channels
    sw = entries[0].sample_width_bits // 8
    pcm_format, pcm_codec = _pcm_format(sw)
    silence_frame = b"\x00" * (ch * sw)

    out_flac.parent.mkdir(parents=True, exist_ok=True)
    partial = out_flac.with_name(out_flac.stem + ".partial.flac")
    partial.unlink(missing_ok=True)
    pcm_hash = hashlib.sha256()
    written_frames = 0

    with tempfile.TemporaryFile() as stderr_log:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", pcm_format,
            "-ar", str(sr),
            "-ac", str(ch),
            "-i", "pipe:0",
            "-c:a", "flac",
            "-compression_level", str(compression_level),
            str(partial),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=stderr_log)
        if proc.stdin is None:
            raise RuntimeError("Unable to open FFmpeg stdin")

        try:
            initial_silence = entries[0].start_sample
            remaining = initial_silence
            block_frames = 65536
            block = silence_frame * block_frames
            while remaining > 0:
                n = min(block_frames, remaining)
                data = block if n == block_frames else silence_frame * n
                proc.stdin.write(data)
                pcm_hash.update(data)
                written_frames += n
                remaining -= n

            for i, e in enumerate(entries):
                src = wavs[e.filename]
                info = parse_wav_pcm(src)
                actual = (
                    info.sample_rate,
                    info.channels,
                    info.bits_per_sample,
                    info.frames,
                )
                expected = (
                    e.sample_rate,
                    e.channels,
                    e.sample_width_bits,
                    e.source_frames,
                )
                if actual != expected:
                    raise RuntimeError(
                        f"WAV changed or no longer matches manifest at {e.filename}: "
                        f"{actual} != {expected}"
                    )
                for frames in iter_pcm_chunks(src, info):
                    proc.stdin.write(frames)
                    pcm_hash.update(frames)
                    written_frames += len(frames) // (ch * sw)

                if i + 1 < len(entries):
                    gap_frames = e.next_start_sample - e.audio_end_sample
                    remaining = gap_frames
                    block_frames = 65536
                    block = silence_frame * block_frames
                    while remaining > 0:
                        n = min(block_frames, remaining)
                        data = block if n == block_frames else silence_frame * n
                        proc.stdin.write(data)
                        pcm_hash.update(data)
                        written_frames += n
                        remaining -= n

            proc.stdin.close()
            rc = proc.wait()
        except BaseException:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.kill()
            proc.wait()
            partial.unlink(missing_ok=True)
            raise

        if rc != 0:
            stderr_log.seek(0)
            detail = stderr_log.read().decode("utf-8", "replace").strip()
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"FFmpeg FLAC encode failed ({rc}): {detail}")

    expected_frames = entries[-1].next_start_sample
    if written_frames != expected_frames:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"PCM frame count mismatch before verification: {written_frames} != {expected_frames}"
        )

    decoded_hash = hashlib.sha256()
    with tempfile.TemporaryFile() as decode_err:
        decoded = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(partial),
                "-f", pcm_format, "-acodec", pcm_codec, "-",
            ],
            stdout=subprocess.PIPE,
            stderr=decode_err,
        )
        if decoded.stdout is None:
            partial.unlink(missing_ok=True)
            raise RuntimeError("Unable to open FFmpeg verification stream")
        while True:
            chunk = decoded.stdout.read(1024 * 1024)
            if not chunk:
                break
            decoded_hash.update(chunk)
        rc = decoded.wait()
        if rc != 0:
            decode_err.seek(0)
            detail = decode_err.read().decode("utf-8", "replace").strip()
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"FFmpeg decode verification failed ({rc}): {detail}")

    source_pcm_hash = pcm_hash.hexdigest()
    decoded_pcm_hash = decoded_hash.hexdigest()
    if source_pcm_hash != decoded_pcm_hash:
        partial.unlink(missing_ok=True)
        raise RuntimeError("FLAC decoded PCM does not match assembled source PCM")

    os.replace(partial, out_flac)
    return {
        "flac_path": str(out_flac),
        "flac_size_bytes": out_flac.stat().st_size,
        "assembled_pcm_sha256": source_pcm_hash,
        "decoded_flac_pcm_sha256": decoded_pcm_hash,
        "lossless_pcm_verified": True,
        "pcm_streamed_directly": True,
        "pcm_frames_written": written_frames,
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
            full_index_csv,
            bilingual_csv,
            chs_root,
            wav_root,
            same_group_gap=same_group_gap,
            group_gap=group_gap,
        )
        write_manifest(entries, report, out_dir)
        if make_flac:
            report.update(build_continuous_flac(entries, wav_root, out_dir / "continuous.flac"))
            write_manifest(entries, report, out_dir)
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
        a.index,
        a.bilingual,
        a.chs,
        a.wavs,
        a.out,
        same_group_gap=a.same_gap,
        group_gap=a.group_gap,
        make_flac=not a.no_flac,
    ), ensure_ascii=False, indent=2))
