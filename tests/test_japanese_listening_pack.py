"""True offline user-review pack acceptance: source ZIP -> finished FLAC -> paired PCM."""
from __future__ import annotations

import csv
import hashlib
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
    import_japanese_listening_review, japanese_listening_review_history,
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
                        # Final-excerpt time positions must contain actual
                        # speech-band PCM samples, not inter-chapter silence.
                        import struct
                        with wave.open(io.BytesIO(rendered), "rb") as reader:
                            count=min(reader.getnframes(),4000)
                            decoded=struct.unpack("<" + "h"*count,
                                                  reader.readframes(count))
                        self.assertGreater(sum(abs(x) for x in decoded)/max(1,count),500)
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

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_stale_pack_download_is_rejected_after_manifest_edit(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            with patch("app.project.STATE_FILE",base/"recent.json"):
                recovered,_,source=self._fixture(base)
                output=Path(recovered.root)/"output"
                build_project_v02(Path(recovered.root)/".generated/quick_index.csv",
                                   source,output,make_flac=True,
                                   delivery_mode="flac_only",translate_missing=False,
                                   audio_language="ja",source_text_language="ja",
                                   target_language="ja",state_dir=Path(recovered.root)/".state")
                report=build_japanese_listening_pack(recovered,sample_count=2)
                target=output/"manifest.json"
                target.write_bytes(target.read_bytes()+b"\n")
                with self.assertRaisesRegex(ValueError,"清单已变化"):
                    get_japanese_listening_pack(recovered,report["pack_id"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_manual_review_round_trip_is_bound_to_pack_and_append_only(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            with patch("app.project.STATE_FILE",base/"recent.json"):
                recovered,_,source=self._fixture(base)
                output=Path(recovered.root)/"output"
                build_project_v02(Path(recovered.root)/".generated/quick_index.csv",
                                   source,output,make_flac=True,
                                   delivery_mode="flac_only",translate_missing=False,
                                   audio_language="ja",source_text_language="ja",
                                   target_language="ja",state_dir=Path(recovered.root)/".state")
                pack=build_japanese_listening_pack(recovered,sample_count=3)
                with zipfile.ZipFile(pack["pack_path"]) as z:
                    raw=z.read("evidence.json")
                    evidence=json.loads(raw)
                    page=z.read("review.html").decode("utf-8")
                digest=hashlib.sha256(raw).hexdigest()
                self.assertIn(pack["pack_id"],page)
                self.assertIn(digest,page)
                self.assertIn("reviewer_confirmed",page)
                self.assertEqual(evidence["pack_id"],pack["pack_id"])
                response={
                    "schema_version":2,
                    "kind":"manual-japanese-recovery-listening-review",
                    "pack_id":pack["pack_id"],
                    "evidence_sha256":digest,
                    "completed":True,
                    "reviewer_confirmed":True,
                    "results":[{"id":row["id"],"language":"yes",
                                "text_match":"yes","rendered_match":"yes"}
                               for row in evidence["rows"]],
                }
                accepted=import_japanese_listening_review(recovered,json.dumps(response))
                self.assertEqual(accepted["status"],"sample-user-reported-pass")
                self.assertEqual(accepted["summary"]["passed"],3)
                self.assertFalse(accepted["entire_archive_verified"])
                self.assertFalse(accepted["spoken_japanese_automatically_verified"])
                response["results"][0]["language"]="no"
                rejected=import_japanese_listening_review(recovered,json.dumps(response))
                self.assertEqual(rejected["status"],"sample-failed")
                self.assertEqual(rejected["summary"]["failed"],1)
                self.assertNotEqual(accepted["receipt_path"],rejected["receipt_path"])
                self.assertTrue(Path(accepted["receipt_path"]).is_file())
                self.assertTrue(Path(rejected["receipt_path"]).is_file())
                history=japanese_listening_review_history(recovered,pack["pack_id"])
                self.assertEqual(history["total_submissions"],2)
                self.assertEqual(history["latest"]["status"],"sample-failed")
                self.assertFalse(history["entire_archive_verified"])

                response["results"][0]["language"]="yes"
                response["reviewer_confirmed"]=False
                incomplete=import_japanese_listening_review(recovered,json.dumps(response))
                self.assertEqual(incomplete["status"],"sample-incomplete")
                response["reviewer_confirmed"]=True
                response["results"][0]["text_match"]="unclear"
                unknown=import_japanese_listening_review(recovered,json.dumps(response))
                self.assertEqual(unknown["status"],"sample-inconclusive")

                receipt_dir=output/"recovery_listening_reviews"/pack["pack_id"]
                count=len(list(receipt_dir.glob("*.json")))
                response["evidence_sha256"]="0"*64
                with self.assertRaisesRegex(ValueError,"证据摘要不符"):
                    import_japanese_listening_review(recovered,json.dumps(response))
                response["evidence_sha256"]=digest
                response["pack_id"]="1"*20
                with self.assertRaises((ValueError, FileNotFoundError)):
                    import_japanese_listening_review(recovered,json.dumps(response))
                response["pack_id"]=pack["pack_id"]
                response["results"].append(dict(response["results"][0]))
                with self.assertRaisesRegex(ValueError,"样本数"):
                    import_japanese_listening_review(recovered,json.dumps(response))
                response["results"].pop()
                response["results"][0]["id"]="999"
                with self.assertRaisesRegex(ValueError,"陌生"):
                    import_japanese_listening_review(recovered,json.dumps(response))
                self.assertEqual(len(list(receipt_dir.glob("*.json"))),count)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_no_review_import_for_changed_final_archive_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            with patch("app.project.STATE_FILE",base/"recent.json"):
                recovered,_,source=self._fixture(base)
                output=Path(recovered.root)/"output"
                build_project_v02(Path(recovered.root)/".generated/quick_index.csv",
                                   source,output,make_flac=True,
                                   delivery_mode="flac_only",translate_missing=False,
                                   audio_language="ja",source_text_language="ja",
                                   target_language="ja",state_dir=Path(recovered.root)/".state")
                pack=build_japanese_listening_pack(recovered,sample_count=2)
                with zipfile.ZipFile(pack["pack_path"]) as z:
                    data=z.read("evidence.json")
                    evidence=json.loads(data)
                response={
                    "schema_version":2,"kind":"manual-japanese-recovery-listening-review",
                    "pack_id":pack["pack_id"],
                    "evidence_sha256":hashlib.sha256(data).hexdigest(),
                    "completed":True,"reviewer_confirmed":True,
                    "results":[{"id":r["id"],"language":"yes","text_match":"yes","rendered_match":"yes"}
                               for r in evidence["rows"]]
                }
                (output/"manifest.json").write_bytes(
                    (output/"manifest.json").read_bytes()+b"\n")
                with self.assertRaisesRegex(ValueError,"清单已变化"):
                    import_japanese_listening_review(recovered,json.dumps(response))
                self.assertFalse((output/"recovery_listening_reviews").exists())

    def test_manual_review_rejects_legacy_unbound_or_oversized_json(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            with patch("app.project.STATE_FILE",base/"recent.json"):
                recovered,_,_source=self._fixture(base)
                with self.assertRaisesRegex(ValueError,"格式不支持"):
                    import_japanese_listening_review(recovered,
                        '{"schema_version":1,"kind":"manual-japanese-recovery-listening-review"}')
                with self.assertRaisesRegex(ValueError,"64 KiB"):
                    import_japanese_listening_review(recovered,"X"*65537)

    def test_rejects_ordinary_project_and_path_injection(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            with patch("app.project.STATE_FILE",base/"app_recent.json"):
                recovered,old,_=self._fixture(base)
                self.assertFalse(load_project(old).recovery_source_attestation_required)
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
