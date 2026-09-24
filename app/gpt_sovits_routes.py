from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .gpt_sovits_exporter import export_gpt_sovits_dataset, load_training_rows
from .project import load_project, resolve_project_path

router = APIRouter(prefix="/api")


@router.post("/project/{project_id}/export/gpt-sovits")
def export_project_gpt_sovits(
    project_id: str,
    speaker: str = "",
    language: str = "en",
):
    """Export dataset files for manual GPT-SoVITS WebUI training.

    This endpoint does not call GPT-SoVITS and does not start training.
    """
    try:
        config = load_project(Path(project_id))

        wav_path = resolve_project_path(config, config.wav_source)
        output_path = resolve_project_path(config, config.output_dir)
        bilingual_path = resolve_project_path(config, config.bilingual_csv)
        manifest_path = resolve_project_path(config, config.index_csv)

        if wav_path is None or output_path is None:
            raise ValueError("Project paths are incomplete")

        rows = load_training_rows(bilingual_path, manifest_path)

        report = export_gpt_sovits_dataset(
            rows,
            wav_path,
            output_path / "gpt_sovits_dataset",
            speaker=speaker or config.name,
            language=language,
        )
        return {"ok": True, "report": report}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )
