from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .pipeline import build_project_v02

BASE = Path(__file__).resolve().parent
app = FastAPI(title="HSR Voice Archive Builder")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")


def optional_path(value: str) -> Path | None:
    value = value.strip()
    return Path(value) if value else None


@app.post("/build")
def build(
    index_csv: str = Form(...),
    bilingual_csv: str = Form(""),
    chs_source: str = Form(""),
    wav_source: str = Form(...),
    output_dir: str = Form(...),
    same_gap: float = Form(0.40),
    group_gap: float = Form(1.20),
    make_flac: bool = Form(False),
    translate_missing: bool = Form(False),
    translation_model: str = Form("gpt-5.6-luna"),
    translation_batch_size: int = Form(80),
):
    try:
        report = build_project_v02(
            Path(index_csv),
            Path(wav_source),
            Path(output_dir),
            bilingual_csv=optional_path(bilingual_csv),
            chs_source=optional_path(chs_source),
            same_group_gap=same_gap,
            group_gap=group_gap,
            make_flac=make_flac,
            translate_missing=translate_missing,
            translation_model=translation_model,
            translation_batch_size=translation_batch_size,
        )
        return JSONResponse({"ok": True, "report": report})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)
