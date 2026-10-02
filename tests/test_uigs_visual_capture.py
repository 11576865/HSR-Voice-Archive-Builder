from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_uigs_visual_capture_covers_all_hsr_surfaces():
    contract = json.loads((ROOT / ".uigs" / "ui-visual-capture.json").read_text(encoding="utf-8"))
    inventory = json.loads((ROOT / ".uigs" / "ui-surfaces.json").read_text(encoding="utf-8"))
    captured = {sid for capture in contract["captures"] for sid in capture["surface_ids"]}
    declared = {surface["id"] for surface in inventory["surfaces"]}
    assert captured == declared
