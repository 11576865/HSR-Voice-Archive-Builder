from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .builder import atomic_write_text
from .diff import classify
from .jobs import assert_no_active_build, assert_project_idle, create_job, delete_project_jobs, get_job, recent_jobs
from .pipeline import build_project_v02
from .preflight import dependency_status
from .quick import (
    create_quick_project,
    discover_source_candidates,
    quick_scan,
    relink_project_source,
)
from .project import (
    ProjectConfig,
    clone_project,
    create_project,
    delete_project,
    forget_project,
    last_project_root,
    load_project,
    project_summary,
    recent_projects,
    resolve_project_path,
    update_project,
)
from .remote_index import fetch_ai_hobbyist_index, remote_update_plan
from .security import api_token, host_allowed, lan_mode, token_matches

BASE = Path(__file__).resolve().parent
app = FastAPI(title="HSR Voice Archive Builder", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

_active_root: Path | None = None


def _restore_last_project() -> None:
    global _active_root
    root = last_project_root()
    if root is None:
        return
    try:
        load_project(root)
        _active_root = root.resolve()
    except Exception:
        _active_root = None


_restore_last_project()


@app.middleware("http")
async def control_surface_guard(request: Request, call_next):
    # A localhost service is still reachable by a browser visiting an unrelated
    # website. Reject unexpected Host values to reduce DNS-rebinding exposure,
    # and require a per-process secret for every control/data API.
    if not host_allowed(request.url.hostname):
        return JSONResponse({"ok": False, "error": "Invalid Host header"}, status_code=400)

    protected = request.url.path.startswith("/api/") or request.url.path == "/build"
    if protected and not token_matches(request.headers.get("X-HSR-Token")):
        return JSONResponse({"ok": False, "error": "Invalid or missing control token"}, status_code=401)

    response = await call_next(request)
    if request.url.path == "/" or protected:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path == "/":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
    return response


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    token = api_token()
    if lan_mode():
        gate = request.query_params.get("token") or request.cookies.get("hsr_voice_gate")
        if not token_matches(gate):
            return HTMLResponse(
                "<h1>HSR Voice Archive Builder</h1><p>LAN control token required.</p>",
                status_code=401,
            )

    template = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    page = template.replace("__HSR_API_TOKEN_JSON__", json.dumps(token))
    response = HTMLResponse(page)
    if lan_mode():
        # This cookie is only an entry gate so a page refresh still works after
        # the URL token is removed. API mutations still require X-HSR-Token.
        response.set_cookie(
            "hsr_voice_gate",
            token,
            httponly=True,
            samesite="strict",
            path="/",
        )
    return response


def optional_path(value: str) -> Path | None:
    value = value.strip()
    return Path(value) if value else None


def _active_config() -> ProjectConfig:
    if _active_root is None:
        raise RuntimeError("No active project")
    return load_project(_active_root)


def _set_active(config: ProjectConfig) -> None:
    global _active_root
    _active_root = Path(config.root).resolve()


def _clear_active() -> None:
    global _active_root
    _active_root = None


def _project_paths(config: ProjectConfig) -> dict[str, Path | None]:
    return {
        "index": resolve_project_path(config, config.index_csv),
        "wavs": resolve_project_path(config, config.wav_source),
        "bilingual": resolve_project_path(config, config.bilingual_csv),
        "chs": resolve_project_path(config, config.chs_source),
        "glossary": resolve_project_path(config, config.glossary_path),
        "reference": resolve_project_path(config, config.reference_source),
        "output": resolve_project_path(config, config.output_dir),
        "state": resolve_project_path(config, config.state_dir),
        "candidates": resolve_project_path(config, config.update_candidates),
    }


@app.get("/api/status")
def api_status():
    project = None
    if _active_root is not None:
        try:
            project = project_summary(_active_config())
        except Exception:
            project = None
    return {
        "ok": True,
        "version": "0.9-H",
        "processing_mode": "local-first",
        "lan_control": lan_mode(),
        "project": project,
        "recent_projects": recent_projects(12),
        "jobs": recent_jobs(20),
        "runtime": dependency_status(),
    }


@app.get("/api/quick/candidates")
def api_quick_candidates():
    try:
        return {"ok": True, "candidates": discover_source_candidates()}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/quick/scan")
def api_quick_scan(
    english_source: str = Form(...),
    chs_source: str = Form(""),
    reference_source: str = Form(""),
    source_text_language: str = Form("en"),
    target_language: str = Form("zh-CN"),
    reference_language: str = Form("auto"),
):
    try:
        plan = quick_scan(
            Path(english_source),
            Path(chs_source) if chs_source.strip() else None,
            reference_source=Path(reference_source) if reference_source.strip() else None,
            source_text_language=source_text_language,
            target_language=target_language,
            reference_language=reference_language,
        )
        return {"ok": True, "plan": plan}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/quick/build")
def api_quick_build(
    english_source: str = Form(...),
    chs_source: str = Form(""),
    project_root: str = Form(""),
    project_name: str = Form(""),
    translation_token_budget: int = Form(0),
    translation_budget_usd: float = Form(0.0),
    reference_source: str = Form(""),
    audio_language: str = Form("auto"),
    source_text_language: str = Form("en"),
    target_language: str = Form("zh-CN"),
    reference_language: str = Form("auto"),
):
    try:
        assert_no_active_build()
        config, plan = create_quick_project(
            Path(english_source),
            chs_source=Path(chs_source) if chs_source.strip() else None,
            reference_source=Path(reference_source) if reference_source.strip() else None,
            root=Path(project_root) if project_root.strip() else None,
            name=project_name,
            audio_language=audio_language,
            source_text_language=source_text_language.strip() or "en",
            target_language=target_language,
            reference_language=reference_language,
        )
        update_project(
            config,
            translation_token_budget=max(0, translation_token_budget),
            translation_budget_usd=max(0.0, translation_budget_usd),
        )
        _set_active(config)
        paths = _project_paths(config)
        if paths["index"] is None or paths["wavs"] is None or paths["output"] is None:
            raise ValueError("Quick project paths are incomplete")
        runtime = dependency_status()
        if config.make_flac and not runtime.get("ffmpeg"):
            raise RuntimeError("FFmpeg is not installed or is not available on PATH")

        def run(report_progress):
            return build_project_v02(
                paths["index"],
                paths["wavs"],
                paths["output"],
                bilingual_csv=paths["bilingual"],
                chs_source=paths["chs"],
                same_group_gap=config.same_group_gap,
                group_gap=config.group_gap,
                make_flac=config.make_flac,
                translate_missing=config.translate_missing,
                translation_model=config.translation_model,
                translation_batch_size=config.translation_batch_size,
                translation_token_budget=config.translation_token_budget,
                translation_budget_usd=config.translation_budget_usd,
                glossary_path=paths["glossary"],
                progress_callback=report_progress,
                reference_source=paths["reference"],
                audio_language=config.audio_language,
                source_text_language=config.source_text_language,
                target_language=config.target_language,
                reference_language=config.reference_language,
                reference_text_embedded=config.reference_text_embedded,
                state_dir=paths["state"],
            )

        job = create_job(
            "quick-build", run, with_progress=True,
            project_root=config.root, project_name=config.name,
        )
        return {
            "ok": True,
            "job": job.id,
            "plan": plan,
            "project": project_summary(config),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/project/create")
def api_project_create(
    root: str = Form(...),
    name: str = Form(...),
    index_csv: str = Form(...),
    wav_source: str = Form(...),
    output_dir: str = Form("output"),
    bilingual_csv: str = Form(""),
    chs_source: str = Form(""),
    glossary_path: str = Form(""),
    reference_source: str = Form(""),
    audio_language: str = Form("auto"),
    source_text_language: str = Form("en"),
    target_language: str = Form("zh-CN"),
    reference_language: str = Form("auto"),
    remote_character: str = Form(""),
):
    try:
        config = create_project(
            Path(root),
            name=name,
            index_csv=index_csv,
            wav_source=wav_source,
            output_dir=output_dir,
            bilingual_csv=bilingual_csv,
            chs_source=chs_source,
            glossary_path=glossary_path,
            reference_source=reference_source,
            audio_language=audio_language,
            source_text_language=source_text_language,
            target_language=target_language,
            reference_language=reference_language,
            remote_character=remote_character,
        )
        _set_active(config)
        return {"ok": True, "project": project_summary(config)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/project/open")
def api_project_open(project_path: str = Form(...)):
    try:
        config = load_project(Path(project_path))
        _set_active(config)
        return {"ok": True, "project": project_summary(config)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)

@app.post("/api/project/close")
def api_project_close():
    _clear_active()
    return {"ok": True, "project": None}


@app.post("/api/project/clone")
def api_project_clone(
    project_path: str = Form(...),
    name: str = Form(...),
    root: str = Form(""),
):
    try:
        source = load_project(Path(project_path))
        assert_project_idle(source.root)
        clone = clone_project(
            source,
            name=name,
            root=Path(root) if root.strip() else None,
        )
        _set_active(clone)
        return {
            "ok": True,
            "project": project_summary(clone),
            "recent_projects": recent_projects(12),
            "source_project": {
                "name": source.name,
                "root": source.root,
            },
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/project/forget")
def api_project_forget(project_path: str = Form(...)):
    try:
        assert_no_active_build()
        root = Path(project_path).expanduser().resolve()
        result = forget_project(root)
        if _active_root is not None and _active_root.resolve() == root:
            _clear_active()
        return {
            "ok": True,
            "result": result,
            "project": None if _active_root is None else project_summary(_active_config()),
            "recent_projects": recent_projects(12),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/project/delete")
def api_project_delete(project_path: str = Form(...)):
    try:
        assert_no_active_build()
        root = Path(project_path).expanduser().resolve()
        was_active = _active_root is not None and _active_root.resolve() == root
        assert_project_idle(str(root))
        result = delete_project(root)
        result["deleted_job_records"] = delete_project_jobs(str(root))
        if was_active:
            _clear_active()
        return {
            "ok": True,
            "result": result,
            "project": None if _active_root is None else project_summary(_active_config()),
            "recent_projects": recent_projects(12),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/project/relink")
def api_project_relink(
    role: str = Form(...),
    replacement_path: str = Form(...),
):
    try:
        config = _active_config()
        assert_project_idle(config.root)
        replacement = replacement_path.strip()
        if not replacement:
            raise ValueError("Replacement path is required")
        config, result = relink_project_source(config, role, Path(replacement))
        return {
            "ok": True,
            "result": result,
            "project": project_summary(config),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/project/save")
def api_project_save(
    name: str = Form(...),
    index_csv: str = Form(...),
    wav_source: str = Form(...),
    output_dir: str = Form("output"),
    bilingual_csv: str = Form(""),
    chs_source: str = Form(""),
    glossary_path: str = Form(""),
    reference_source: str = Form(""),
    audio_language: str = Form("auto"),
    source_text_language: str = Form("en"),
    target_language: str = Form("zh-CN"),
    reference_language: str = Form("auto"),
    update_candidates: str = Form(""),
    remote_character: str = Form(""),
    remote_index_url: str = Form(""),
    same_group_gap: float = Form(0.40),
    group_gap: float = Form(1.20),
    make_flac: bool = Form(False),
    translate_missing: bool = Form(False),
    translation_model: str = Form("gpt-5.6-sol"),
    translation_batch_size: int = Form(80),
    translation_token_budget: int = Form(0),
    translation_budget_usd: float = Form(0.0),
):
    try:
        config = _active_config()
        update_project(
            config,
            name=name.strip(),
            index_csv=index_csv.strip(),
            wav_source=wav_source.strip(),
            output_dir=output_dir.strip() or "output",
            bilingual_csv=bilingual_csv.strip(),
            chs_source=chs_source.strip(),
            glossary_path=glossary_path.strip(),
            reference_source=reference_source.strip(),
            audio_language=audio_language.strip() or "auto",
            source_text_language=source_text_language.strip() or "en",
            target_language=target_language.strip() or "zh-CN",
            reference_language=reference_language.strip() or "auto",
            update_candidates=update_candidates.strip(),
            remote_character=remote_character.strip(),
            remote_index_url=remote_index_url.strip() or config.remote_index_url,
            same_group_gap=same_group_gap,
            group_gap=group_gap,
            make_flac=make_flac,
            translate_missing=translate_missing,
            translation_model=translation_model.strip() or "gpt-5.6-sol",
            translation_batch_size=max(1, translation_batch_size),
            translation_token_budget=max(0, translation_token_budget),
            translation_budget_usd=max(0.0, translation_budget_usd),
        )
        return {"ok": True, "project": project_summary(config)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/project/build")
def api_project_build():
    try:
        assert_no_active_build()
        config = _active_config()
        paths = _project_paths(config)
        if paths["index"] is None or paths["wavs"] is None or paths["output"] is None:
            raise ValueError("Project index, WAV source, and output directory are required")
        if config.make_flac and not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is not installed or is not available on PATH")
        if config.translate_missing and not dependency_status().get("translation_api_key_configured"):
            raise RuntimeError(
                "Translation API key is not configured; run "
                "'python -m app.credentials configure --provider vapi' "
                "or disable AI fallback translation"
            )

        def run(report_progress):
            return build_project_v02(
                paths["index"],
                paths["wavs"],
                paths["output"],
                bilingual_csv=paths["bilingual"],
                chs_source=paths["chs"],
                same_group_gap=config.same_group_gap,
                group_gap=config.group_gap,
                make_flac=config.make_flac,
                translate_missing=config.translate_missing,
                translation_model=config.translation_model,
                translation_batch_size=config.translation_batch_size,
                translation_token_budget=config.translation_token_budget,
                translation_budget_usd=config.translation_budget_usd,
                glossary_path=paths["glossary"],
                progress_callback=report_progress,
                reference_source=paths["reference"],
                audio_language=config.audio_language,
                source_text_language=config.source_text_language,
                target_language=config.target_language,
                reference_language=config.reference_language,
                reference_text_embedded=config.reference_text_embedded,
                state_dir=paths["state"],
            )

        job = create_job(
            "build", run, with_progress=True,
            project_root=config.root, project_name=config.name,
        )
        return {"ok": True, "job": job.id}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/update/scan")
def api_update_scan(candidates_path: str = Form("")):
    try:
        config = _active_config()
        if candidates_path.strip():
            update_project(config, update_candidates=candidates_path.strip())
        paths = _project_paths(config)
        output = paths["output"]
        candidates = paths["candidates"]
        if output is None or candidates is None:
            raise ValueError("Candidate update file is not configured")
        manifest = output / "manifest.json"
        if not manifest.is_file():
            raise FileNotFoundError("Build the project once before checking updates")
        if not candidates.is_file():
            raise FileNotFoundError(candidates)
        result = classify(manifest, candidates)
        atomic_write_text(
            output / "update_plan.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return {"ok": True, "plan": result, "project": project_summary(config)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.post("/api/update/check-remote")
def api_update_check_remote(
    character: str = Form(""),
    remote_index_url: str = Form(""),
):
    try:
        config = _active_config()
        character = character.strip() or config.remote_character
        remote_index_url = remote_index_url.strip() or config.remote_index_url
        if not character:
            raise ValueError("Set a remote character filter first")
        update_project(config, remote_character=character, remote_index_url=remote_index_url)
        paths = _project_paths(config)
        output = paths["output"]
        if output is None:
            raise ValueError("Output directory is not configured")
        manifest = output / "manifest.json"
        if not manifest.is_file():
            raise FileNotFoundError("Build the project once before checking remote updates")

        def run():
            rows = fetch_ai_hobbyist_index(character, remote_index_url)
            plan = remote_update_plan(manifest, rows)
            output.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                output / "update_plan.json",
                json.dumps(plan, ensure_ascii=False, indent=2),
            )
            return plan

        job = create_job(
            "remote-update-check", run,
            project_root=config.root, project_name=config.name,
        )
        return {"ok": True, "job": job.id}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


@app.get("/api/jobs")
def api_jobs():
    return {"ok": True, "jobs": recent_jobs(20)}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = get_job(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)
    return {"ok": True, "job": job}


@app.post("/api/output/open")
def api_output_open():
    try:
        config = _active_config()
        output = resolve_project_path(config, config.output_dir)
        if output is None:
            raise ValueError("Output directory is not configured")
        output.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(output))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(output)])
        elif shutil.which("termux-open"):
            subprocess.Popen(["termux-open", str(output)])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(output)])
        else:
            raise RuntimeError("No supported file-manager opener found")
        return {"ok": True, "path": str(output)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)


# Compatibility endpoint retained for existing v0.2 callers.
@app.post("/build")
def legacy_build(
    index_csv: str = Form(...),
    bilingual_csv: str = Form(""),
    chs_source: str = Form(""),
    wav_source: str = Form(...),
    output_dir: str = Form(...),
    same_gap: float = Form(0.40),
    group_gap: float = Form(1.20),
    make_flac: bool = Form(False),
    translate_missing: bool = Form(False),
    translation_model: str = Form("gpt-5.6-sol"),
    translation_batch_size: int = Form(80),
    translation_token_budget: int = Form(0),
    translation_budget_usd: float = Form(0.0),
    glossary_path: str = Form(""),
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
            translation_batch_size=max(1, translation_batch_size),
            translation_token_budget=max(0, translation_token_budget),
            translation_budget_usd=max(0.0, translation_budget_usd),
        )
        return JSONResponse({"ok": True, "report": report})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)
