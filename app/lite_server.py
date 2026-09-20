from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from email.parser import BytesParser
from email.policy import default as email_policy
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .builder import atomic_write_text
from .diff import classify
from .jobs import create_job, get_job, recent_jobs
from .pipeline import build_project_v02
from .preflight import dependency_status
from .project import (
    ProjectConfig,
    create_project,
    last_project_root,
    load_project,
    project_summary,
    resolve_project_path,
    update_project,
)
from .remote_index import fetch_ai_hobbyist_index, remote_update_plan
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


def _project_paths(config: ProjectConfig) -> dict[str, Path | None]:
    return {
        "index": resolve_project_path(config, config.index_csv),
        "wavs": resolve_project_path(config, config.wav_source),
        "bilingual": resolve_project_path(config, config.bilingual_csv),
        "chs": resolve_project_path(config, config.chs_source),
        "output": resolve_project_path(config, config.output_dir),
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
    server_version = "HSRVoiceLite/0.5"

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
                "version": "0.6-termux-lite",
                "processing_mode": "local-first",
                "lan_control": lan_mode(),
                "project": project,
                "jobs": recent_jobs(8),
                "runtime": dependency_status(),
            })
            return
        if path == "/api/jobs":
            self._json({"ok": True, "jobs": recent_jobs(20)})
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
        if path == "/api/project/create":
            config = create_project(
                Path(data["root"]),
                name=data["name"],
                index_csv=data["index_csv"],
                wav_source=data["wav_source"],
                output_dir=data.get("output_dir", "output"),
                bilingual_csv=data.get("bilingual_csv", ""),
                chs_source=data.get("chs_source", ""),
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
                update_candidates=data.get("update_candidates", "").strip(),
                remote_character=data.get("remote_character", "").strip(),
                remote_index_url=data.get("remote_index_url", "").strip() or config.remote_index_url,
                same_group_gap=_float(data.get("same_group_gap"), 0.40),
                group_gap=_float(data.get("group_gap"), 1.20),
                make_flac=_bool(data.get("make_flac")),
                translate_missing=_bool(data.get("translate_missing")),
                translation_model=data.get("translation_model", "gpt-5.6-luna").strip() or "gpt-5.6-luna",
                translation_batch_size=max(1, _int(data.get("translation_batch_size"), 80)),
            )
            self._json({"ok": True, "project": project_summary(config)})
            return

        if path == "/api/project/build":
            config = _active_config()
            paths = _project_paths(config)
            if paths["index"] is None or paths["wavs"] is None or paths["output"] is None:
                raise ValueError("Project index, WAV source, and output directory are required")
            runtime = dependency_status()
            if config.make_flac and not runtime.get("ffmpeg"):
                raise RuntimeError("FFmpeg is not installed or is not available on PATH")
            if config.translate_missing and not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is not set")

            def run():
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
                )

            job = create_job("build", run)
            self._json({"ok": True, "job": job.id})
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
            character = data.get("character", "").strip() or config.remote_character
            remote_url = data.get("remote_index_url", "").strip() or config.remote_index_url
            if not character:
                raise ValueError("Set a remote character filter first")
            update_project(config, remote_character=character, remote_index_url=remote_url)
            output = resolve_project_path(config, config.output_dir)
            if output is None:
                raise ValueError("Output directory is not configured")
            manifest = output / "manifest.json"
            if not manifest.is_file():
                raise FileNotFoundError("Build the project once before checking remote updates")

            def run():
                rows = fetch_ai_hobbyist_index(character, remote_url)
                plan = remote_update_plan(manifest, rows)
                output.mkdir(parents=True, exist_ok=True)
                atomic_write_text(output / "update_plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
                return plan

            job = create_job("remote-update-check", run)
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
