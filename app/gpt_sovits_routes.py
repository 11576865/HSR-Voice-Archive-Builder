"""Local project endpoint for manual GPT-SoVITS dataset preparation."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .gpt_sovits_exporter import export_project_dataset
from .project import ProjectConfig

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
        return {"ok": True, "report": export_project_dataset(config, speaker=speaker, language=language)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)
