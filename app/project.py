from __future__ import annotations

import json
import os
import shutil
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
    state_dir: str = ".state"
    bilingual_csv: str = ""
    chs_source: str = ""
    glossary_path: str = ""
    reference_source: str = ""
    reference_text_embedded: bool = False
    managed_project_root: bool = False
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
    state_dir: str = ".state",
    bilingual_csv: str = "",
    chs_source: str = "",
    glossary_path: str = "",
    reference_source: str = "",
    reference_text_embedded: bool = False,
    managed_project_root: bool = False,
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
        state_dir=_portable_path(root, state_dir) or ".state",
        bilingual_csv=_portable_path(root, bilingual_csv),
        chs_source=_portable_path(root, chs_source),
        glossary_path=_portable_path(root, glossary_path),
        reference_source=_portable_path(root, reference_source),
        reference_text_embedded=bool(reference_text_embedded),
        managed_project_root=bool(managed_project_root),
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
    old_reference_identity = (
        str(resolve_project_path(config, config.reference_source) or ""),
        str(resolve_project_path(config, config.index_csv) or ""),
        str(resolve_project_path(config, config.wav_source) or ""),
        str(config.reference_language or "auto"),
    )
    for key, value in changes.items():
        if key not in allowed or key in {"schema_version", "root"}:
            continue
        if value is None:
            continue
        setattr(config, key, value)
    root = normalize_root(Path(config.root))
    for key in ("index_csv", "wav_source", "output_dir", "state_dir", "bilingual_csv", "chs_source", "glossary_path", "reference_source", "update_candidates"):
        setattr(config, key, _portable_path(root, getattr(config, key)))
    new_reference_identity = (
        str(resolve_project_path(config, config.reference_source) or ""),
        str(resolve_project_path(config, config.index_csv) or ""),
        str(resolve_project_path(config, config.wav_source) or ""),
        str(config.reference_language or "auto"),
    )
    if config.reference_text_embedded and new_reference_identity != old_reference_identity:
        # Embedded reference text is aligned to a particular primary index,
        # primary package and reference package/language. Manual edits to any of
        # those inputs must not silently keep the old materialized mapping.
        config.reference_text_embedded = False
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
    hidden = [
        str(Path(item).expanduser())
        for item in state.get("hidden_projects", [])
        if isinstance(item, str) and item.strip()
    ]
    value = str(root)
    recent = [item for item in recent if item != value]
    recent.insert(0, value)
    hidden = [item for item in hidden if item != value]
    atomic_write_json(
        STATE_FILE,
        {
            "last_project": value,
            "recent_projects": recent[:20],
            "hidden_projects": hidden[:100],
            "updated_at": utc_now(),
        },
    )


def forget_project(root: Path) -> dict[str, Any]:
    """Remove a project from the switcher without deleting project files."""
    root = normalize_root(root)
    value = str(root)
    state = _read_state()
    recent = [
        str(Path(item).expanduser())
        for item in state.get("recent_projects", [])
        if isinstance(item, str) and item.strip() and str(Path(item).expanduser()) != value
    ]
    hidden = [
        str(Path(item).expanduser())
        for item in state.get("hidden_projects", [])
        if isinstance(item, str) and item.strip()
    ]
    hidden = [item for item in hidden if item != value]
    hidden.insert(0, value)
    last = str(state.get("last_project", "") or "").strip()
    if last == value:
        last = recent[0] if recent else ""
    atomic_write_json(
        STATE_FILE,
        {
            "last_project": last,
            "recent_projects": recent[:20],
            "hidden_projects": hidden[:100],
            "updated_at": utc_now(),
        },
    )
    return {"root": value, "forgotten": True}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def delete_project(root: Path) -> dict[str, Any]:
    """Delete project-owned state while protecting external/user source files.

    Quick Mode roots are explicitly marked as managed and may be removed in full.
    Manual roots keep arbitrary user files; only the project marker, generated
    directory, and an in-root output directory are removed.
    """
    root = normalize_root(root)
    marker = root / PROJECT_FILENAME
    if not marker.is_file():
        raise FileNotFoundError(marker)
    if root == Path.home().resolve() or root.parent == root:
        raise ValueError(f"Refusing to delete unsafe project root: {root}")

    config = load_project(root)
    removed: list[str] = []
    retained: list[str] = []

    # Remove it from selection state before filesystem mutation. Hidden state
    # also prevents sibling discovery from re-adding a partially deleted project.
    forget_project(root)

    if config.managed_project_root:
        shutil.rmtree(root)
        removed.append(str(root))
        return {
            "root": str(root),
            "deleted": True,
            "managed_root": True,
            "removed": removed,
            "retained": retained,
        }

    output = resolve_project_path(config, config.output_dir)
    state_path = resolve_project_path(config, config.state_dir)
    generated = root / ".generated"
    if output is not None and output != root and _is_within(output, root) and output.exists():
        if output.is_dir():
            shutil.rmtree(output)
        else:
            output.unlink()
        removed.append(str(output))
    if state_path is not None and state_path != root and _is_within(state_path, root) and state_path.exists():
        if state_path.is_dir():
            shutil.rmtree(state_path)
        else:
            state_path.unlink()
        removed.append(str(state_path))
    if generated.is_dir() and _is_within(generated, root):
        shutil.rmtree(generated)
        removed.append(str(generated))
    if marker.is_file():
        marker.unlink()
        removed.append(str(marker))

    try:
        leftovers = list(root.iterdir())
    except OSError:
        leftovers = []
    if not leftovers:
        root.rmdir()
        removed.append(str(root))
    else:
        retained = [str(p) for p in leftovers[:50]]

    return {
        "root": str(root),
        "deleted": True,
        "managed_root": False,
        "removed": removed,
        "retained": retained,
    }


