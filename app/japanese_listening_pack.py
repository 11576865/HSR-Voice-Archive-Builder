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
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .japanese_source_recovery import (
    verify_japanese_recovery_build_inputs,
    verify_japanese_recovery_build_manifest,
)
from .project import ProjectConfig, resolve_project_path
from .builder import atomic_write_text
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
         "stream=index,codec_type,codec_name:stream_tags=title:stream_disposition=default",
         "-of", "json", str(target)],
        capture_output=True, text=True, timeout=90, check=True,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    flac = [
        stream for stream in streams
        if stream.get("codec_type") == "audio"
        and stream.get("codec_name") == "flac"
    ]
    if len(flac) == 1:
        return "0:" + str(int(flac[0]["index"]))
    if wanted != "single-flac" or not flac:
        raise ValueError("需要明确的成品连续 FLAC 音轨，不能猜测多个音频轨道")
    # The certified MKV exporter maps the continuous stream as the first
    # audio track with explicit default disposition and title; chapter FLACs
    # follow it. Refuse if either the ordering or metadata was lost.
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    named = [
        stream for stream in flac
        if (stream.get("tags") or {}).get("title") == "HSR continuous voice"
        and int((stream.get("disposition") or {}).get("default") or 0) == 1
    ]
    if (
        len(named) != 1 or not audios
        or int(named[0]["index"]) != int(audios[0]["index"])
    ):
        raise ValueError("MKV 包含多条 FLAC，但无法唯一确认默认的 HSR continuous voice 主音轨")
    return "0:" + str(int(named[0]["index"]))


def _clip(source: Path, dest: Path, duration: float, *, start: float = 0,
          stream: str = "0:a:0") -> None:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.6f}", "-i", str(source),
           "-map", stream, "-t", f"{duration:.6f}", "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    subprocess.run(cmd, capture_output=True, timeout=180, check=True)
    if not dest.is_file() or dest.stat().st_size < 128:
        raise RuntimeError("试听片段解码为空或无效：" + dest.name)


