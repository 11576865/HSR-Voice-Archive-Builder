from __future__ import annotations

import json
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
    update_candidates: str = ""
    remote_character: str = ""
    remote_index_url: str = "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/EN.xlsx"
    same_group_gap: float = 0.40
    group_gap: float = 1.20
    make_flac: bool = True
    translate_missing: bool = False
    translation_model: str = "gpt-5.6-luna"
    translation_batch_size: int = 80


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
    for key in ("index_csv", "wav_source", "output_dir", "bilingual_csv", "chs_source", "update_candidates"):
        setattr(config, key, _portable_path(root, getattr(config, key)))
    save_project(config)
    return config


def remember_project(root: Path) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"last_project": str(normalize_root(root)), "updated_at": utc_now()}, indent=2),
        encoding="utf-8",
    )


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
    report: dict[str, Any] = {}
    if report_file.is_file():
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
    outputs = {}
    for name in ("manifest.json", "manifest.csv", "bilingual_index_corrected.csv", "bilingual.srt", "build_report.json", "continuous.flac", "update_plan.json"):
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
        "report": report,
        "outputs": outputs,
    }
