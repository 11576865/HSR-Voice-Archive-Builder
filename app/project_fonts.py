"""Project-owned subtitle font assets.

Uploaded SFNT files remain within the selected project's output/subtitle_fonts
folder. Do not expose client paths or install fonts globally.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import struct
from pathlib import Path

from subtitle_layout.fonts import validate_ass_font_name

MAX_FONT_BYTES = 12 * 1024 * 1024
MAX_PROJECT_FONTS = 32
EXTENSIONS = {".ttf", ".otf", ".ttc"}
SFNT_MAGIC = {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}


def font_asset_directory(output: Path) -> Path:
    return Path(output) / "subtitle_fonts"


def _font_name_records(data: bytes, allowed_ids: set[int]) -> list[tuple[int, int, str]]:
    """Read validated SFNT name records from the first face of TTF/OTF/TTC."""
    offset = 0
    if data[:4] == b"ttcf":
        if len(data) < 16:
            raise ValueError("Truncated TrueType collection")
        count = struct.unpack_from(">I", data, 8)[0]
        if not 1 <= count <= 128:
            raise ValueError("Invalid TTC face count")
        offset = struct.unpack_from(">I", data, 12)[0]
    if offset < 0 or offset + 12 > len(data) or data[offset:offset + 4] not in SFNT_MAGIC:
        raise ValueError("Not a valid SFNT/OTF header")
    count = struct.unpack_from(">H", data, offset + 4)[0]
    if count < 1 or count > 256 or offset + 12 + count * 16 > len(data):
        raise ValueError("Invalid SFNT table directory")
    table = None
    for i in range(count):
        item = offset + 12 + i * 16
        tag, _, table_offset, table_size = struct.unpack_from(">4sIII", data, item)
        if tag == b"name":
            table = (table_offset, table_size)
            break
    if table is None:
        raise ValueError("Font has no name table")
    base, size = table
    if base + size > len(data) or size < 6:
        raise ValueError("Invalid font name table length")
    _, entries, storage = struct.unpack_from(">HHH", data, base)
    if entries > 4096 or 6 + entries * 12 > size:
        raise ValueError("Invalid font name records")
    found: list[tuple[int, int, str]] = []
    for i in range(entries):
        row = base + 6 + i * 12
        platform, encoding, language, name_id, length, string_offset = struct.unpack_from(">HHHHHH", data, row)
        if name_id not in allowed_ids or not length or length > 512:
            continue
        start = base + storage + string_offset
        if start < base or start + length > base + size:
            continue
        blob = data[start:start + length]
        try:
            name = blob.decode("utf-16-be" if platform in (0, 3) else "mac_roman").strip()
            name = validate_ass_font_name(name)
        except (UnicodeError, ValueError):
            continue
        found.append((name_id, platform, name))
    return found


def _font_family(data: bytes) -> str:
    """Read the SFNT family name (typographic family 16, then family 1)."""
    found = [
        (0 if name_id == 16 else 1, 0 if platform == 3 else 1, family)
        for name_id, platform, family in _font_name_records(data, {1, 16})
    ]
    if not found:
        raise ValueError("Font has no readable family name")
    found.sort(key=lambda row: row[:2])
    return found[0][2]


def project_font_face_names(font_file: Path) -> set[str]:
    """Return names embedded in the imported SFNT, including PostScript name 6.

    libass sometimes logs a PostScript face (e.g. HYRunYuan-55W) instead of
    the family or the content-addressed filename. Parse the uploaded binary;
    don't accept an arbitrary common-prefix face as proof of its identity.
    """
    try:
        if (font_file.is_symlink() or not font_file.is_file()
                or not 0 < font_file.stat().st_size <= MAX_FONT_BYTES):
            return set()
        return {
            name for _, _, name in _font_name_records(
                font_file.read_bytes(), {1, 4, 6, 16}
            )
        }
    except (OSError, ValueError, struct.error):
        return set()


def list_project_fonts(output: Path) -> list[dict[str, object]]:
    root = font_asset_directory(output)
    if root.is_symlink() or not root.is_dir():
        return []
    result = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in EXTENSIONS:
            continue
        if path.stat().st_size > MAX_FONT_BYTES:
            continue
        try:
            data = path.read_bytes()
            family = _font_family(data)
        except (OSError, ValueError, struct.error):
            continue
        result.append({"filename": path.name, "family": family, "bytes": len(data)})
    return result


def project_font_for_family(output: Path, family: str) -> Path | None:
    wanted = validate_ass_font_name(family).casefold()
    root = font_asset_directory(output)
    for record in list_project_fonts(output):
        if str(record["family"]).casefold() == wanted:
            return root / str(record["filename"])
    return None


def import_project_font(output: Path, filename: str, encoded: str) -> dict[str, object]:
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in EXTENSIONS:
        raise ValueError("Only TTF, OTF and TTC fonts are accepted")
    if len(encoded) > ((MAX_FONT_BYTES + 2) // 3) * 4 + 16:
        raise ValueError("Font exceeds 12 MiB input limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Font payload is not valid base64") from exc
    if not 0 < len(data) <= MAX_FONT_BYTES:
        raise ValueError("Font must be between 1 byte and 12 MiB")
    family = _font_family(data)
    root = font_asset_directory(output)
    if root.is_symlink():
        raise ValueError("Font asset directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(data).hexdigest()
    name = token[:24] + suffix
    destination = root / name
    if not destination.exists() and len(list_project_fonts(output)) >= MAX_PROJECT_FONTS:
        raise ValueError("Project font asset limit (32) reached")
    if destination.is_symlink():
        raise ValueError("Invalid existing font destination")
    if not destination.exists():
        temporary = root / (name + ".part")
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    # Exactly one selectable face per family. Leaving two content-addressed
    # files with the same internal family in fontsdir makes Fontconfig/libass
    # choose whichever it indexes first (often the old import). Only retire
    # matching family files AFTER the new validated asset is durable.
    replaced = []
    for previous in list_project_fonts(output):
        if (str(previous["family"]).casefold() != family.casefold()
                or previous["filename"] == name):
            continue
        old_path = root / str(previous["filename"])
        if old_path.is_symlink():
            raise ValueError("Existing project font is an unsafe symlink")
        old_path.unlink(missing_ok=True)
        replaced.append(str(previous["filename"]))
    return {"filename": name, "family": family, "bytes": len(data),
            "sha256": token, "preview_available": True,
            "replaced_files": replaced}