def _review_html(rows: list[dict[str, Any]], pack_id: str, evidence_sha256: str) -> str:
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
<p><label><input id="human-confirm" type="checkbox">我确认以上判断基于亲自试听原始音频与成品音频，而非自动推断。</label></p>
<p><button id="save">导出人工试听记录 JSON</button></p><p id="result"></p>
<script>
const PACK_ID=""" + json.dumps(pack_id) + """;
const EVIDENCE_HASH=""" + json.dumps(evidence_sha256) + """;
document.getElementById('save').onclick=()=>{
 const results=[...document.querySelectorAll('section[data-id]')].map(s=>({
   id:s.dataset.id,language:s.querySelector('.language').value,
   text_match:s.querySelector('.text').value,
   rendered_match:s.querySelector('.render').value
 }));
 const completed=results.every(r=>r.language&&r.text_match&&r.rendered_match);
 const payload={schema_version:2,kind:'manual-japanese-recovery-listening-review',
  pack_id:PACK_ID,evidence_sha256:EVIDENCE_HASH,
  reviewer_confirmed:document.getElementById('human-confirm').checked,
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
    elif source.suffix.lower() == ".7z":
        from .quick import _list_7z_members
        records, _reader = _list_7z_members(source)
        found = {str(row["name"]): int(row["size"]) for row in records}
        if any(member not in found for member in members):
            raise ValueError("7z 压缩包缺少待核验来源成员")
        if sum(found[member] for member in members) > MAX_EXTRACT_BYTES:
            raise ValueError("试听抽样 7z 解压总大小超过安全上限")


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
            "schema_version": 2,
            "kind": "japanese-recovery-manual-audition",
            "pack_id": pack_id,
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
        (stage / "review.html").write_text(
            _review_html(catalog, pack_id, _hash(stage / "evidence.json")), "utf-8",
        )
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
    # A ZIP exported for an earlier build must not be presented as current
    # evidence after a user edits the manifest, final file, or original source.
    verify_japanese_recovery_build_inputs(config)
    manifest = output / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("试听包对应的构建 Manifest 已不存在")
    with zipfile.ZipFile(result) as pack:
        evidence = json.loads(pack.read("evidence.json"))
    if (
        evidence.get("kind") != "japanese-recovery-manual-audition"
        or evidence.get("build_manifest_sha256") != _hash(manifest)
        or evidence.get("original_source_fingerprint") != str(config.wav_source_fingerprint)
    ):
        raise ValueError("试听包的原始来源或构建清单已变化，请重新导出")
    final, _map = _media(config, output)
    state = final.stat()
    if (
        final.name != evidence.get("final_artifact")
        or state.st_size != evidence.get("final_artifact_size")
        or state.st_mtime_ns != evidence.get("final_artifact_mtime_ns")
    ):
        raise ValueError("成品音频自试听包生成后已变化，请重新导出")
    return result



def import_japanese_listening_review(
    config: ProjectConfig,
    review_text: str,
) -> dict[str, Any]:
    """Import one explicit user assessment as append-only SAMPLE evidence.

    A downloaded JSON is not a cryptographic proof of who actually listened:
    the app records it as user-reported, never as an automatic language verdict.
    The result must be for the active unchanged source/manifest/final artifact.
    """
    if not isinstance(review_text, str) or len(review_text.encode("utf-8")) > 64 * 1024:
        raise ValueError("人工试听记录必须为不超过 64 KiB 的 JSON 文本")
    try:
        payload = json.loads(review_text)
    except (ValueError, TypeError) as exc:
        raise ValueError("人工试听记录不是合法 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 2 or payload.get("kind") != "manual-japanese-recovery-listening-review":
        raise ValueError("人工试听记录格式不支持；请从最新试听包 review.html 重新导出")
    pack_id = payload.get("pack_id")
    expected_evidence_sha256 = payload.get("evidence_sha256")
    if not isinstance(pack_id, str) or not _PACK_ID.fullmatch(pack_id):
        raise ValueError("试听记录缺少有效试听包 ID")
    if not isinstance(expected_evidence_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_evidence_sha256):
        raise ValueError("试听记录未绑定有效的证据摘要")
    pack_path = get_japanese_listening_pack(config, pack_id)
    with zipfile.ZipFile(pack_path) as z:
        raw_evidence = z.read("evidence.json")
    if hashlib.sha256(raw_evidence).hexdigest() != expected_evidence_sha256:
        raise ValueError("试听记录与原始/成品对照包的证据摘要不符")
    evidence = json.loads(raw_evidence)
    if evidence.get("schema_version") != 2 or evidence.get("pack_id") != pack_id:
        raise ValueError("试听包不是可接受的带身份验收证据")
    rows = evidence.get("rows")
    if not isinstance(rows, list) or not 2 <= len(rows) <= MAX_SAMPLES:
        raise ValueError("试听证据中没有可验收的有效样本")
    expected_ids = [row.get("id") for row in rows if isinstance(row, dict)]
    if len(expected_ids) != len(rows) or len(set(expected_ids)) != len(rows):
        raise ValueError("试听包的样本身份不唯一")
    answers = payload.get("results")
    if not isinstance(answers, list) or len(answers) != len(expected_ids):
        raise ValueError("人工试听记录的样本数与原始证据不匹配")
    values = {"", "yes", "no", "unclear"}
    answer_by_id: dict[str, dict[str, str]] = {}
    for item in answers:
        if not isinstance(item, dict) or type(item.get("id")) is not str:
            raise ValueError("人工试听结果中存在无效样本")
        ident = item["id"]
        if ident in answer_by_id or ident not in expected_ids:
            raise ValueError("人工试听记录含重复或陌生的样本 ID")
        for field in ("language", "text_match", "rendered_match"):
            if type(item.get(field)) is not str or item[field] not in values:
                raise ValueError(f"人工试听结果字段无效：{field}")
        answer_by_id[ident] = {name: item[name] for name in ("language", "text_match", "rendered_match")}
    if set(answer_by_id) != set(expected_ids):
        raise ValueError("人工试听样本集合与原始试听包不一致")

    # Recalculate rather than trust the easily edited 'completed' flag.
    confirmed = payload.get("reviewer_confirmed") is True
    total = len(expected_ids)
    failed = sum("no" in answer_by_id[key].values() for key in expected_ids)
    unclear = sum(
        "no" not in answer_by_id[key].values() and "unclear" in answer_by_id[key].values()
        for key in expected_ids
    )
    missing = sum(
        all(answer_by_id[key][column] != "no" for column in ("language", "text_match", "rendered_match"))
        and "unclear" not in answer_by_id[key].values()
        and "" in answer_by_id[key].values()
        for key in expected_ids
    )
    passed = sum(all(value == "yes" for value in answer_by_id[key].values()) for key in expected_ids)
    if failed:
        status = "sample-failed"
    elif unclear:
        status = "sample-inconclusive"
    elif missing or not confirmed:
        status = "sample-incomplete"
    else:
        status = "sample-user-reported-pass"

    record = {
        "schema_version": 1,
        "kind": "japanese-recovery-human-audition-receipt",
        "pack_id": pack_id,
        "evidence_sha256": expected_evidence_sha256,
        "original_source_fingerprint": evidence["original_source_fingerprint"],
        "build_manifest_sha256": evidence["build_manifest_sha256"],
        "pack_sha256": _hash(pack_path),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reviewer_confirmed": confirmed,
        "summary": {
            "sample_count": total,
            "passed": passed,
            "failed": failed,
            "inconclusive": unclear,
            "unreviewed": missing,
        },
        "results": [
            {"id": key,
             "source_member_id": next(row["source_member_id"] for row in rows if row["id"] == key),
             **answer_by_id[key]}
            for key in expected_ids
        ],
        "entire_archive_verified": False,
        "spoken_japanese_automatically_verified": False,
        "assessment_source": "user-reported-manual-listening",
    }
    # Keep every submission immutable rather than silently overwriting a
    # previous failure; later corrections can be separately audited.
    get_japanese_listening_pack(config, pack_id)
    output = resolve_project_path(config, config.output_dir)
    assert output is not None
    receipt_dir = output / "recovery_listening_reviews" / pack_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_id = uuid.uuid4().hex
    path = receipt_dir / (receipt_id + ".json")
    atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2))
    return {
        "pack_id": pack_id,
        "receipt_id": receipt_id,
        "status": status,
        "summary": record["summary"],
        "entire_archive_verified": False,
        "spoken_japanese_automatically_verified": False,
        "reviewer_confirmed": confirmed,
        "receipt_path": str(path),
    }


def japanese_listening_review_history(config: ProjectConfig, pack_id: str) -> dict[str, Any]:
    """Read append-only reviewer submissions, but only for a current pack."""
    get_japanese_listening_pack(config, pack_id)
    output = resolve_project_path(config, config.output_dir)
    assert output is not None
    folder = output / "recovery_listening_reviews" / pack_id
    records: list[dict[str, Any]] = []
    if folder.is_dir():
        for path in folder.glob("*.json"):
            if path.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("kind") == "japanese-recovery-human-audition-receipt" and data.get("pack_id") == pack_id:
                records.append({
                    "receipt_id": path.stem,
                    "status": data["status"],
                    "recorded_at": data["recorded_at"],
                    "summary": data["summary"],
                    "reviewer_confirmed": data["reviewer_confirmed"],
                })
    records.sort(key=lambda x: (x["recorded_at"], x["receipt_id"]), reverse=True)
    return {"pack_id": pack_id, "total_submissions": len(records),
            "latest": records[0] if records else None, "history": records[:30],
            "entire_archive_verified": False}
