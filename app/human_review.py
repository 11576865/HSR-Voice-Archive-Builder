from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .builder import atomic_write_text
from .semantic_quality import SEMANTIC_QA_VERSION, summarize_semantic_qa
from .translation_quality import has_hard_issue, summarize_qa, translation_qa

ENTRY_RE = re.compile(
    r"\[\[ENTRY\]\]\s*\n"
    r"ID:\s*(?P<id>[^\r\n]+)\s*\n"
    r"SOURCE_SHA256:\s*(?P<sha>[0-9a-f]{64})"
    r"(?P<body>.*?)"
    r"\[\[CORRECTION\]\]\s*\n(?P<correction>.*?)\n\[\[/CORRECTION\]\]"
    r".*?\[\[/ENTRY\]\]",
    re.DOTALL,
)
PLACEHOLDER = "请删除这一行，并在这里填写修订后的中文字幕。"


class HumanReviewRequired(RuntimeError):
    job_state = "awaiting_input"

    def __init__(self, path: Path, count: int):
        self.path = path
        self.count = int(count)
        self.job_result = {"review_file": str(path), "review_count": self.count}
        super().__init__(f"{self.count} 条译文需要人工修订：{path}")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_review_txt(path: Path, records: list[dict[str, Any]]) -> Path:
    lines = [
        "HSR Voice Archive Builder · 语义偏离人工修订文件",
        "格式版本：1",
        f"待修订条目：{len(records)}",
        "说明：只修改每个 [[CORRECTION]] 与 [[/CORRECTION]] 之间的内容；不要修改 ID。",
        "",
    ]
    for row in records:
        english = str(row.get("english", ""))
        issues = "；".join(str(x) for x in row.get("final_issues") or row.get("issues") or [])
        note = str(row.get("final_note") or row.get("note") or "语义复核仍未通过").strip()
        lines.extend([
            "============================================================",
            "[[ENTRY]]",
            f"ID: {row.get('id', '')}",
            f"SOURCE_SHA256: {_sha(english)}",
            "问题类型：" + (issues or "语义偏离"),
            "检查说明：" + note,
            "",
            "英文原文：",
            english,
            "",
            "当前中文：",
            str(row.get("chinese", "")),
            "",
            "[[CORRECTION]]",
            PLACEHOLDER,
            "[[/CORRECTION]]",
            "[[/ENTRY]]",
            "",
        ])
    atomic_write_text(path, "\ufeff" + "\n".join(lines))
    return path


def import_review_txt(
    text: str,
    *,
    state_dir: Path,
    output_path: Path,
    target_language: str,
) -> dict[str, Any]:
    semantic_path = state_dir / "semantic_qa.json"
    checkpoint_path = state_dir / ".translation_checkpoint.json"
    translation_qa_path = state_dir / "translation_qa.json"
    if not semantic_path.is_file() or not checkpoint_path.is_file():
        raise FileNotFoundError("当前项目没有等待人工修订的语义检查状态")
    semantic = json.loads(semantic_path.read_text(encoding="utf-8-sig"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
    qa_payload = (
        json.loads(translation_qa_path.read_text(encoding="utf-8-sig"))
        if translation_qa_path.is_file() else {"schema_version": 1, "records": []}
    )
    pending = {
        str(row.get("id")): row
        for row in semantic.get("records", [])
        if isinstance(row, dict) and row.get("hard_failed")
    }
    if not pending:
        raise ValueError("当前项目没有尚未解决的语义偏离条目")
    submitted: dict[str, tuple[str, str]] = {}
    for match in ENTRY_RE.finditer(text.replace("\r\n", "\n")):
        row_id = match.group("id").strip()
        correction = match.group("correction").strip().lstrip("\ufeff")
        if correction and correction != PLACEHOLDER:
            submitted[row_id] = (match.group("sha"), correction)
    if not submitted:
        raise ValueError("没有读取到任何已填写的用户修订")

    records = checkpoint.get("records")
    if not isinstance(records, dict):
        raise ValueError("翻译检查点格式损坏")
    qa_by_id = {
        str(row.get("id")): row
        for row in qa_payload.get("records", [])
        if isinstance(row, dict)
    }
    imported = []
    for row_id, (source_sha, correction) in submitted.items():
        if row_id not in pending:
            raise ValueError(f"条目不属于当前待修订清单：{row_id}")
        source = pending[row_id]
        english = str(source.get("english", ""))
        if source_sha != _sha(english):
            raise ValueError(f"原文指纹不匹配，可能提交了旧版修订文件：{row_id}")
        issues = translation_qa(english, correction, {}, target_language)
        if has_hard_issue(issues):
            raise ValueError(f"人工修订仍未通过基础格式检查：{row_id}")
        saved = records.get(row_id)
        if not isinstance(saved, dict):
            raise ValueError(f"翻译检查点缺少条目：{row_id}")
        saved["chinese"] = correction
        saved["semantic_qa_version"] = SEMANTIC_QA_VERSION
        saved["manual_override"] = True
        saved["qa_issues"] = issues
        source.update({
            "chinese": correction,
            "ok": True,
            "hard_failed": False,
            "manual_override": True,
            "final_issues": [],
            "final_note": "用户人工修订并确认",
        })
        if row_id in qa_by_id:
            qa_by_id[row_id].update({
                "chinese": correction,
                "issues": issues,
                "hard_failed": False,
                "manual_override": True,
            })
        imported.append(row_id)

    semantic["summary"] = summarize_semantic_qa(semantic.get("records", []))
    qa_payload["summary"] = summarize_qa(qa_payload.get("records", []))
    atomic_write_text(semantic_path, json.dumps(semantic, ensure_ascii=False, indent=2))
    atomic_write_text(checkpoint_path, json.dumps(checkpoint, ensure_ascii=False, indent=2))
    atomic_write_text(translation_qa_path, json.dumps(qa_payload, ensure_ascii=False, indent=2))
    remaining = [row for row in semantic.get("records", []) if row.get("hard_failed")]
    if remaining:
        write_review_txt(output_path, remaining)
    else:
        output_path.unlink(missing_ok=True)
    return {
        "imported": len(imported),
        "imported_ids": imported,
        "remaining": len(remaining),
        "ready_to_resume": not remaining,
    }
