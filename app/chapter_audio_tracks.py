"""Validate generated chapter FLAC collection before embedding into an MKV.

The builder's manifest, rather than filesystem glob order, is authoritative.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


MAX_CHAPTER_AUDIO_TRACKS = 128


def chapter_audio_tracks(output: Path) -> list[tuple[Path, str]]:
    directory = output / "chapter_flac"
    manifest = directory / "chapter_flac_manifest.json"
    if directory.is_symlink() or manifest.is_symlink() or not manifest.is_file():
        raise ValueError("章节 FLAC 已启用，但缺少安全有效的章节清单；请重新构建章节音频")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    rows = data.get("outputs") if isinstance(data, dict) and data.get("schema_version") == 1 else None
    if not isinstance(rows, list) or len(rows) > MAX_CHAPTER_AUDIO_TRACKS or data.get("chapter_count") != len(rows):
        raise ValueError("章节 FLAC 清单格式错误或音轨数量超出限制")
    tracks: list[tuple[Path, str]] = []
    names: set[str] = set()
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or row.get("ordinal") != position:
            raise ValueError("章节音轨的顺序不符合构建清单")
        claimed = str(row.get("path") or "")
        name = Path(claimed).name
        if not name or name in names or not name.endswith(".flac") or name in {".", ".."}:
            raise ValueError("章节音轨文件名无效或重复")
        track = directory / name
        if track.is_symlink() or not track.is_file() or track.stat().st_size <= 0:
            raise ValueError("章节音轨缺失或不可安全读取：" + name)
        digest = str(row.get("file_sha256") or "")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("章节 FLAC 清单缺少有效音频校验值")
        hasher = hashlib.sha256()
        with track.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise ValueError("章节音轨与清单校验不一致：" + name)
        names.add(name)
        tracks.append((track, str(row.get("group_id") or name)))
    return tracks
