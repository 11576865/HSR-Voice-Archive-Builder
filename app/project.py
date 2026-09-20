from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_FILENAME = ".hsr-voice-project.json"
STATE_DIR = Path.home() / ".hsr-voice-archive-builder"
STATE_FILE = STATE_DIR / "state.json"


@dataclass
class ProjectConfig:
    schema_version: int
    name: str
    root: str
    index_csv: str
    wav_source: str
    output_dir: str = "output"
    bilingual_csv: str = ""
    chs_source: str = ""
    glossary_path: str = ""
    reference_source: str = ""
    audio_language: str = "auto"
    source_text_language: str = "en"
    target_language: str = "zh-CN"
    reference_language: str = "auto"
    update_candidates: str = ""
    remote_character: str = ""
    remote_index_url: str = "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/EN.xlsx"
    same_group_gap: float = 0.40
    group_gap: float = 1.20
    make_flac: bool = True
    translate_missing: bool = False
    translation_model: str = "gpt-5.6-sol"
    translation_batch_size: int = 80
    translation_token_budget: int = 0
    translation_budget_usd: float = 0.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def normalize_root(root: Path) -> Path:
    return root.expanduser().resolve()


def _portable_path(root: Path, value: str | Path | None) -> str:
    if value is None or str(value).strip() == "":
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        return str(path)
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path.resolve())


def resolve_project_path(config: ProjectConfig, value: str) -> Path | None:
    value = value.strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return normalize_root(Path(config.root)) / path


def project_file(root: Path) -> Path:
    return normalize_root(root) / PROJECT_FILENAME


def save_project(config: ProjectConfig) -> Path:
    root = normalize_root(Path(config.root))
    root.mkdir(parents=True, exist_ok=True)
    payload = asdict(config)
    payload["root"] = str(root)
    path = root / PROJECT_FILENAME
    atomic_write_json(path, payload)
    remember_project(root)
    return path


def create_project(
    root: Path,
    *,
    name: str,
    index_csv: str,
    wav_source: str,
    output_dir: str = "output",
    bilingual_csv: str = "",
    chs_source: str = "",
    glossary_path: str = "",
    reference_source: str = "",
    audio_language: str = "auto",
    source_text_language: str = "en",
    target_language: str = "zh-CN",
    reference_language: str = "auto",
    remote_character: str = "",
) -> ProjectConfig:
    root = normalize_root(root)
    if not name.strip():
        raise ValueError("Project name is required")
    if not index_csv.strip():
        raise ValueError("Index CSV is required")
    if not wav_source.strip():
        raise ValueError("WAV source is required")
    config = ProjectConfig(
        schema_version=1,
        name=name.strip(),
        root=str(root),
        index_csv=_portable_path(root, index_csv),
        wav_source=_portable_path(root, wav_source),
        output_dir=_portable_path(root, output_dir) or "output",
        bilingual_csv=_portable_path(root, bilingual_csv),
        chs_source=_portable_path(root, chs_source),
        glossary_path=_portable_path(root, glossary_path),
        reference_source=_portable_path(root, reference_source),
        audio_language=str(audio_language or "auto").strip() or "auto",
        source_text_language=str(source_text_language or "en").strip() or "en",
        target_language=str(target_language or "zh-CN").strip() or "zh-CN",
        reference_language=str(reference_language or "auto").strip() or "auto",
        remote_character=remote_character.strip(),
    )
    save_project(config)
    return config


def load_project(root_or_file: Path) -> ProjectConfig:
    candidate = root_or_file.expanduser()
    path = candidate if candidate.name == PROJECT_FILENAME else candidate / PROJECT_FILENAME
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError(f"Unsupported project schema: {data.get('schema_version')}")
    data["root"] = str(path.parent)
    config = ProjectConfig(**data)
    remember_project(path.parent)
    return config


def update_project(config: ProjectConfig, **changes: Any) -> ProjectConfig:
    allowed = set(ProjectConfig.__dataclass_fields__)
    for key, value in changes.items():
        if key not in allowed or key in {"schema_version", "root"}:
            continue
        if value is None:
            continue
        setattr(config, key, value)
    root = normalize_root(Path(config.root))
    for key in ("index_csv", "wav_source", "output_dir", "bilingual_csv", "chs_source", "glossary_path", "reference_source", "update_candidates"):
        setattr(config, key, _portable_path(root, getattr(config, key)))
    save_project(config)
    return config


def _read_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def remember_project(root: Path) -> None:
    root = normalize_root(root)
    state = _read_state()
    recent = [
        str(Path(item).expanduser())
        for item in state.get("recent_projects", [])
        if isinstance(item, str) and item.strip()
    ]
    value = str(root)
    recent = [item for item in recent if item != value]
    recent.insert(0, value)
    atomic_write_json(
        STATE_FILE,
        {
            "last_project": value,
            "recent_projects": recent[:20],
            "updated_at": utc_now(),
        },
    )


def recent_projects(limit: int = 12) -> list[dict[str, str]]:
    state = _read_state()
    roots: list[str] = []
    last = str(state.get("last_project", "") or "").strip()
    if last:
        roots.append(last)
    for item in state.get("recent_projects", []):
        if isinstance(item, str) and item.strip() and item not in roots:
            roots.append(item)

    result: list[dict[str, str]] = []
    for raw in roots[: max(1, int(limit)) * 2]:
        root = Path(raw).expanduser()
        path = root / PROJECT_FILENAME
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        result.append({
            "name": str(payload.get("name", "") or root.name),
            "root": str(root.resolve()),
        })
        if len(result) >= max(1, int(limit)):
            break
    return result


def last_project_root() -> Path | None:
    if not STATE_FILE.is_file():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        value = data.get("last_project", "")
        return Path(value) if value else None
    except (OSError, ValueError, TypeError):
        return None


def project_summary(config: ProjectConfig) -> dict[str, Any]:
    output = resolve_project_path(config, config.output_dir)
    assert output is not None
    manifest = output / "manifest.json"
    report_file = output / "build_report.json"
    final_stage_file = output / "stages" / "final_report.json"
    report: dict[str, Any] = {}
    if report_file.is_file():
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
    outputs = {}
    for name in (
        "manifest.json",
        "manifest.csv",
        "bilingual_index_corrected.csv",
        "bilingual.srt",
        "build_report.json",
        "translation_qa.json",
        "semantic_qa.json",
        "translation_usage.json",
        "continuous.flac",
        "update_plan.json",
        "stages/01_scan.json",
        "stages/02_metadata.json",
        "stages/03_translation.json",
        "stages/04_translation_qa.json",
        "stages/05_manifest.json",
        "stages/06_audio_state.json",
        "stages/final_report.json",
    ):
        p = output / name
        outputs[name] = {
            "exists": p.is_file(),
            "size_bytes": p.stat().st_size if p.is_file() else 0,
            "path": str(p),
        }
    return {
        "name": config.name,
        "root": config.root,
        "config": asdict(config),
        "has_manifest": manifest.is_file(),
        "build_complete": report_file.is_file() and final_stage_file.is_file(),
        "report": report,
        "outputs": outputs,
    }
