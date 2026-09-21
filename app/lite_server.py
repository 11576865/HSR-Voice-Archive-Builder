from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import csv
import tempfile
from email.parser import BytesParser
from email.policy import default as email_policy
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
STATIC = BASE / "static"
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


def _bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "HSRVoiceLite/0.9-H"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _host_ok(self) -> bool:
        raw = self.headers.get("Host", "")
        host = urlsplit("//" + raw).hostname
        return host_allowed(host)

    def _api_ok(self) -> bool:
        return token_matches(self.headers.get("X-HSR-Token"))

    def _send_bytes(
        self,
        code: int,
        data: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: object, code: int = 200) -> None:
        self._send_bytes(
            code,
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, exc: Exception, code: int = 400) -> None:
        self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, code)

    def _read_body(self) -> bytes:
        length = _int(self.headers.get("Content-Length", "0"), 0)
        if length < 0 or length > 8 * 1024 * 1024:
            raise ValueError("Request body too large")
        return self.rfile.read(length)

    def _form(self) -> dict[str, str]:
        body = self._read_body()
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return {str(k): "" if v is None else str(v) for k, v in data.items()}
        if ctype.startswith("application/x-www-form-urlencoded"):
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            return {k: v[-1] if v else "" for k, v in parsed.items()}
        if ctype.startswith("multipart/form-data"):
            envelope = (
                f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
                + body
            )
            msg = BytesParser(policy=email_policy).parsebytes(envelope)
            result: dict[str, str] = {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                raw = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                result[name] = raw.decode(charset, "replace")
            return result
        if not body:
            return {}
        raise ValueError(f"Unsupported Content-Type: {ctype}")

    def _lan_gate_ok(self) -> bool:
        if not lan_mode():
            return True
        query = parse_qs(urlsplit(self.path).query)
        candidate = (query.get("token") or [""])[-1]
        if not candidate:
            jar = cookies.SimpleCookie()
            jar.load(self.headers.get("Cookie", ""))
            morsel = jar.get("hsr_voice_gate")
            candidate = morsel.value if morsel else ""
        return token_matches(candidate)

    def do_GET(self) -> None:
        if not self._host_ok():
            self._json({"ok": False, "error": "Invalid Host header"}, 400)
            return
        path = urlsplit(self.path).path

        if path.startswith("/api/"):
            if not self._api_ok():
                self._json({"ok": False, "error": "Invalid or missing control token"}, 401)
                return
            try:
                self._handle_api_get(path)
            except Exception as exc:
                self._error(exc)
            return

        if path == "/":
            if not self._lan_gate_ok():
                self._send_bytes(
                    401,
                    b"<h1>HSR Voice Archive Builder</h1><p>LAN control token required.</p>",
                    "text/html; charset=utf-8",
                )
                return
            template = (STATIC / "index.html").read_text(encoding="utf-8")
            page = template.replace("__HSR_API_TOKEN_JSON__", json.dumps(api_token()))
            headers = {
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                    "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                )
            }
            if lan_mode():
                headers["Set-Cookie"] = (
                    f"hsr_voice_gate={api_token()}; Path=/; HttpOnly; SameSite=Strict"
                )
            self._send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8", extra_headers=headers)
            return

        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC / rel).resolve()
            try:
                target.relative_to(STATIC.resolve())
            except ValueError:
                self._json({"ok": False, "error": "Invalid static path"}, 400)
                return
            if not target.is_file():
                self._json({"ok": False, "error": "Not found"}, 404)
                return
            suffix = target.suffix.lower()
            ctype = {
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
            }.get(suffix, "application/octet-stream")
            self._send_bytes(200, target.read_bytes(), ctype)
            return

        self._json({"ok": False, "error": "Not found"}, 404)

    def _handle_api_get(self, path: str) -> None:
        if path == "/api/status":
            project = None
            if _active_root is not None:
                try:
                    project = project_summary(_active_config())
                except Exception:
                    project = None
            self._json({
                "ok": True,
                "version": "0.9-H-termux-lite",
                "processing_mode": "local-first",
                "lan_control": lan_mode(),
                "project": project,
                "recent_projects": recent_projects(12),
                "jobs": recent_jobs(20),
                "runtime": dependency_status(),
            })
            return
        if path == "/api/quick/candidates":
            self._json({"ok": True, "candidates": discover_source_candidates()})
            return
        if path == "/api/jobs":
            self._json({"ok": True, "jobs": recent_jobs(20)})
            return
        if path == "/api/review/file":
            config = _active_config()
            output = resolve_project_path(config, config.output_dir)
            review = output / "semantic_review_required.txt" if output else None
            if review is None or not review.is_file():
                self._json({"ok": False, "error": "No review file is pending"}, 404)
                return
            self._send_bytes(
                200,
                review.read_bytes(),
                "text/plain; charset=utf-8",
                extra_headers={"Content-Disposition": 'attachment; filename="semantic_review_required.txt"'},
            )
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = get_job(job_id)
            if job is None:
                self._json({"ok": False, "error": "Job not found"}, 404)
            else:
                self._json({"ok": True, "job": job})
            return
        self._json({"ok": False, "error": "Not found"}, 404)

    def do_POST(self) -> None:
        if not self._host_ok():
            self._json({"ok": False, "error": "Invalid Host header"}, 400)
            return
        path = urlsplit(self.path).path
        if not (path.startswith("/api/") or path == "/build"):
            self._json({"ok": False, "error": "Not found"}, 404)
            return
        if not self._api_ok():
            self._json({"ok": False, "error": "Invalid or missing control token"}, 401)
            return
        try:
            data = self._form()
            self._handle_api_post(path, data)
        except Exception as exc:
            self._error(exc)

    def _handle_api_post(self, path: str, data: dict[str, str]) -> None:
        if path == "/api/quick/scan":
            english_source = data.get("english_source", "").strip()
            if not english_source:
                raise ValueError("Primary voice package is required")
            chs_value = data.get("chs_source", "").strip()
            reference_value = data.get("reference_source", "").strip()
            source_text_language = data.get("source_text_language", "en").strip() or "en"
            target_language = data.get("target_language", "zh-CN").strip() or "zh-CN"
            reference_language = data.get("reference_language", "auto").strip() or "auto"
            plan = quick_scan(
                Path(english_source),
                Path(chs_value) if chs_value else None,
                reference_source=Path(reference_value) if reference_value else None,
                source_text_language=source_text_language,
                target_language=target_language,
                reference_language=reference_language,
            )
            self._json({"ok": True, "plan": plan})
            return

        if path == "/api/quick/build":
            assert_no_active_build()
            english_source = data.get("english_source", "").strip()
            if not english_source:
                raise ValueError("Primary voice package is required")
            chs_value = data.get("chs_source", "").strip()
            reference_value = data.get("reference_source", "").strip()
            root_value = data.get("project_root", "").strip()
            config, plan = create_quick_project(
                Path(english_source),
                chs_source=Path(chs_value) if chs_value else None,
                reference_source=Path(reference_value) if reference_value else None,
                root=Path(root_value) if root_value else None,
                name=data.get("project_name", ""),
                audio_language=data.get("audio_language", "auto"),
                source_text_language=data.get("source_text_language", "en").strip() or "en",
                target_language=data.get("target_language", "zh-CN"),
                reference_language=data.get("reference_language", "auto"),
            )
            update_project(
                config,
                translation_token_budget=max(0, _int(data.get("translation_token_budget"), 0)),
                translation_budget_usd=max(0.0, _float(data.get("translation_budget_usd"), 0.0)),
                intro_gap=max(0.0, _float(data.get("intro_gap"), 5.0)),
                same_group_gap=max(0.0, _float(data.get("same_group_gap"), 0.40)),
                group_gap=max(0.0, _float(data.get("group_gap"), 1.20)),
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
            self._json({
                "ok": True,
                "job": job.id,
                "plan": plan,
                "project": project_summary(config),
            })
            return

        if path == "/api/project/create":
            config = create_project(
                Path(data["root"]),
                name=data["name"],
                index_csv=data["index_csv"],
                wav_source=data["wav_source"],
                output_dir=data.get("output_dir", "output"),
                bilingual_csv=data.get("bilingual_csv", ""),
                chs_source=data.get("chs_source", ""),
                glossary_path=data.get("glossary_path", ""),
                reference_source=data.get("reference_source", ""),
                audio_language=data.get("audio_language", "auto"),
                source_text_language=data.get("source_text_language", "en"),
                target_language=data.get("target_language", "zh-CN"),
                reference_language=data.get("reference_language", "auto"),
                remote_character=data.get("remote_character", ""),
            )
            _set_active(config)
            self._json({"ok": True, "project": project_summary(config)})
            return

        if path == "/api/project/open":
            config = load_project(Path(data["project_path"]))
            _set_active(config)
            self._json({"ok": True, "project": project_summary(config)})
            return

        if path == "/api/project/close":
            _clear_active()
            self._json({"ok": True, "project": None})
            return

        if path == "/api/project/clone":
            raw = data.get("project_path", "").strip()
            name = data.get("name", "").strip()
            root_value = data.get("root", "").strip()
            if not raw:
                raise ValueError("Project path is required")
            source = load_project(Path(raw))
            assert_project_idle(source.root)
            clone = clone_project(
                source,
                name=name,
                root=Path(root_value) if root_value else None,
            )
            _set_active(clone)
            self._json({
                "ok": True,
                "project": project_summary(clone),
                "recent_projects": recent_projects(12),
                "source_project": {
                    "name": source.name,
                    "root": source.root,
                },
            })
            return

        if path == "/api/project/forget":
            assert_no_active_build()
            raw = data.get("project_path", "").strip()
            if not raw:
                raise ValueError("Project path is required")
            root = Path(raw).expanduser().resolve()
            result = forget_project(root)
            if _active_root is not None and _active_root.resolve() == root:
                _clear_active()
            self._json({
                "ok": True,
                "result": result,
                "project": None if _active_root is None else project_summary(_active_config()),
                "recent_projects": recent_projects(12),
            })
            return

        if path == "/api/project/delete":
            assert_no_active_build()
            raw = data.get("project_path", "").strip()
            if not raw:
                raise ValueError("Project path is required")
            root = Path(raw).expanduser().resolve()
            was_active = _active_root is not None and _active_root.resolve() == root
            assert_project_idle(str(root))
            result = delete_project(root)
            result["deleted_job_records"] = delete_project_jobs(str(root))
            if was_active:
                _clear_active()
            self._json({
                "ok": True,
                "result": result,
                "project": None if _active_root is None else project_summary(_active_config()),
                "recent_projects": recent_projects(12),
            })
            return

        if path == "/api/project/relink":
            config = _active_config()
            assert_project_idle(config.root)
            role = data.get("role", "").strip()
            replacement = data.get("replacement_path", "").strip()
            if not replacement:
                raise ValueError("Replacement path is required")
            config, result = relink_project_source(config, role, Path(replacement))
            self._json({
                "ok": True,
                "result": result,
                "project": project_summary(config),
            })
            return

        if path == "/api/project/save":
            config = _active_config()
            update_project(
                config,
                name=data.get("name", config.name).strip(),
                index_csv=data.get("index_csv", config.index_csv).strip(),
                wav_source=data.get("wav_source", config.wav_source).strip(),
                output_dir=data.get("output_dir", "output").strip() or "output",
                bilingual_csv=data.get("bilingual_csv", "").strip(),
                chs_source=data.get("chs_source", "").strip(),
                glossary_path=data.get("glossary_path", "").strip(),
                reference_source=data.get("reference_source", "").strip(),
                audio_language=data.get("audio_language", "auto").strip() or "auto",
                source_text_language=data.get("source_text_language", "en").strip() or "en",
                target_language=data.get("target_language", "zh-CN").strip() or "zh-CN",
                reference_language=data.get("reference_language", "auto").strip() or "auto",
                update_candidates=data.get("update_candidates", "").strip(),
                remote_character=data.get("remote_character", "").strip(),
                remote_index_url=data.get("remote_index_url", "").strip() or config.remote_index_url,
                intro_gap=max(0.0, _float(data.get("intro_gap"), 5.0)),
                same_group_gap=max(0.0, _float(data.get("same_group_gap"), 0.40)),
                group_gap=_float(data.get("group_gap"), 1.20),
                make_flac=_bool(data.get("make_flac")),
                translate_missing=_bool(data.get("translate_missing")),
                translation_model=data.get("translation_model", "gpt-5.6-luna").strip() or "gpt-5.6-luna",
                translation_batch_size=max(1, _int(data.get("translation_batch_size"), 80)),
                translation_token_budget=max(0, _int(data.get("translation_token_budget"), 0)),
                translation_budget_usd=max(0.0, _float(data.get("translation_budget_usd"), 0.0)),
            )
            self._json({"ok": True, "project": project_summary(config)})
            return

        if path == "/api/project/build":
            assert_no_active_build()
            config = _active_config()
            paths = _project_paths(config)
            if paths["index"] is None or paths["wavs"] is None or paths["output"] is None:
                raise ValueError("Project index, WAV source, and output directory are required")
            runtime = dependency_status()
            if config.make_flac and not runtime.get("ffmpeg"):
                raise RuntimeError("FFmpeg is not installed or is not available on PATH")
            if config.translate_missing and not runtime.get("translation_api_key_configured"):
                raise RuntimeError(
                    "Translation API key is not configured; run "
                    "'python -m app.credentials configure --provider vapi'"
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
            self._json({"ok": True, "job": job.id})
            return

        if path == "/api/review/import":
            config = _active_config()
            paths = _project_paths(config)
            state, output = paths["state"], paths["output"]
            if state is None or output is None:
                raise ValueError("Project state or output directory is not configured")
            result = import_review_txt(
                data.get("review_text", ""),
                state_dir=state,
                output_path=output / "semantic_review_required.txt",
                target_language=config.target_language,
            )
            self._json({"ok": True, "result": result, "project": project_summary(config)})
            return

        if path == "/api/update/scan":
            config = _active_config()
            candidate_value = data.get("candidates_path", "").strip()
            if candidate_value:
                update_project(config, update_candidates=candidate_value)
            paths = _project_paths(config)
            output, candidates = paths["output"], paths["candidates"]
            if output is None or candidates is None:
                raise ValueError("Candidate update file is not configured")
            manifest = output / "manifest.json"
            if not manifest.is_file():
                raise FileNotFoundError("Build the project once before checking updates")
            result = classify(manifest, candidates)
            atomic_write_text(output / "update_plan.json", json.dumps(result, ensure_ascii=False, indent=2))
            self._json({"ok": True, "plan": result, "project": project_summary(config)})
            return

        if path == "/api/update/check-remote":
            config = _active_config()
            requested_character = data.get("character", "").strip()
            remote_url = data.get("remote_index_url", "").strip() or config.remote_index_url
            characters = remote_character_candidates(config, requested_character)
            if not characters:
                raise ValueError(
                    "Could not identify the remote-index character from this project; "
                    "run Quick Scan again or set it in Advanced settings"
                )
            update_project(config, remote_index_url=remote_url)
            output = resolve_project_path(config, config.output_dir)
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
                    rows = fetch_ai_hobbyist_index(candidate, remote_url)
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
                    url=remote_url,
                    queried_character=matched_character,
                )
                plan = exclude_applied_updates(plan, output / "update_apply_report.json")
                plan = exclude_indexed_updates(plan, resolve_project_path(config, config.index_csv))
                resolved_character = str(plan.get("character") or matched_character)
                plan["attempted_characters"] = attempted
                update_project(
                    config,
                    remote_character=resolved_character,
                    remote_index_url=remote_url,
                )
                output.mkdir(parents=True, exist_ok=True)
                atomic_write_text(output / "update_plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
                return plan

            job = create_job(
            "remote-update-check", run,
            project_root=config.root, project_name=config.name,
        )
            self._json({"ok": True, "job": job.id})
            return

        if path == "/api/update/apply-remote":
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
                resolved = resolve_targets(result_json, targets)
                report_progress("resolve", f"已可靠定位 {len(resolved['targets'])}/{len(targets)} 条新增语音", len(targets), len(targets))
                atomic_write_text(state / "incremental_resolution.json", json.dumps(resolved, ensure_ascii=False, indent=2))
                if not resolved["targets"]:
                    raise RuntimeError("新增条目均无法可靠映射到 Hugging Face 音频；未修改项目")
                incoming = generated / "incremental_audio"
                downloaded = download_resolved_audio(
                    resolved, incoming,
                    lambda current, total, name: report_progress("download", f"正在下载：{name}", current, total),
                )
                successful = {Path(row["filename"]).name for row in downloaded["completed"]}
                atomic_write_text(state / "incremental_download.json", json.dumps(downloaded, ensure_ascii=False, indent=2))
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
                    fields = list(existing_rows[0].keys()) if existing_rows else ["index", "group", "filename", "source", "source_detail", "english", "reference_text", "reference_language", "sha256"]
                known = {Path(str(row.get("filename", ""))).name for row in existing_rows}
                details = {Path(str(row.get("filename", ""))).name: row for row in targets}
                for filename in sorted(successful):
                    if filename not in known:
                        metadata = details.get(filename, {})
                        existing_rows.append({"index": str(len(existing_rows) + 1), "group": infer_group(Path(filename).stem), "filename": filename, "source": "huggingface", "source_detail": "simon3000/starrail-voice", "english": str(metadata.get("english", "")), "reference_text": "", "reference_language": "", "sha256": ""})
                updated_index = generated / "quick_index_incremental.csv"
                with updated_index.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                    writer.writeheader(); writer.writerows(existing_rows)
                update_project(config, wav_source=str(combined), index_csv=str(updated_index), wav_source_fingerprint="")
                report = {"resolved": len(resolved["targets"]), "unresolved": resolved["unresolved"], "downloaded_or_existing": len(downloaded["completed"]), "failed": downloaded["failed"], "project_updated": True, "rebuild_required": True, "applied_filenames": sorted(successful)}
                atomic_write_text(output / "update_apply_report.json", json.dumps(report, ensure_ascii=False, indent=2))
                report_progress("apply", "新增语音已应用，等待重新构建成品", 1, 1)
                return report

            job = create_job("remote-update-apply", run, with_progress=True, project_root=config.root, project_name=config.name)
            self._json({"ok": True, "job": job.id})
            return

        if path == "/api/output/open":
            config = _active_config()
            output = resolve_project_path(config, config.output_dir)
            if output is None:
                raise ValueError("Output directory is not configured")
            output.mkdir(parents=True, exist_ok=True)
            if shutil.which("termux-open"):
                subprocess.Popen(["termux-open", str(output)])
            elif shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", str(output)])
            else:
                raise RuntimeError("No supported file-manager opener found")
            self._json({"ok": True, "path": str(output)})
            return

        self._json({"ok": False, "error": "Not found"}, 404)


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Termux lightweight control server: http://{host}:{port}/")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Dependency-light local HTTP server for Termux")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
