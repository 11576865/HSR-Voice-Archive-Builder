from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .builder import build_project

BASE = Path(__file__).resolve().parent
app = FastAPI(title="HSR Voice Archive Builder")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")


@app.post("/build")
def build(
    index_csv: str = Form(...),
    bilingual_csv: str = Form(...),
    chs_source: str = Form(...),
    wav_source: str = Form(...),
    output_dir: str = Form(...),
    same_gap: float = Form(0.40),
    group_gap: float = Form(1.20),
    make_flac: bool = Form(False),
):
    try:
        report = build_project(
            Path(index_csv),
            Path(bilingual_csv),
            Path(chs_source),
            Path(wav_source),
            Path(output_dir),
            same_group_gap=same_gap,
            group_gap=group_gap,
            make_flac=make_flac,
        )
        return JSONResponse({"ok": True, "report": report})
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )
