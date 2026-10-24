"""Acceptance for isolated source-only Japanese recovery candidates."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.japanese_source_recovery import (
    RECOVERY_ATTESTATION,
    create_isolated_japanese_recovery,
    verify_japanese_recovery_build_inputs,
    verify_japanese_recovery_build_manifest,
)
from app.project import PROJECT_FILENAME, ProjectConfig, save_project
from app.quick import source_inventory


class JapaneseSourceRecoveryTests(unittest.TestCase):
    def prepare(self, base: Path) -> tuple[ProjectConfig, Path]:
        root = base / "Original-JP"
        generated = root / ".generated"
        generated.mkdir(parents=True)
        zip_path = base / "original_voice.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("chapter/voice_jp_001.wav", b"RIFF" + b"\x00" * 32)
            archive.writestr("chapter/voice_jp_001.lab", "日本語テキスト")
        inventory = source_inventory(zip_path)
        initial_csv = generated / "quick_index.csv"
        with initial_csv.open("w", newline="", encoding="utf-8-sig") as out:
            writer = csv.DictWriter(out, fieldnames=[
                "index", "group", "filename", "source_member_id", "english",
            ])
            writer.writeheader()
            writer.writerow({
                "index": "1", "group": "chapter",
                "filename": "voice_jp_001.wav",
                "source_member_id": "chapter/voice_jp_001.wav",
                "english": "日本語テキスト",
            })
        (generated / "quick_scan.json").write_text(json.dumps({
            "source_text_language": "ja",
            "english": {
                "source": str(zip_path),
                "fingerprint": inventory["fingerprint"],
            },
        }), encoding="utf-8")
        # Simulate a legacy wrong-language incremental source without touching original ZIP.
        corrupted = generated / "combined_audio_versions" / "old"
        corrupted.mkdir(parents=True)
        (corrupted / "english.wav").write_bytes(b"EN")
        output = root / "output"
        output.mkdir()
        (output / "update_apply_report.json").write_text(json.dumps({
            "project_updated": True, "applied_filenames": ["english.wav"],
        }))
        (output / "continuous.flac").write_bytes(b"old product")
        cfg = ProjectConfig(
            schema_version=1, name="Old JP",
            root=str(root),
            index_csv=str(generated / "quick_index_incremental.csv"),
            wav_source=str(corrupted),
            output_dir="output",
            audio_language="ja",
            source_text_language="ja",
            target_language="zh-CN",
            game_id="honkai-star-rail",
        )
        save_project(cfg)
        return cfg, zip_path

    def test_creates_clean_unbuilt_candidate_and_preserves_old_project_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, original = self.prepare(base)
                root = Path(cfg.root)
                before = {
                    p.relative_to(root).as_posix(): p.read_bytes()
                    for p in root.rglob("*") if p.is_file()
                }
                result = create_isolated_japanese_recovery(cfg)
                after = {
                    p.relative_to(root).as_posix(): p.read_bytes()
                    for p in root.rglob("*") if p.is_file()
                }
                self.assertEqual(before, after)
                self.assertTrue(result["source_fingerprint_verified"])
                self.assertEqual(result["wav_members"], 1)
                self.assertFalse(result["built"])
                self.assertFalse(result["listening_verified"])
                restored = Path(result["candidate_project"])
                self.assertTrue(restored.is_dir())
                self.assertEqual(result["source"], str(original))
                restored_cfg = json.loads((restored / PROJECT_FILENAME).read_text(encoding="utf-8"))
                self.assertEqual(restored_cfg["wav_source"], str(original))
                self.assertEqual(restored_cfg["index_csv"], ".generated/quick_index.csv")
                self.assertEqual(restored_cfg["audio_language"], "ja")
                self.assertEqual(restored_cfg["source_text_language"], "ja")
                self.assertTrue(restored_cfg["remote_index_url"].endswith("/JP.xlsx"))
                self.assertFalse((restored / "output").exists())
                self.assertFalse((restored / ".state").exists())
                self.assertFalse((restored / ".generated" / "combined_audio_versions").exists())
                self.assertFalse((restored / ".generated" / "incremental_audio").exists())
                self.assertTrue((root / "output" / "continuous.flac").is_file())
                self.assertTrue((restored / RECOVERY_ATTESTATION).is_file())
                from app.project import load_project
                recovered = load_project(restored)
                proof = verify_japanese_recovery_build_inputs(recovered)
                self.assertEqual(proof["wav_members"], 1)
                self.assertEqual(proof["member_ids"], ["chapter/voice_jp_001.wav"])
                self.assertTrue(recovered.recovery_source_attestation_required)

    def test_tampered_original_source_is_refused_without_new_project(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, original = self.prepare(base)
                with original.open("ab") as handle:
                    handle.write(b"tampered")
                with self.assertRaisesRegex(ValueError, "指纹不一致"):
                    create_isolated_japanese_recovery(cfg)
                self.assertFalse(any("source-recovery" in p.name for p in base.iterdir()))

    def test_mismatched_index_members_are_refused(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, _original = self.prepare(base)
                initial = Path(cfg.root) / ".generated" / "quick_index.csv"
                initial.write_text(
                    "filename,source_member_id\nother.wav,chapter/other.wav\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "不完全匹配"):
                    create_isolated_japanese_recovery(cfg)
                self.assertFalse(any("source-recovery" in p.name for p in base.iterdir()))

    def test_ambiguous_auto_language_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, _ = self.prepare(base)
                cfg.audio_language = "auto"
                save_project(cfg)
                with self.assertRaisesRegex(ValueError, "ja"):
                    create_isolated_japanese_recovery(cfg)

    def test_rebuild_rechecks_external_original_after_recovery_candidate_creation(self):
        from app.project import load_project
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, original = self.prepare(base)
                before = {p.relative_to(Path(cfg.root)).as_posix(): p.read_bytes()
                          for p in Path(cfg.root).rglob("*") if p.is_file()}
                candidate = Path(create_isolated_japanese_recovery(cfg)["candidate_project"])
                recovered = load_project(candidate)
                self.assertIsNotNone(verify_japanese_recovery_build_inputs(recovered))
                with original.open("ab") as handle:
                    handle.write(b"ZIP archive modified after candidate creation")
                with self.assertRaisesRegex(ValueError, "原始语音包自创建后已发生变化"):
                    verify_japanese_recovery_build_inputs(recovered)
                self.assertFalse((candidate / "output").exists())
                self.assertEqual({p.relative_to(Path(cfg.root)).as_posix(): p.read_bytes()
                                  for p in Path(cfg.root).rglob("*") if p.is_file()}, before)

    def test_rebuild_rejects_tampered_candidate_index_and_config_source(self):
        from app.project import load_project
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, _ = self.prepare(base)
                candidate = Path(create_isolated_japanese_recovery(cfg)["candidate_project"])
                recovered = load_project(candidate)
                index = candidate / ".generated" / "quick_index.csv"
                original_csv = index.read_bytes()
                index.write_bytes(original_csv + b"tamper")
                with self.assertRaisesRegex(ValueError, "索引或来源指纹"):
                    verify_japanese_recovery_build_inputs(recovered)
                index.write_bytes(original_csv)
                recovered.wav_source = cfg.wav_source
                with self.assertRaisesRegex(ValueError, "偏离"):
                    verify_japanese_recovery_build_inputs(recovered)

    def test_deleted_candidate_receipt_fails_closed(self):
        from app.project import load_project
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, _ = self.prepare(base)
                candidate = Path(create_isolated_japanese_recovery(cfg)["candidate_project"])
                recovered = load_project(candidate)
                (candidate / RECOVERY_ATTESTATION).unlink()
                with self.assertRaisesRegex(ValueError, "缺少必须的来源证明"):
                    verify_japanese_recovery_build_inputs(recovered)

    def test_ordinary_preexisting_projects_are_not_forced_into_recovery_contract(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, _ = self.prepare(base)
                self.assertFalse(cfg.recovery_source_attestation_required)
                self.assertIsNone(verify_japanese_recovery_build_inputs(cfg))

    def test_manifest_member_identity_must_match_after_build(self):
        from app.project import load_project
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                cfg, _ = self.prepare(base)
                candidate = Path(create_isolated_japanese_recovery(cfg)["candidate_project"])
                recovered = load_project(candidate)
                proof = verify_japanese_recovery_build_inputs(recovered)
                output = candidate / "output"
                output.mkdir()
                manifest = output / "manifest.json"
                good = {"entries": [{"filename": "voice_jp_001.wav",
                                     "source_member_id": "chapter/voice_jp_001.wav"}]}
                manifest.write_text(json.dumps(good), encoding="utf-8")
                accepted = verify_japanese_recovery_build_manifest(recovered, proof)
                self.assertEqual(accepted["status"], "source-members-verified")
                self.assertEqual(accepted["wav_members"], 1)
                self.assertFalse(accepted["spoken_japanese_verified"])
                manifest.write_text(json.dumps({"entries": [{"filename": "english.wav",
                                     "source_member_id": "chapter/english.wav"}]}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "不完全匹配"):
                    verify_japanese_recovery_build_manifest(recovered, proof)
                manifest.write_text(json.dumps({"entries": []}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "成员数"):
                    verify_japanese_recovery_build_manifest(recovered, proof)

    def test_real_offline_pipeline_rebuild_preserves_original_member_identity(self):
        # Exercise Quick CSV -> ZIP PCM -> metadata/manifest pipeline, not only
        # an invented manifest. No translation, network, or FFmpeg is needed.
        import io
        import wave
        from app.pipeline import build_project_v02
        from app.project import load_project

        def pcm_wav() -> bytes:
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\x00\x00" * 1600)
            return buffer.getvalue()

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                config, original = self.prepare(base)
                chapter_wavs = ("chapter1/same.wav", "chapter2/same.wav")
                chapter_texts = ("日本語の台詞一", "日本語の台詞二")
                with zipfile.ZipFile(original, "w") as archive:
                    for member, text in zip(chapter_wavs, chapter_texts):
                        archive.writestr(member, pcm_wav())
                        archive.writestr(member[:-4] + ".lab", text)
                index = Path(config.root) / ".generated" / "quick_index.csv"
                with index.open("w", newline="", encoding="utf-8-sig") as file:
                    writer = csv.DictWriter(file, fieldnames=[
                        "index", "group", "filename", "source_member_id",
                        "english", "source",
                    ])
                    writer.writeheader()
                    for i, (member, text) in enumerate(zip(chapter_wavs, chapter_texts), 1):
                        writer.writerow({
                            "index": str(i), "group": "chapter",
                            "filename": "same.wav", "source_member_id": member,
                            "english": text, "source": "primary-package-lab",
                        })
                scan_file = Path(config.root) / ".generated" / "quick_scan.json"
                scan = json.loads(scan_file.read_text(encoding="utf-8"))
                scan["english"]["fingerprint"] = source_inventory(original)["fingerprint"]
                scan_file.write_text(json.dumps(scan), encoding="utf-8")
                config.target_language = "ja"
                config.translate_missing = False
                save_project(config)
                old_flac = Path(config.root) / "output" / "continuous.flac"
                old_bytes = old_flac.read_bytes()
                restored = Path(create_isolated_japanese_recovery(config)["candidate_project"])
                recovered = load_project(restored)
                source_proof = verify_japanese_recovery_build_inputs(recovered)
                output = restored / "output"
                build_project_v02(
                    restored / ".generated" / "quick_index.csv",
                    original, output,
                    make_flac=False,
                    translate_missing=False,
                    audio_language="ja",
                    source_text_language="ja",
                    target_language="ja",
                    state_dir=restored / ".state",
                )
                acceptance = verify_japanese_recovery_build_manifest(
                    recovered, source_proof,
                )
                self.assertEqual(acceptance["wav_members"], 2)
                self.assertFalse(acceptance["spoken_japanese_verified"])
                manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    {row["source_member_id"] for row in manifest["entries"]},
                    set(chapter_wavs),
                )
                self.assertEqual(
                    {row["source_member_id"]: row["source_text"] for row in manifest["entries"]},
                    dict(zip(chapter_wavs, chapter_texts)),
                )
                self.assertEqual(old_flac.read_bytes(), old_bytes)

    def test_preexisting_unattested_candidate_directory_fails_closed(self):
        from app.project import load_project
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch("app.project.STATE_FILE", base / "recent.json"):
                config, original = self.prepare(base)
                legacy = base / "Original-JP-JP-source-recovery-0123456789"
                legacy.mkdir()
                config.root = str(legacy)
                config.wav_source = str(original)
                config.index_csv = ".generated/quick_index.csv"
                save_project(config)
                with self.assertRaisesRegex(ValueError, "旧版日语恢复候选"):
                    verify_japanese_recovery_build_inputs(load_project(legacy))

    def test_recovery_build_gate_is_applied_by_both_queued_backend_jobs(self):
        repository = Path(__file__).resolve().parents[1]
        for filename in ("server.py", "lite_server.py"):
            with self.subTest(filename=filename):
                code = (repository / "app" / filename).read_text(encoding="utf-8")
                self.assertIn("initial_proof = verify_japanese_recovery_build_inputs(config)", code)
                self.assertIn("verify_japanese_recovery_build_manifest(config, initial_proof)", code)
                self.assertIn('result["japanese_source_recovery"] = attested', code)
        recovery = (repository / "app/japanese_source_recovery.py").read_text(encoding="utf-8")
        self.assertIn("RECOVERY_ATTESTATION", recovery)

    def test_both_servers_expose_explicit_recovery_confirmation(self):
        repository = Path(__file__).resolve().parents[1]
        for filename in ("server.py", "lite_server.py"):
            with self.subTest(filename=filename):
                code = (repository / "app" / filename).read_text(encoding="utf-8")
                self.assertIn("/api/update/create-japanese-recovery", code)
                self.assertIn('"create-isolated"', code)
                self.assertIn('create_isolated_japanese_recovery(', code)
        ui = (repository / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="createJapaneseRecoveryBtn"', ui)
        self.assertIn("独立恢复工程", ui)
        jobs = (repository / "app/jobs.py").read_text(encoding="utf-8")
        self.assertIn('"locale-recovery"', jobs)


if __name__ == "__main__":
    unittest.main()
