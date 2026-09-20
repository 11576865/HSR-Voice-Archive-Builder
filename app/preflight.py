from __future__ import annotations

import argparse
import importlib
import os
import shutil
import sys
from importlib import metadata
from typing import Any

MIN_PYTHON = (3, 11)
COMMON_DEPENDENCIES = (
    ("openpyxl", "openpyxl", None),
    ("defusedxml", "defusedxml", None),
)

DESKTOP_DEPENDENCIES = (
    ("fastapi", "fastapi", None),
    ("uvicorn", "uvicorn", None),
    ("python-multipart", "multipart", None),
)


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for token in value.split("."):
        digits = ""
        for ch in token:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _is_termux() -> bool:
    return bool(
        os.environ.get("TERMUX_VERSION")
        or os.environ.get("PREFIX", "").startswith("/data/data/com.termux")
    )


def _check_python_dependency(
    issues: list[str],
    versions: dict[str, str],
    distribution: str,
    module: str,
    minimum: tuple[int, ...] | None,
) -> None:
    try:
        importlib.import_module(module)
    except Exception as exc:
        issues.append(f"{distribution}: import failed ({type(exc).__name__}: {exc})")
        return
    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        version = "unknown"
    versions[distribution] = version
    if minimum and version != "unknown" and _version_tuple(version) < minimum:
        issues.append(
            f"{distribution}: {version} is older than "
            + ".".join(map(str, minimum))
        )


def dependency_status() -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    versions: dict[str, str] = {}
    termux = _is_termux()

    for distribution, module, minimum in COMMON_DEPENDENCIES:
        _check_python_dependency(issues, versions, distribution, module, minimum)
    if not termux:
        for distribution, module, minimum in DESKTOP_DEPENDENCIES:
            _check_python_dependency(issues, versions, distribution, module, minimum)

    # Translation uses the HTTPS Responses API directly through Python's
    # standard library. No OpenAI SDK / jiter / Rust dependency is required.
    openai_rest = True
    openai_api_key_configured = bool(os.environ.get("OPENAI_API_KEY", "").strip())

    seven_zip = shutil.which("7zz") or shutil.which("7z") or ""
    py7zr_ok = False
    try:
        importlib.import_module("py7zr")
        try:
            version = metadata.version("py7zr")
        except metadata.PackageNotFoundError:
            version = "unknown"
        versions["py7zr"] = version
        py7zr_ok = version == "unknown" or _version_tuple(version) >= (1, 1, 3)
        if not py7zr_ok:
            warnings.append(f"py7zr {version} is older than 1.1.3 and will not be used")
    except Exception:
        pass

    if termux:
        if not seven_zip:
            issues.append(
                "7-Zip CLI not found. Install Termux package '7zip' (or legacy 'p7zip')."
            )
        warnings.append(
            "Termux/Android detected: the lightweight stdlib HTTP server and native 7-Zip "
            "are used instead of FastAPI/Pydantic/py7zr because those dependency chains "
            "are not reliably installable on Android. GPT translation uses direct HTTPS REST."
        )
        warnings.append(
            "Android may terminate long CPU-heavy/background jobs; interrupted jobs are "
            "journaled but FLAC encoding restarts from the beginning."
        )
    elif not py7zr_ok and not seven_zip:
        issues.append("7z extraction requires py7zr>=1.1.3 or a native 7zz/7z executable")

    if sys.version_info < MIN_PYTHON:
        issues.append(
            f"Python {sys.version_info.major}.{sys.version_info.minor} is unsupported; "
            f"need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+"
        )

    ffmpeg = shutil.which("ffmpeg") or ""
    if not ffmpeg:
        warnings.append("FFmpeg not found: FLAC building is unavailable until it is installed.")

    return {
        "ok": not issues,
        "python": sys.version.split()[0],
        "versions": versions,
        "issues": issues,
        "warnings": warnings,
        "ffmpeg": ffmpeg,
        "seven_zip": seven_zip,
        "openai_rest": openai_rest,
        "openai_api_key_configured": openai_api_key_configured,
        "termux": termux,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Runtime preflight for HSR Voice Archive Builder")
    p.add_argument("--deps-only", action="store_true")
    args = p.parse_args()

    status = dependency_status()
    if status["issues"]:
        print("Dependency check failed:")
        for issue in status["issues"]:
            print(f"- {issue}")
        raise SystemExit(1)

    print(f"Python {status['python']}: OK")
    print("Python dependencies: OK")
    if status["seven_zip"]:
        print(f"7-Zip: {status['seven_zip']}")
    if not args.deps_only:
        if status["ffmpeg"]:
            print(f"FFmpeg: {status['ffmpeg']}")
        else:
            print("FFmpeg: not found (FLAC building will be unavailable)")
        for warning in status["warnings"]:
            print(f"Warning: {warning}")


if __name__ == "__main__":
    main()
