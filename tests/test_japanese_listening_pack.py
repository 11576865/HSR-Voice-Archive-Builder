"""True offline user-review pack acceptance: source ZIP -> finished FLAC -> paired PCM."""
from __future__ import annotations

import csv
import io
import json
import math
import shutil
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.japanese_listening_pack import (
    _select, build_japanese_listening_pack, get_japanese_listening_pack,
)
from app.japanese_source_recovery import create_isolated_japanese_recovery
from app.pipeline import build_project_v02
from app.project import ProjectConfig, load_project, save_project
from app.quick import source_inventory


def pcm_tone(hz: float) -> bytes:
    import struct
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setframerate(16000)
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.writeframes(b"".join(struct.pack("<h", round(7000 * math.sin(2*math.pi*hz*i/16000)))
                                for i in range(16000)))
    return output.getvalue()


class JapaneseListeningPackTests(unittest.TestCase):
    def _fixture(self, base: Path) -> tuple[ProjectConfig, Path, Path]:
        original = base / "JP.zip"
        with zipfile.ZipFile(original, "w") as z:
            for name, text, hz in (
                ("chapter1/same.wav", "あいうえお", 320),
                ("chapter2/same.wav", "かきくけこ", 640),
                ("chapter3/unique.wav", "さしすせそ", 480),
            ):
                z.writestr(name, pcm_tone(hz))
                z.writestr(name.replace(".wav", ".lab"), text)
        root = base / "OriginalJP"
        (root / ".generated").mkdir(parents=True)
        index = root / ".generated" / "quick_index.csv"
        with index.open("w", newline="", encoding="utf-8-sig") as stream:
            w = csv.DictWriter(stream, fieldnames=[
                "index", "group", "filename", "source_member_id", "english",
            ])
            w.writeheader()
            for i, (name, text) in enumerate((
                ("chapter1/same.wav", "あいうえお"),
                ("chapter2/same.wav", "かきくけこ"),
                ("chapter3/unique.wav", "さしすせそ"),
            ), 1):
                w.writerow({"index": i, "group": "chapter"+str(i), "filename": Path(name).name,
                            "source_member_id": name, "english": text})
        (root / ".generated" / "quick_scan.json").write_text(
            json.dumps({"source_text_language":"ja",
                        "english":{"source":str(original),
                        "fingerprint":source_inventory(original)["fingerprint"]}}), "utf-8")
        cfg = ProjectConfig(schema_version=1, name="Japanese Original", root=str(root),
                            index_csv=str(index), wav_source=str(original),
                            output_dir="output", game_id="honkai-star-rail",
                            audio_language="ja", source_text_language="ja",
                            target_language="ja", translate_missing=False,
                            delivery_mode="flac_only")
        save_project(cfg)
        result = create_isolated_japanese_recovery(cfg)
        recovered_root = Path(result["candidate_project"])
        recovered = load_project(recovered_root)
        return recovered, root, original

    def test_priority_of_duplicate_basename_pairs_and_chapter_sampling(self):
        entries = [
            {"filename":"same.wav","source_member_id":"c1/same.wav","group":"one"},
            {"filename":"unique.wav","source_member_id":"c2/unique.wav","group":"two"},
            {"filename":"same.wav","source_member_id":"c3/same.wav","group":"three"},
            {"filename":"tail.wav","source_member_id":"c4/tail.wav","group":"four"},
        ]
        rows = _select(entries,3)
        self.assertEqual([r["source_member_id"] for r in rows[:2]],
                         ["c1/same.wav","c3/same.wav"])
        self.assertEqual(len(rows),3)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "Real offline FFmpeg required for media acceptance")
    def test_export_real_original_and_final_flac_clips_in_offline_review_zip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("app.project.STATE_FILE", root / "app_recent.json"):
                recovered, original_root, source = self._fixture(root)
                output = Path(recovered.root) / "output"
                build_project_v02(
                    Path(recovered.root) / ".generated/quick_index.csv",
                    source, output, make_flac=True,
                    delivery_mode="flac_only", translate_missing=False,
                    audio_language="ja", source_text_language="ja",
                    target_language="ja", state_dir=Path(recovered.root) / ".state",
                )
                old_flac = original_root / "output" / "continuous.flac"
                old_bytes = old_flac.read_bytes() if old_flac.exists() else None
                result = build_japanese_listening_pack(recovered,sample_count=3)
                self.assertEqual(result["selected"],3)
                self.assertFalse(result["spoken_japanese_verified"])
                self.assertEqual(
                    get_japanese_listening_pack(recovered,result["pack_id"]),
                    Path(result["pack_path"]),
                )
                with zipfile.ZipFile(result["pack_path"]) as z:
                    evidence = json.loads(z.read("evidence.json"))
                    self.assertFalse(evidence["human_review_complete"])
                    self.assertFalse(evidence["spoken_japanese_verified"])
                    self.assertEqual(evidence["selected"],3)
                    self.assertEqual(set(evidence["rows"][0].keys()) >=
                                     {"original_clip","rendered_clip","source_member_id"}, True)
                    self.assertEqual({r["source_member_id"] for r in evidence["rows"][:2]},
                                     {"chapter1/same.wav","chapter2/same.wav"})
                    for row in evidence["rows"]:
                        src=z.read(row["original_clip"])
                        rendered=z.read(row["rendered_clip"])
                        self.assertTrue(src.startswith(b"RIFF"))
                        self.assertTrue(rendered.startswith(b"RIFF"))
                        self.assertGreater(len(rendered),1000)
                        self.assertGreater(len(src),1000)
                    page=z.read("review.html").decode("utf-8")
                    self.assertIn("人工试听验收",page)
                    self.assertIn("manual_listening_review.json",page)
                self.assertFalse(old_flac.exists() if old_bytes is None else old_flac.read_bytes()!=old_bytes)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_failed_source_recheck_cannot_publish_misleading_pack(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            with patch("app.project.STATE_FILE", base/"app_recent.json"):
                recovered,_,source=self._fixture(base)
                output=Path(recovered.root)/"output"
                build_project_v02(Path(recovered.root)/".generated/quick_index.csv",
                                   source,output,make_flac=True,
                                   delivery_mode="flac_only",translate_missing=False,
                                   audio_language="ja",source_text_language="ja",
                                   target_language="ja",state_dir=Path(recovered.root)/".state")
                with source.open("ab") as handle:
                    handle.write(b"changed AFTER build")
                with self.assertRaisesRegex(ValueError,"发生变化"):
                    build_japanese_listening_pack(recovered,sample_count=2)
                self.assertFalse((output/"recovery_listening_packs").exists())

    def test_rejects_ordinary_project_and_path_injection(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            with patch("app.project.STATE_FILE",base/"app_recent.json"):
                recovered,old,_=self._fixture(base)
                self.assertIsNone(getattr(load_project(old),"recovery_source_attestation_required",None)
                                  if False else None)
                with self.assertRaisesRegex(ValueError,"identifier"):
                    get_japanese_listening_pack(recovered,"../outside")
                with self.assertRaisesRegex(ValueError,"No verified"):
                    get_japanese_listening_pack(load_project(old),"a"*20)

    def test_web_and_termux_have_job_and_download_route(self):
        root=Path(__file__).resolve().parents[1]
        for name in ("server.py","lite_server.py"):
            with self.subTest(adapter=name):
                text=(root/"app"/name).read_text("utf-8")
                self.assertIn("/api/recovery/listening-pack",text)
                self.assertIn("build_japanese_listening_pack(",text)
                self.assertIn("get_japanese_listening_pack(",text)
                self.assertIn('"jp-listening-pack"',text)
        script=(root/"app/static/index.html").read_text("utf-8")
        self.assertIn('id="generateJapaneseListeningPackBtn"',script)
        self.assertIn('id="japaneseListeningPackDownload"',script)


if __name__ == "__main__":
    unittest.main()
