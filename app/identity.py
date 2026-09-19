from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VARIANT_RE = re.compile(r"^(?P<base>.+)_(?P<variant>[fm])$", re.IGNORECASE)
_GROUP_RE = re.compile(r"^(archive|chapter\d+(?:_\d+)?|companion\d+(?:_\d+)?|side\d+(?:_\w+)?)_", re.IGNORECASE)


@dataclass(frozen=True)
class VoiceIdentity:
    filename: str
    stem: str
    logical_id: str
    variant: str
    group: str


def infer_group(stem: str) -> str:
    match = _GROUP_RE.match(stem)
    if match:
        return match.group(1)
    parts = stem.split("_")
    return parts[0] if parts else stem


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
