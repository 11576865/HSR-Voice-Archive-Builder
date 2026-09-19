from __future__ import annotations

import argparse
import importlib
import os
import shutil
import sys
from importlib import metadata
from typing import Any

MIN_PYTHON = (3, 11)
DEPENDENCIES = (
    ("fastapi", "fastapi", None),
    ("uvicorn", "uvicorn", None),
    ("python-multipart", "multipart", None),
    ("py7zr", "py7zr", (1, 1, 3)),
    ("openai", "openai", None),
    ("openpyxl", "openpyxl", None),
    ("defusedxml", "defusedxml", None),
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


def dependency_status() -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    versions: dict[str, str] = {}
    for distribution, module, minimum in DEPENDENCIES:
        try:
            importlib.import_module(module)
        except Exception as exc:
            issues.append(f"{distribution}: import failed ({type(exc).__name__}: {exc})")
            continue
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

    if sys.version_info < MIN_PYTHON:
        issues.append(
            f"Python {sys.version_info.major}.{sys.version_info.minor} is unsupported; "
            f"need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+"
        )

    ffmpeg = shutil.which("ffmpeg") or ""
    if not ffmpeg:
        warnings.append("FFmpeg not found: FLAC building is unavailable until it is installed.")

    termux = bool(os.environ.get("TERMUX_VERSION") or os.environ.get("PREFIX", "").startswith("/data/data/com.termux"))
    if termux:
        warnings.append(
            "Termux/Android detected: Android may terminate long CPU-heavy/background jobs; "
            "interrupted jobs are journaled but FLAC encoding restarts from the beginning."
        )

    return {
        "ok": not issues,
        "python": sys.version.split()[0],
        "versions": versions,
        "issues": issues,
        "warnings": warnings,
        "ffmpeg": ffmpeg,
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
    if not args.deps_only:
        if status["ffmpeg"]:
            print(f"FFmpeg: {status['ffmpeg']}")
        else:
            print("FFmpeg: not found (FLAC building will be unavailable)")


if __name__ == "__main__":
    main()
