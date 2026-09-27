from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

# Confirmed AI-Hobbyist Genshin character-package family. Keep this parser
# deliberately conservative: unknown filename families should remain generic
# instead of being guessed as Genshin.
_GENSHIN_ANECDOTE_RE = re.compile(
    r"^vo_(?P<family>anecdote)_(?P<section>\d+)_(?P<character>[a-z0-9]+)_(?P<sequence>\d+)$",
    re.IGNORECASE,
)

_HSR_HINT_RE = re.compile(
    r"^(?:archive(?:_vo_avatar)?|chapter\d+(?:_\d+)?|companion\d+(?:_\d+)?|side\d+(?:_\w+)?)_",
    re.IGNORECASE,
)


def genshin_voice_parts(filename_or_stem: str) -> dict[str, str] | None:
    stem = Path(str(filename_or_stem or "")).stem
    match = _GENSHIN_ANECDOTE_RE.match(stem)
    if not match:
        return None
    parts = {key: value for key, value in match.groupdict().items()}
    parts["group"] = f"{parts['family'].lower()}_{parts['section']}"
    parts["game_id"] = "genshin-impact"
    return parts


def detect_game_profile(filenames: Iterable[str]) -> dict[str, Any]:
    names = [Path(str(name)).name for name in filenames if str(name).strip()]
    total = len(names)
    if not total:
        return {
            "game_id": "generic",
            "label": "Generic voice package",
            "confidence": "none",
            "matched": 0,
            "total": 0,
            "evidence": "no WAV filenames",
            "remote_updates_supported": False,
        }

    genshin_matches = [name for name in names if genshin_voice_parts(name)]
    if genshin_matches:
        share = len(genshin_matches) / total
        return {
            "game_id": "genshin-impact",
            "label": "Genshin Impact",
            "confidence": "high" if share >= 0.75 else "medium",
            "matched": len(genshin_matches),
            "total": total,
            "evidence": "vo_anecdote_<section>_<character>_<sequence>",
            # Local WAV+LAB archives are supported. The Genshin JSON remote
            # index/downloader is intentionally a separate provider task.
            "remote_updates_supported": False,
        }

    hsr_matches = [
        name for name in names
        if _HSR_HINT_RE.match(Path(name).stem)
    ]
    if hsr_matches:
        share = len(hsr_matches) / total
        return {
            "game_id": "honkai-star-rail",
            "label": "Honkai: Star Rail",
            "confidence": "high" if share >= 0.75 else "medium",
            "matched": len(hsr_matches),
            "total": total,
            "evidence": "archive/chapter/companion/side filename family",
            "remote_updates_supported": True,
        }

    return {
        "game_id": "generic",
        "label": "Generic voice package",
        "confidence": "low",
        "matched": 0,
        "total": total,
        "evidence": "no game-specific filename family recognized",
        "remote_updates_supported": False,
    }


def remote_updates_supported(game_id: str) -> bool:
    return str(game_id or "").strip().casefold() in {
        "honkai-star-rail",
        "hsr",
        "star-rail",
    }
