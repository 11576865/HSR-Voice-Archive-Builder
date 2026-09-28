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

_GAME_ALIASES = {
    "hsr": "honkai-star-rail",
    "star-rail": "honkai-star-rail",
    "honkai-star-rail": "honkai-star-rail",
    "genshin": "genshin-impact",
    "genshin-impact": "genshin-impact",
}

_GAME_PROVIDERS: dict[str, dict[str, Any]] = {
    "honkai-star-rail": {
        "game_id": "honkai-star-rail",
        "label": "Honkai: Star Rail",
        "remote_updates_supported": True,
        "index_kind": "xlsx",
        "index_urls": {
            "en": "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/EN.xlsx",
            "zh-CN": "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/CHS.xlsx",
            "ja": "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/JP.xlsx",
            "ko": "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/KR.xlsx",
        },
        "audio_dataset": "simon3000/starrail-voice",
    },
    "genshin-impact": {
        "game_id": "genshin-impact",
        "label": "Genshin Impact",
        "remote_updates_supported": True,
        "index_kind": "json",
        "index_urls": {
            "en": "https://raw.githubusercontent.com/AI-Hobbyist/Genshin_Voice_Sorting_Scripts/main/Indexs/all/EN.json",
            "zh-CN": "https://raw.githubusercontent.com/AI-Hobbyist/Genshin_Voice_Sorting_Scripts/main/Indexs/all/CHS.json",
            "ja": "https://raw.githubusercontent.com/AI-Hobbyist/Genshin_Voice_Sorting_Scripts/main/Indexs/all/JP.json",
            "ko": "https://raw.githubusercontent.com/AI-Hobbyist/Genshin_Voice_Sorting_Scripts/main/Indexs/all/KR.json",
        },
        "audio_dataset": "simon3000/genshin-voice",
    },
}

_LANGUAGE_ALIASES = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "chs": "zh-CN",
    "cn": "zh-CN",
    "jp": "ja",
    "ja-jp": "ja",
    "kr": "ko",
    "ko-kr": "ko",
    "en-us": "en",
}


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
            "remote_updates_supported": True,
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


def normalize_game_id(game_id: str) -> str:
    key = str(game_id or "").strip().casefold()
    return _GAME_ALIASES.get(key, key or "generic")


def game_provider(game_id: str) -> dict[str, Any]:
    normalized = normalize_game_id(game_id)
    provider = _GAME_PROVIDERS.get(normalized)
    if provider is None:
        return {
            "game_id": normalized,
            "label": "Generic voice package",
            "remote_updates_supported": False,
            "index_kind": "",
            "index_urls": {},
            "audio_dataset": "",
        }
    return {
        **provider,
        "index_urls": dict(provider.get("index_urls", {})),
    }


def normalize_language(language: str) -> str:
    raw = str(language or "en").strip()
    return _LANGUAGE_ALIASES.get(raw.casefold(), raw)


def game_index_url(game_id: str, language: str) -> str:
    provider = game_provider(game_id)
    key = normalize_language(language)
    urls = provider.get("index_urls", {})
    if key not in urls:
        raise ValueError(
            f"No built-in {provider.get('label', game_id)} remote index is configured "
            f"for language {language!r}; supported source languages are en, zh-CN, ja, ko"
        )
    return str(urls[key])


def game_index_kind(game_id: str) -> str:
    return str(game_provider(game_id).get("index_kind", ""))


def game_audio_dataset(game_id: str) -> str:
    return str(game_provider(game_id).get("audio_dataset", ""))


def remote_updates_supported(game_id: str) -> bool:
    return bool(game_provider(game_id).get("remote_updates_supported"))
