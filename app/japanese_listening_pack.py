"""Offline human-audition pack for an attested isolated Japanese recovery.

Each item pairs a short ORIGINAL WAV excerpt with the matching time position
in the BUILT FLAC/MKV. This cannot recognize Japanese or self-certify speech.
Only the human reviewer can make that decision.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .japanese_source_recovery import (
    verify_japanese_recovery_build_inputs,
    verify_japanese_recovery_build_manifest,
)
from .project import ProjectConfig, resolve_project_path
from .quick import _extract_source_members

PACK_DIR = "recovery_listening_packs"
MAX_SAMPLES = 12
MAX_CLIP_SECONDS = 9.0
MAX_EXTRACT_BYTES = 192 * 1024 * 1024
_PACK_ID = re.compile(r"[0-9a-f]{20}\Z")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _select(entries: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    """Deterministically prioritize same-basename ambiguity and chapter coverage."""
    counts = Counter(str(row["filename"]).casefold() for row in entries)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        key = str(row["source_member_id"])
        if key not in seen and len(selected) < maximum:
            selected.append(row)
            seen.add(key)

    # Inspect up to two independent members from the first ambiguous
    # basename groups: they are the highest-risk cases for old Termux merges.
    duplicates: dict[str, list[dict[str, Any]]] = {}
    for row in entries:
        name = str(row["filename"]).casefold()
        if counts[name] > 1:
            duplicates.setdefault(name, []).append(row)
    for name in sorted(duplicates):
        for row in duplicates[name][:2]:
            add(row)
    # Cover as many distinct chapter groups as fit the budget.
    covered = {str(row.get("group") or "") for row in selected}
    for row in entries:
        group = str(row.get("group") or "")
        if group not in covered:
            add(row)
            covered.add(group)
    # Spread remaining review slots across the whole manifest.
    if len(selected) < maximum:
        for i in range(maximum * 3):
            index = min(len(entries) - 1, (i * len(entries)) // (maximum * 3))
            add(entries[index])
    for row in entries:
        add(row)
    return selected


def _media(config: ProjectConfig, output: Path) -> tuple[Path, str]:
    """Require a usable final FLAC; for MKV require exactly one FLAC track."""
    if config.delivery_mode == "flac_only":
        target = output / "continuous.flac"
        if not target.is_file() or target.is_symlink():
            raise FileNotFoundError("请先构建 FLAC 成品，再导出试听包")
        return target, "0:a:0"
    from .archive_bundle_lifecycle import bundle_export_status
    health = bundle_export_status(output)
    if health.get("state") != "current":
        raise ValueError("最终 MKV 尚未通过当前成品验收，拒绝生成误导性的试听包")
    target = output / "HSR_Voice_Archive_Bundle.mkv"
    if not target.is_file() or target.is_symlink():
        raise FileNotFoundError("当前 MKV 成品缺失")
    return target, "single-flac"


def _flac_stream(target: Path, wanted: str) -> str:
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        raise RuntimeError("需要本地 FFmpeg 和 FFprobe 才能从原始与最终音轨生成试听切片")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name", "-of", "json", str(target)],
        capture_output=True, text=True, timeout=90, check=True,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    flac = [
        stream for stream in streams
        if stream.get("codec_type") == "audio"
        and stream.get("codec_name") == "flac"
    ]
    if len(flac) != 1:
        raise ValueError("试听包需要唯一的 FLAC 成品音轨，不能猜测多个音频轨道")
    return "0:" + str(int(flac[0]["index"]))


def _clip(source: Path, dest: Path, duration: float, *, start: float = 0,
          stream: str = "0:a:0") -> None:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.6f}", "-i", str(source),
           "-map", stream, "-t", f"{duration:.6f}", "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    subprocess.run(cmd, capture_output=True, timeout=180, check=True)
    if not dest.is_file() or dest.stat().st_size < 128:
        raise RuntimeError("试听片段解码为空或无效：" + dest.name)


def _review_html(rows: list[dict[str, Any]]) -> str:
    items: list[str] = []
    for row in rows:
        code = str(row["id"])
        original = html.escape(str(row["original_clip"]), quote=True)
        rendered = html.escape(str(row["rendered_clip"]), quote=True)
        member = html.escape(str(row["source_member_id"]))
        source_text = html.escape(str(row["source_text"]))
        target_text = html.escape(str(row["target_text"]))
        items.append(
            f'<section data-id="{code}"><h2>{code} · {member}</h2>'
            f'<p><b>原文：</b>{source_text}</p><p><b>译文：</b>{target_text}</p>'
            f'<p>原始 WAV <audio controls preload="none" src="{original}"></audio></p>'
            f'<p>最终成品 <audio controls preload="none" src="{rendered}"></audio></p>'
            '<label>日语发音 <select class="language"><option value="">未检查</option>'
            '<option value="yes">确认日语</option><option value="no">非日语</option>'
            '<option value="unclear">无法判断</option></select></label>'
            '<label>原文与发音 <select class="text"><option value="">未检查</option>'
            '<option value="yes">一致</option><option value="no">不一致</option>'
            '<option value="unclear">无法判断</option></select></label>'
            '<label>原始与成品 <select class="render"><option value="">未检查</option>'
            '<option value="yes">听感一致</option><option value="no">不一致</option>'
            '<option value="unclear">无法判断</option></select></label></section>'
        )
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>日语语音恢复 · 人工试听验收</title>
<style>body{max-width:900px;margin:2rem auto;padding:0 1rem;font:16px/1.6 system-ui;color:#182334;background:#f3f6fb}
section{background:white;padding:1rem;margin:1rem 0;border:1px solid #d4dce6;border-radius:10px}
audio{width:100%;max-width:540px;vertical-align:middle}
label{display:inline-block;margin:.4rem .8rem .4rem 0}select,button{padding:.45rem}</style>
<h1>日语语音恢复 · 人工试听验收</h1>
<p>逐项试听原始 WAV 与最终 FLAC/MKV 对应音频，核查原文与发音。自动化只能保证来源身份和时间定位；
<strong>勾选项属于你的人工判断</strong>，不是自动语言识别。抽检通过不证明整个归档没有问题。</p>
""" + "\n".join(items) + """
<p><button id="save">导出人工试听记录 JSON</button></p><p id="result"></p>
<script>
document.getElementById('save').onclick=()=>{
 const results=[...document.querySelectorAll('section[data-id]')].map(s=>({
   id:s.dataset.id,language:s.querySelector('.language').value,
   text_match:s.querySelector('.text').value,
   rendered_match:s.querySelector('.render').value
 }));
 const completed=results.every(r=>r.language&&r.text_match&&r.rendered_match);
 const payload={schema_version:1,kind:'manual-japanese-recovery-listening-review',
  completed,generated_from:'original-and-final-clips',results};
 const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);
 a.download='manual_listening_review.json';a.click();URL.revokeObjectURL(a.href);
 document.getElementById('result').textContent=completed
 ?'已导出完整人工记录；请保存并复核所有未通过项。'
 :'已导出未完成记录；仍有未选择的检查项。';
};
</script></html>"""


