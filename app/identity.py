from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .game_profiles import genshin_voice_parts

_VARIANT_RE = re.compile(r"^(?P<base>.+)_(?P<variant>[fm])$", re.IGNORECASE)
_CHAPTER_RE = re.compile(r"^chapter(?P<major>\d+)(?:_(?P<section>\d+))?(?:_|$)", re.IGNORECASE)
_COMPANION_RE = re.compile(r"^companion(?P<major>\d*)(?:_(?P<section>\d+))?(?:_|$)", re.IGNORECASE)
_SIDE_RE = re.compile(r"^side(?P<major>\d+|x)?(?:_|$)", re.IGNORECASE)
_FINALITY_RE = re.compile(r"^(?P<group>chapterfinality\d*|finality)(?:_|$)", re.IGNORECASE)
_ARCHIVE_RE = re.compile(r"^archive(?:_|$)", re.IGNORECASE)
_ANECDOTE_RE = re.compile(r"^anecdote_(?P<section>\d+)(?:_|$)", re.IGNORECASE)


@dataclass(frozen=True)
class VoiceIdentity:
    filename: str
    stem: str
    logical_id: str
    variant: str
    group: str


@dataclass(frozen=True)
class GroupClassification:
    group: str
    major_group: str
    confidence: str
    method: str


def _strip_known_voice_prefixes(stem: str) -> str:
    value = str(stem or "").strip()
    # HSR exports can add one or more technical prefixes before the semantic
    # group (for example vo_chapter... or Ev_archive...). Strip only prefixes
    # we have observed and understand; do not strip arbitrary first tokens.
    while True:
        lowered = value.casefold()
        if lowered.startswith("ev_"):
            value = value[3:]
            continue
        if lowered.startswith("vo_"):
            value = value[3:]
            continue
        return value


def _known_group(value: str) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    anecdote = _ANECDOTE_RE.match(raw)
    if anecdote:
        group = f"anecdote_{anecdote.group('section')}"
        return group.lower(), group.lower()

    if _ARCHIVE_RE.match(raw):
        return "archive", "archive"

    finality = _FINALITY_RE.match(raw)
    if finality:
        group = finality.group("group").lower()
        return group, group

    chapter = _CHAPTER_RE.match(raw)
    if chapter:
        major = chapter.group("major")
        section = chapter.group("section")
        group = f"chapter{major}" + (f"_{section}" if section else "")
        return group.lower(), f"chapter{major}".lower()

    companion = _COMPANION_RE.match(raw)
    if companion:
        major = companion.group("major") or ""
        section = companion.group("section")
        group = "companion" + major + (f"_{section}" if section else "")
        major_group = "companion" + major
        return group.lower(), major_group.lower()

    side = _SIDE_RE.match(raw)
    if side:
        major = side.group("major") or ""
        group = "side" + major
        return group.lower(), group.lower()

    return None


def _known_group_from_filename(stem: str) -> tuple[str, str, str] | None:
    genshin = genshin_voice_parts(stem)
    if genshin:
        group = str(genshin["group"]).lower()
        return group, group, "genshin_filename_schema"

    normalized = _strip_known_voice_prefixes(stem)
    known = _known_group(normalized)
    if known:
        return known[0], known[1], "filename_schema"
    return None


def infer_group(stem: str) -> str:
    known = _known_group_from_filename(stem)
    if known:
        return known[0]
    parts = str(stem or "").split("_")
    return parts[0] if parts else str(stem or "")


def classify_major_group(
    filename_or_member: str,
    group: str | None = None,
) -> GroupClassification:
    """Classify a voice line for chapter/major-group ordering.

    confirmed means the filename or declared group matches a structure we
    explicitly understand. inferred means a package directory or an explicit
    non-automatic index group supplied useful grouping information. unknown
    means the old first-token fallback would be the only evidence, so chapter
    sorting must not pretend that token is a real story group.
    """

    value = str(filename_or_member or "").replace("\\", "/")
    path = Path(value)
    stem = path.stem

    direct = _known_group_from_filename(stem)
    if direct:
        return GroupClassification(
            group=direct[0],
            major_group=direct[1],
            confidence="confirmed",
            method=direct[2],
        )

    declared = str(group or "").strip()
    declared_known = _known_group(_strip_known_voice_prefixes(declared))
    if declared_known:
        return GroupClassification(
            group=declared_known[0],
            major_group=declared_known[1],
            confidence="confirmed",
            method="declared_group_schema",
        )

    # Structured archive paths can preserve chapter information even when the
    # basename itself is opaque. Treat this as inferred rather than confirmed.
    for part in reversed(path.parts[:-1]):
        known = _known_group(_strip_known_voice_prefixes(part))
        if known:
            return GroupClassification(
                group=known[0],
                major_group=known[1],
                confidence="inferred",
                method="package_path",
            )

    # A manually authored CSV may declare a useful custom group. Trust it as
    # inferred only when it differs from the automatic first-token fallback.
    # This prevents filenames such as vo_100101_01.wav from becoming a fake
    # "vo" chapter merely because infer_group() returned its first token.
    automatic = infer_group(stem)
    if (
        declared
        and declared.casefold() != "unknown"
        and declared.casefold() != automatic.casefold()
    ):
        return GroupClassification(
            group=declared.casefold(),
            major_group=declared.casefold(),
            confidence="inferred",
            method="explicit_index_group",
        )

    return GroupClassification(
        group="",
        major_group="",
        confidence="unknown",
        method="unrecognized",
    )


def parse_voice_identity(filename: str, group: str | None = None) -> VoiceIdentity:
    stem = Path(filename).stem
    match = _VARIANT_RE.match(stem)
    if match:
        logical_id = match.group("base")
        variant = match.group("variant").lower()
    else:
        logical_id = stem
        variant = ""
    return VoiceIdentity(
        filename=Path(filename).name,
        stem=stem,
        logical_id=logical_id,
        variant=variant,
        group=group or infer_group(logical_id),
    )