def recent_projects(limit: int = 12) -> list[dict[str, str]]:
    state = _read_state()
    roots: list[str] = []
    hidden = {
        str(Path(item).expanduser())
        for item in state.get("hidden_projects", [])
        if isinstance(item, str) and item.strip()
    }
    last = str(state.get("last_project", "") or "").strip()
    if last:
        roots.append(last)
    for item in state.get("recent_projects", []):
        if isinstance(item, str) and item.strip() and item not in roots:
            roots.append(item)

    # Discover sibling Quick Mode projects so the switcher is useful even on
    # first launch after upgrading from an older version that remembered only
    # one last_project value.
    bases: list[Path] = []
    if last:
        bases.append(Path(last).expanduser().parent)
    home = Path.home()
    bases.extend([
        home / "storage" / "downloads" / "HSR_Voice_Test",
        home / "storage" / "shared" / "Download" / "HSR_Voice_Test",
        Path("/storage/emulated/0/Download/HSR_Voice_Test"),
        home / "HSR-Voice-Projects",
    ])
    for base in bases:
        try:
            for marker in base.glob(f"*/{PROJECT_FILENAME}"):
                value = str(marker.parent.resolve())
                if value not in roots and value not in hidden:
                    roots.append(value)
        except (OSError, PermissionError):
            continue

    result: list[dict[str, str]] = []
    roots = [raw for raw in roots if raw not in hidden]
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
            "managed_project_root": bool(payload.get("managed_project_root", False)),
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
    state = resolve_project_path(config, config.state_dir)
    assert output is not None
    assert state is not None
    manifest = output / "manifest.json"
    report_file = output / "build_report.json"
    final_stage_file = state / "stages" / "final_report.json"
    legacy_final_stage_file = output / "stages" / "final_report.json"
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
        "continuous.flac",
        "update_plan.json",
    ):
        p = output / name
        outputs[name] = {
            "exists": p.is_file(),
            "size_bytes": p.stat().st_size if p.is_file() else 0,
            "path": str(p),
        }

    state_outputs = {}
    for name in (
        ".translation_checkpoint.json",
        "translation_qa.json",
        "semantic_qa.json",
        "translation_usage.json",
        "stages/01_scan.json",
        "stages/02_metadata.json",
        "stages/03_translation.json",
        "stages/04_translation_qa.json",
        "stages/05_manifest.json",
        "stages/06_audio_state.json",
        "stages/final_report.json",
    ):
        p = state / name
        state_outputs[name] = {
            "exists": p.is_file(),
            "size_bytes": p.stat().st_size if p.is_file() else 0,
            "path": str(p),
        }
    return {
        "name": config.name,
        "root": config.root,
        "config": asdict(config),
        "has_manifest": manifest.is_file(),
        "build_complete": report_file.is_file() and (
            final_stage_file.is_file() or legacy_final_stage_file.is_file()
        ),
        "report": report,
        "outputs": outputs,
        "state_outputs": state_outputs,
    }