def _source_size_limit(source: Path, members: list[str]) -> None:
    if source.is_dir():
        total = 0
        for member in members:
            file = source / member
            if file.is_symlink() or not file.is_file():
                raise ValueError("原始采样成员缺失或为符号链接：" + member)
            total += file.stat().st_size
        if total > MAX_EXTRACT_BYTES:
            raise ValueError("试听抽样原始 WAV 总大小超过安全上限")
    elif source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as z:
            found = {info.filename.replace("\\", "/"): info for info in z.infolist()
                     if not info.is_dir()}
        if any(member not in found for member in members):
            raise ValueError("压缩包缺少待核验来源成员")
        if sum(found[member].file_size for member in members) > MAX_EXTRACT_BYTES:
            raise ValueError("试听抽样 ZIP 解压总大小超过安全上限")


def build_japanese_listening_pack(
    config: ProjectConfig,
    *, sample_count: int = 8,
    report_progress: Callable[[str, str, int, int], None] | None = None,
) -> dict[str, Any]:
    if not 2 <= sample_count <= MAX_SAMPLES:
        raise ValueError("试听样本数必须为 2 至 12")
    proof = verify_japanese_recovery_build_inputs(config)
    if proof is None:
        raise ValueError("仅支持具备原始包来源证明的独立日语恢复候选工程")
    verified = verify_japanese_recovery_build_manifest(config, proof)
    if verified is None:
        raise ValueError("日语恢复候选尚未完成来源成员核验")
    output = resolve_project_path(config, config.output_dir)
    assert output is not None
    manifest = output / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    rows = payload["entries"]
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Manifest contains malformed entries")
        start, end = float(row.get("start_seconds", -1)), float(row.get("audio_end_seconds", -1))
        if not (0 <= start < end and end-start >= 0.25):
            continue
        candidates.append(row)
    if len(candidates) < 2:
        raise ValueError("有效音频条目不足两条，无法生成具有对照意义的试听包")
    chosen = _select(candidates, min(sample_count, len(candidates)))
    source = Path(proof["source"])
    members = [str(row["source_member_id"]) for row in chosen]
    _source_size_limit(source, members)
    final, expected_stream = _media(config, output)
    stream = _flac_stream(final, expected_stream)
    manifest_sha = _hash(manifest)
    media_stat = final.stat()
    out_dir = output / PACK_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    pack_id = uuid.uuid4().hex[:20]
    archive_target = out_dir / (pack_id + ".zip")

    with tempfile.TemporaryDirectory(prefix=".jp-listening-", dir=out_dir) as temporary:
        stage = Path(temporary)
        original = _extract_source_members(source, members, stage / "extracted")
        catalog: list[dict[str, Any]] = []
        for i, row in enumerate(chosen, 1):
            member = str(row["source_member_id"])
            input_file = original.get(member)
            if input_file is None or input_file.is_symlink() or not input_file.is_file():
                raise ValueError("原始日语抽样文件无法提取：" + member)
            seconds = min(MAX_CLIP_SECONDS, float(row["audio_end_seconds"])-float(row["start_seconds"]))
            folder = stage / "samples" / f"{i:03d}"
            folder.mkdir(parents=True)
            source_out, final_out = folder / "original.wav", folder / "final.wav"
            _clip(input_file, source_out, seconds)
            _clip(final, final_out, seconds, start=float(row["start_seconds"]), stream=stream)
            catalog.append({
                "id": f"{i:03d}",
                "source_member_id": member,
                "filename": row["filename"],
                "group": row.get("group", ""),
                "source_text": row.get("source_text", row.get("english", "")),
                "target_text": row.get("target_text", row.get("chinese", "")),
                "start_seconds": row["start_seconds"],
                "sample_duration_seconds": round(seconds, 3),
                "original_clip": f"samples/{i:03d}/original.wav",
                "rendered_clip": f"samples/{i:03d}/final.wav",
                "original_clip_sha256": _hash(source_out),
                "rendered_clip_sha256": _hash(final_out),
            })
            if report_progress:
                report_progress("audition", f"正在生成日语原始/成品试听对照 {i}/{len(chosen)}", i, len(chosen))
        evidence = {
            "schema_version": 1,
            "kind": "japanese-recovery-manual-audition",
            "original_source_fingerprint": proof["source_fingerprint"],
            "build_manifest_sha256": manifest_sha,
            "final_artifact": final.name,
            "final_artifact_size": media_stat.st_size,
            "final_artifact_mtime_ns": media_stat.st_mtime_ns,
            "selected": len(catalog),
            "original_source_verified": True,
            "manifest_member_identity_verified": True,
            "spoken_japanese_verified": False,
            "human_review_complete": False,
            "sampling_method": "same-basename-pairs, chapters, distributed-index",
            "rows": catalog,
        }
        (stage / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), "utf-8")
        (stage / "review.html").write_text(_review_html(catalog), "utf-8")
        (stage / "README.txt").write_text(
            "解压此 ZIP 后打开 review.html，试听每项原始与最终成品切片。"
            "检查是否为日语、原文是否对应、成品音频是否一致，再导出人工记录。"
            "此工具不自动判断声学语言，抽检不代替整个归档验收。\n", "utf-8")
        # Ensure the original has not been replaced while clipping samples.
        verify_japanese_recovery_build_inputs(config)
        if _hash(manifest) != manifest_sha:
            raise ValueError("生成试听包时 Manifest 发生变化，拒绝发布")
        after = final.stat()
        if after.st_size != media_stat.st_size or after.st_mtime_ns != media_stat.st_mtime_ns:
            raise ValueError("生成试听包时最终媒体发生变化，拒绝发布")
        temp_zip = stage / "review.zip"
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for entry in sorted(stage.rglob("*")):
                if entry.is_file() and entry != temp_zip and not entry.is_relative_to(stage / "extracted"):
                    bundle.write(entry, entry.relative_to(stage).as_posix())
        os.replace(temp_zip, archive_target)
    return {"pack_id": pack_id, "pack_path": str(archive_target),
            "selected": len(catalog), "manifest_sha256": manifest_sha,
            "human_listening_required": True, "spoken_japanese_verified": False,
            "download_url": "/api/recovery/listening-pack/" + pack_id}


def get_japanese_listening_pack(config: ProjectConfig, pack_id: str) -> Path:
    if not _PACK_ID.fullmatch(pack_id):
        raise ValueError("Invalid listening pack identifier")
    output = resolve_project_path(config, config.output_dir)
    if output is None or not config.recovery_source_attestation_required:
        raise ValueError("No verified Japanese recovery candidate is active")
    result = output / PACK_DIR / (pack_id + ".zip")
    if result.is_symlink() or not result.is_file():
        raise FileNotFoundError("试听包不存在")
    return result
