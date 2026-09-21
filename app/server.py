from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import csv
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .builder import atomic_write_text, ensure_dir_or_extract
from .diff import classify
from .huggingface_audio import download_resolved_audio, download_result_json, resolve_targets
from .identity import infer_group
from .human_review import import_review_txt
from .jobs import assert_no_active_build, assert_project_idle, create_job, delete_project_jobs, get_job, recent_jobs
from .pipeline import build_project_v02
from .preflight import dependency_status
from .quick import (
    create_quick_project,
    discover_source_candidates,
    quick_scan,
    relink_project_source,
    remote_character_candidates,
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
from .remote_index import exclude_applied_updates, exclude_indexed_updates, fetch_ai_hobbyist_index, remote_update_plan
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
    intro_gap: float = Form(5.0),
    same_group_gap: float = Form(0.40),
    group_gap: float = Form(1.20),
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
            intro_gap=max(0.0, intro_gap),
            same_group_gap=max(0.0, same_group_gap),
            group_gap=max(0.0, group_gap),
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
                intro_gap=config.intro_gap,
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
    intro_gap: float = Form(5.0),
    same_group_gap: float = Form(0.40),
    group_gap: float = Form(1.20),
    make_flac: bool = Form(False),
    translate_missing: bool = Form(False),
    translation_model: str = Form("gpt-5.6-luna"),
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
            intro_gap=max(0.0, intro_gap),
            same_group_gap=max(0.0, same_group_gap),
            group_gap=group_gap,
            make_flac=make_flac,
            translate_missing=translate_missing,
            translation_model=translation_model.strip() or "gpt-5.6-luna",
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
                intro_gap=config.intro_gap,
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


@app.post("/api/review/import")
def api_review_import(review_text: str = Form(...)):
    try:
        config = _active_config()
        paths = _project_paths(config)
        state, output = paths["state"], paths["output"]
        if state is None or output is None:
            raise ValueError("Project state or output directory is not configured")
        result = import_review_txt(
            review_text,
            state_dir=state,
            output_path=output / "semantic_review_required.txt",
            target_language=config.target_language,
        )
        return {"ok": True, "result": result, "project": project_summary(config)}
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
        requested_character = character.strip()
        remote_index_url = remote_index_url.strip() or config.remote_index_url
        characters = remote_character_candidates(config, requested_character)
        if not characters:
            raise ValueError(
                "Could not identify the remote-index character from this project; "
                "run Quick Scan again or set it in Advanced settings"
            )
        update_project(config, remote_index_url=remote_index_url)
        paths = _project_paths(config)
        output = paths["output"]
        if output is None:
            raise ValueError("Output directory is not configured")
        manifest = output / "manifest.json"
        if not manifest.is_file():
            raise FileNotFoundError("Build the project once before checking remote updates")

        def run():
            rows = []
            matched_character = ""
            attempted: list[str] = []
            for candidate in characters:
                attempted.append(candidate)
                rows = fetch_ai_hobbyist_index(candidate, remote_index_url)
                if rows:
                    matched_character = candidate
                    break
            if not rows:
                raise RuntimeError(
                    "Remote index returned no rows for the detected role labels: "
                    + ", ".join(attempted)
                )
            plan = remote_update_plan(
                manifest,
                rows,
                url=remote_index_url,
                queried_character=matched_character,
            )
            plan = exclude_applied_updates(plan, output / "update_apply_report.json")
            plan = exclude_indexed_updates(plan, paths["index"])
            resolved_character = str(plan.get("character") or matched_character)
            plan["attempted_characters"] = attempted
            update_project(
                config,
                remote_character=resolved_character,
                remote_index_url=remote_index_url,
            )
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


@app.post("/api/update/apply-remote")
def api_update_apply_remote():
    """Download reliably resolved additions, adopt a combined source, and leave rebuild explicit."""
    try:
        config = _active_config()
        paths = _project_paths(config)
        output, source, index = paths["output"], paths["wavs"], paths["index"]
        if output is None or source is None or index is None:
            raise ValueError("Project source, index, or output is not configured")
        update_plan_path = output / "update_plan.json"
        if not update_plan_path.is_file():
            raise FileNotFoundError("Check the remote index before downloading additions")
        remote_plan = json.loads(update_plan_path.read_text(encoding="utf-8"))
        targets = []
        for item in remote_plan.get("new_logical", []):
            if isinstance(item, dict):
                metadata = dict(item.get("metadata") or {})
                metadata.setdefault("filename", item.get("filename", ""))
                if metadata.get("filename"):
                    targets.append(metadata)
        if not targets:
            raise ValueError("The latest check contains no new voice records")
        project_root = Path(config.root).resolve()

        def run(report_progress):
            state = project_root / ".state" / "huggingface"
            generated = project_root / ".generated"
            result_json = state / "result.json"
            report_progress("metadata", "正在取得 Hugging Face 定位索引", 0, 1)
            download_result_json(result_json, lambda message: report_progress("metadata", message, 0, 1))
            report_progress("resolve", "正在将新增条目定位到精确数据集行", 0, len(targets))
            resolved = resolve_targets(result_json, targets)
            report_progress("resolve", f"已可靠定位 {len(resolved['targets'])}/{len(targets)} 条新增语音", len(targets), len(targets))
            atomic_write_text(state / "incremental_resolution.json", json.dumps(resolved, ensure_ascii=False, indent=2))
            if not resolved["targets"]:
                raise RuntimeError("新增条目均无法可靠映射到 Hugging Face 音频；未修改项目")
            incoming = generated / "incremental_audio"
            downloaded = download_resolved_audio(
                resolved,
                incoming,
                lambda current, total, name: report_progress("download", f"正在下载：{name}", current, total),
            )
            if downloaded["failed"]:
                atomic_write_text(state / "incremental_download.json", json.dumps(downloaded, ensure_ascii=False, indent=2))
            successful = {Path(row["filename"]).name for row in downloaded["completed"]}
            if not successful:
                raise RuntimeError("没有新增音频下载成功；未修改项目")
            report_progress("apply", f"正在合并 {len(successful)} 条新增语音并更新项目索引", 0, 1)

            generated.mkdir(parents=True, exist_ok=True)
            temporary_root = Path(tempfile.mkdtemp(prefix="combined-audio-", dir=generated))
            combined = generated / "combined_audio"
            try:
                extracted = ensure_dir_or_extract(source, temporary_root, "existing")
                for wav in extracted.rglob("*.wav"):
                    target = temporary_root / wav.name
                    if wav.resolve() != target.resolve():
                        shutil.copy2(wav, target)
                if extracted.parent == temporary_root and extracted != temporary_root:
                    shutil.rmtree(extracted)
                for audio in incoming.iterdir():
                    if audio.is_file() and not audio.name.endswith(".part"):
                        shutil.copy2(audio, temporary_root / audio.name)
                if combined.exists():
                    shutil.rmtree(combined)
                temporary_root.replace(combined)
            except Exception:
                shutil.rmtree(temporary_root, ignore_errors=True)
                raise

            with index.open("r", encoding="utf-8-sig", newline="") as handle:
                existing_rows = list(csv.DictReader(handle))
                fields = list(existing_rows[0].keys()) if existing_rows else [
                    "index", "group", "filename", "source", "source_detail", "english",
                    "reference_text", "reference_language", "sha256",
                ]
            known = {Path(str(row.get("filename", ""))).name for row in existing_rows}
            details = {Path(str(row.get("filename", ""))).name: row for row in targets}
            for filename in sorted(successful):
                if filename in known:
                    continue
                metadata = details.get(filename, {})
                existing_rows.append({
                    "index": str(len(existing_rows) + 1),
                    "group": infer_group(Path(filename).stem),
                    "filename": filename,
                    "source": "huggingface",
                    "source_detail": "simon3000/starrail-voice",
                    "english": str(metadata.get("english", "")),
                    "reference_text": "",
                    "reference_language": "",
                    "sha256": "",
                })
            updated_index = generated / "quick_index_incremental.csv"
            with updated_index.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(existing_rows)
            update_project(config, wav_source=str(combined), index_csv=str(updated_index), wav_source_fingerprint="")
            report = {
                "resolved": len(resolved["targets"]),
                "unresolved": resolved["unresolved"],
                "downloaded_or_existing": len(downloaded["completed"]),
                "failed": downloaded["failed"],
                "project_updated": True,
                "rebuild_required": True,
                "applied_filenames": sorted(successful),
            }
            atomic_write_text(output / "update_apply_report.json", json.dumps(report, ensure_ascii=False, indent=2))
            report_progress("apply", "新增语音已应用，等待重新构建成品", 1, 1)
            return report

        job = create_job(
            "remote-update-apply", run, with_progress=True,
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


@app.get("/api/review/file")
def api_review_file():
    config = _active_config()
    output = resolve_project_path(config, config.output_dir)
    if output is None:
        return JSONResponse({"ok": False, "error": "Output directory is not configured"}, status_code=400)
    path = output / "semantic_review_required.txt"
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "No review file is pending"}, status_code=404)
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)


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
    translation_model: str = Form("gpt-5.6-luna"),
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
