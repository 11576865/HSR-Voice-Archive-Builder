"""Local project endpoint for manual GPT-SoVITS dataset preparation."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .builder import ensure_dir_or_extract
from .gpt_sovits_exporter import export_gpt_sovits_dataset, load_training_rows
from .project import ProjectConfig, resolve_project_path

router = APIRouter(prefix="/api")
_project_resolver: Callable[[str], ProjectConfig] | None = None


def configure_project_resolver(resolver: Callable[[str], ProjectConfig]) -> None:
    global _project_resolver
    _project_resolver = resolver


@router.post("/project/{project_id}/export/gpt-sovits")
def export_project_gpt_sovits(project_id: str, speaker: str = "", language: str = "en"):
    """Create WAV, .list and quality reports without contacting GPT-SoVITS."""
    try:
        if _project_resolver is None:
            raise RuntimeError("Project resolver is not configured")
        config = _project_resolver(project_id)
        wav_path = resolve_project_path(config, config.wav_source)
        output = resolve_project_path(config, config.output_dir)
        if wav_path is None or output is None:
            raise ValueError("Project paths are incomplete")
        corrected = output / "bilingual_index_corrected.csv"
        manifest = output / "manifest.csv"
        if not manifest.is_file() and not (output / "manifest.json").is_file():
            raise FileNotFoundError("Build the archive before exporting a training dataset")
        if not manifest.is_file():
            # Legacy completed projects can use the final corrected CSV directly.
            if not corrected.is_file():
                raise FileNotFoundError("Completed archive manifest CSV is missing")
        rows = load_training_rows(corrected, manifest)
        name = (speaker or config.name).strip()
        if not name or name != name.strip(" .") or any(c in name for c in '<>:"/\\|?*\r\n') or name in {".", ".."}:
            raise ValueError("Invalid speaker name")
        output.mkdir(parents=True, exist_ok=True)
        destination = output / f"{name}_GPTSoVITS"
        with tempfile.TemporaryDirectory(prefix=".gpt_sovits_", dir=output) as temp:
            working = Path(temp)
            wav_root = ensure_dir_or_extract(wav_path, working, "source_wavs")
            staging = working / destination.name
            report = export_gpt_sovits_dataset(rows, wav_root, staging, speaker=name, language=language)
            # The WebUI .list must point to the published dataset, not the temporary folder.
            list_file = staging / f"{name}.list"
            list_file.write_text(
                list_file.read_text(encoding="utf-8").replace(str(staging.resolve()), str(destination.resolve())),
                encoding="utf-8",
            )
            report["output"] = str(destination.resolve())
            report["list_file"] = str((destination / f"{name}.list").resolve())
            (staging / "dataset_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            backup = working / "previous_dataset"
            if destination.exists():
                destination.rename(backup)
            try:
                staging.rename(destination)
            except BaseException:
                if backup.exists():
                    backup.rename(destination)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        return {"ok": True, "report": report}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)
