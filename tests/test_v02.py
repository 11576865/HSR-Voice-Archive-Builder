from __future__ import annotations

import csv
import json
import tempfile
import unittest
import wave
from pathlib import Path

from app.diff import classify
from app.identity import parse_voice_identity
from app.pipeline import build_project_v02


def write_wav(path: Path, frames: int, sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * frames)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


class V02Tests(unittest.TestCase):
    def test_optional_bilingual_and_official_lab(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            labs = root / "labs"
            out = root / "out"
            labs.mkdir()
            write_wav(wavs / "archive_evanescia_1.wav", 8000)
            (labs / "archive_evanescia_1.lab").write_text("官方中文", encoding="utf-8")
            index = root / "index.csv"
            write_csv(index, ["filename", "english"], [{"filename": "archive_evanescia_1.wav", "english": "Hello."}])
            report = build_project_v02(index, wavs, out, chs_source=labs, make_flac=False)
            self.assertEqual(report["count_total"], 1)
            self.assertEqual(report["count_official_chs_lab"], 1)
            payload = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["entries"][0]["logical_id"], "archive_evanescia_1")

    def test_variant_identity_and_diff(self) -> None:
        ident = parse_voice_identity("chapter5_3_evanescia_125_f.wav")
        self.assertEqual((ident.logical_id, ident.variant), ("chapter5_3_evanescia_125", "f"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"entries": [{"filename": "chapter5_3_evanescia_125_f.wav"}]}), encoding="utf-8")
            pending = root / "pending.txt"
            pending.write_text("chapter5_3_evanescia_125.wav\nchapter5_42_evanescia_101.wav\n", encoding="utf-8")
            result = classify(manifest, pending)
            self.assertEqual(result["counts"]["variant_of_existing"], 1)
            self.assertEqual(result["counts"]["new_logical"], 1)


if __name__ == "__main__":
    unittest.main()
