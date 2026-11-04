"""Read-only field acceptance for a finished HSR archive on Android/Termux.

Does not start jobs, create project files, print subtitle text, or report a
previously finished bundle as evidence that a new build has been attempted.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .archive_bundle_lifecycle import bundle_export_status
from .project import PROJECT_FILENAME, STATE_DIR

FIX_COMMIT = "19145b4ec7cba4c96da0a818c7ee35944cd0dd9e"
MKV_NAME = "HSR_Voice_Archive_Bundle.mkv"


def _read_mapping(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _source_revision() -> dict[str, object]:
    repo = Path(__file__).resolve().parent.parent
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        ancestor = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", FIX_COMMIT, "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return {
            "revision": head.stdout.strip() if head.returncode == 0 else "unknown",
            "pair_fix_present": (ancestor.returncode == 0
                                 if ancestor.returncode in (0, 1) else None),
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"revision": "unknown", "pair_fix_present": None}


def _last_build(root: Path, journal: Path) -> dict[str, str]:
    if journal.is_symlink() or not journal.is_file():
        return {"state": "not_found", "finished_at": "", "error_type": ""}
    try:
        rows = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "journal_unreadable", "finished_at": "", "error_type": ""}
    if not isinstance(rows, list):
        return {"state": "journal_unreadable", "finished_at": "", "error_type": ""}
    matches: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict) or item.get("kind") not in {"build", "quick-build"}:
            continue
        try:
            same_root = Path(str(item.get("project_root") or "")).expanduser().resolve() == root
        except (OSError, ValueError):
            same_root = False
        if same_root:
            matches.append(item)
    if not matches:
        return {"state": "not_found", "finished_at": "", "error_type": ""}
    latest = max(matches, key=lambda row: str(row.get("created_at") or ""))
    error = str(latest.get("error") or "").strip().splitlines()
    tail = error[-1] if error else ""
    match = re.search(r"\b([A-Za-z_][A-Za-z_0-9]*(?:Error|Exception)):", tail)
    return {
        "state": str(latest.get("state") or "unknown"),
        "finished_at": str(latest.get("finished_at") or ""),
        # Avoid printing traceback source paths, language content, or API data.
        "error_type": match.group(1) if match else ("other" if error else ""),
    }


def _probe_media(path: Path) -> list[dict[str, str]]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe_missing")
    try:
        run = subprocess.run(
            [executable, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=45, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffprobe_timeout") from exc
    if run.returncode:
        raise RuntimeError("ffprobe_failed")
    try:
        payload = json.loads(run.stdout)
        streams = payload["streams"]
        if not isinstance(streams, list):
            raise ValueError("streams not list")
        return [
            {"type": str(item.get("codec_type") or ""),
             "codec": str(item.get("codec_name") or "")}
            for item in streams if isinstance(item, dict)
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("ffprobe_invalid_output") from exc


def inspect_project(project_root: Path, *, journal: Path | None = None) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    config = _read_mapping(root / PROJECT_FILENAME)
    outcome: dict[str, Any] = {
        "schema_version": 1,
        **_source_revision(),
        "project_found": bool(config),
        "delivery_mode": str(config.get("delivery_mode") or "unknown"),
        "build_job": _last_build(root, journal or STATE_DIR / "jobs.json"),
        "build_report_found": False,
        "reported_voice_count": None,
        "artifact_present": False,
        "provenance_state": "not_checked",
        "streams": {"video": False, "flac": False, "ass": False, "srt": False},
        "artifact_check": "not_checked",
        "accepted": False,
    }
    if not config:
        return outcome

    configured_output = Path(str(config.get("output_dir") or "output")).expanduser()
    output = (root / configured_output) if not configured_output.is_absolute() else configured_output
    if output.is_symlink() or not output.is_dir():
        outcome["artifact_check"] = "output_missing_or_unsafe"
        return outcome
    report = _read_mapping(output / "build_report.json")
    outcome["build_report_found"] = bool(report)
    count = report.get("count_total")
    if isinstance(count, int) and not isinstance(count, bool):
        outcome["reported_voice_count"] = count

    complete = config.get("delivery_mode", "complete") != "flac_only"
    artifact = output / (MKV_NAME if complete else "continuous.flac")
    outcome["artifact_present"] = artifact.is_file() and not artifact.is_symlink() and artifact.stat().st_size > 0
    if not outcome["artifact_present"]:
        outcome["artifact_check"] = "artifact_missing_or_unsafe"
        return outcome

    if complete:
        try:
            # This performs real project provenance checks and may hash a
            # large MKV. It is read-only and never deletes source artifacts.
            status = bundle_export_status(output)
            outcome["provenance_state"] = str(status.get("state") or "unknown")
        except (OSError, ValueError, TypeError, RuntimeError):
            outcome["provenance_state"] = "verification_error"
    else:
        outcome["provenance_state"] = "not_applicable"

    try:
        streams = _probe_media(artifact)
    except (OSError, RuntimeError) as exc:
        outcome["artifact_check"] = str(exc) if str(exc) in {
            "ffprobe_missing", "ffprobe_timeout", "ffprobe_failed",
            "ffprobe_invalid_output",
        } else "ffprobe_error"
        return outcome

    codecs = {(s["type"], s["codec"]) for s in streams}
    outcome["streams"] = {
        "video": any(s["type"] == "video" for s in streams),
        "flac": ("audio", "flac") in codecs,
        "ass": ("subtitle", "ass") in codecs,
        "srt": ("subtitle", "subrip") in codecs,
    }
    outcome["artifact_check"] = "probed"
    stream_ok = (
        outcome["streams"]["flac"] and
        (not complete or all(outcome["streams"][kind] for kind in ("video", "ass", "srt")))
    )
    outcome["accepted"] = bool(
        outcome["pair_fix_present"] is True
        and outcome["build_job"]["state"] == "succeeded"
        and outcome["build_report_found"]
        and (not complete or outcome["provenance_state"] == "current")
        and stream_ok
    )
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only HSR build/media acceptance; outputs no dialogue or credentials",
    )
    parser.add_argument("--project", type=Path, required=True,
                        help="Existing HSR project root, not the output directory")
    args = parser.parse_args()
    result = inspect_project(args.project)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Nonzero means acceptance evidence is incomplete, not necessarily a bug.
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
