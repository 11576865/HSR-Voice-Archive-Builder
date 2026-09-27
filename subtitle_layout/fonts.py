from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path


def validate_ass_font_name(value: object) -> str:
    """Return a safe ASS font family name or raise ValueError."""
    name = str(value or "").strip()
    if not name:
        raise ValueError("Subtitle font name must not be empty")
    if len(name) > 128:
        raise ValueError("Subtitle font name is too long")
    if "," in name or "\n" in name or "\r" in name:
        raise ValueError("Subtitle font name must not contain commas or line breaks")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("Subtitle font name contains control characters")
    return name


def _font_roots() -> list[Path]:
    roots: list[Path] = []
    windir = os.environ.get("WINDIR")
    if windir:
        roots.append(Path(windir) / "Fonts")
    roots.extend(
        [
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
            Path.home() / ".local" / "share" / "fonts",
        ]
    )
    prefix = os.environ.get("PREFIX")
    if prefix:
        roots.append(Path(prefix) / "share" / "fonts")
    seen: set[str] = set()
    result: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen and root.is_dir():
            seen.add(key)
            result.append(root)
    return result


def _normalized_name(value: str) -> str:
    return "".join(ch.casefold() for ch in value if ch.isalnum())


@functools.lru_cache(maxsize=64)
def resolve_font_path(font_name: str) -> str | None:
    """Resolve a font family to a concrete file when the host can do so."""
    name = validate_ass_font_name(font_name)

    fc_match = shutil.which("fc-match")
    if fc_match:
        try:
            completed = subprocess.run(
                [fc_match, "-f", "%{file}\n", "--", name],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            candidate = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        except (OSError, subprocess.SubprocessError):
            pass

    wanted = _normalized_name(name)
    if not wanted:
        return None
    extensions = {".ttf", ".otf", ".ttc"}
    best: Path | None = None
    for root in _font_roots():
        try:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.casefold() not in extensions:
                    continue
                stem = _normalized_name(path.stem)
                if stem == wanted:
                    return str(path.resolve())
                if best is None and (wanted in stem or stem in wanted):
                    best = path
        except OSError:
            continue
    return str(best.resolve()) if best is not None else None
