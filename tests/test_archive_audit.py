from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.archive_audit import audit_archive


class ArchiveAuditTests(unittest.TestCase):
    def test_chapter_coverage_and_same_basename_identity(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            rows = [
                dict(index=1, filename="same.wav", source_member_id="story/a/same.wav",
                     group="主线", source_text="Hello", target_text="你好",
                     sha256="a" * 64, start_seconds=0, audio_end_seconds=1,
                     display_end_seconds=1),
                dict(index=2, filename="same.wav", source_member_id="story/b/same.wav",
                     group="同行", source_text="Thanks", target_text="谢谢",
                     sha256="b" * 64, start_seconds=2, audio_end_seconds=3,
                     display_end_seconds=3),
            ]
            (output / "manifest.json").write_text(
                json.dumps({"entries": rows}), encoding="utf-8",
            )
            result = audit_archive(output)
            self.assertEqual(result["summary"]["entries"], 2)
            self.assertEqual(result["summary"]["groups"], 2)
            self.assertEqual(result["summary"]["duplicate_basename_groups"], 1)
            self.assertEqual(result["summary"]["issues"], 0)
            self.assertEqual([row["name"] for row in result["groups"]], ["主线", "同行"])

    def test_conflicting_member_and_bad_time_reported_without_writes(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            rows = [
                dict(index=1, filename="x.wav", source_member_id="a/x.wav",
                     source_text="a", target_text="a", sha256="a" * 64,
                     start_seconds=5, audio_end_seconds=6, display_end_seconds=6),
                dict(index=1, filename="x.wav", source_member_id="a/x.wav",
                     source_text="", target_text="", sha256="not-a-hash",
                     start_seconds=3, audio_end_seconds=2, display_end_seconds=2),
            ]
            path = output / "manifest.json"
            path.write_text(json.dumps({"entries": rows}), encoding="utf-8")
            before = path.read_bytes()
            result = audit_archive(output)
            codes = {item["code"] for item in result["issues"]}
            self.assertTrue({
                "duplicate_entry_id", "duplicate_source_member",
                "invalid_source_timeline", "source_audio_hash_missing",
            }.issubset(codes))
            self.assertEqual(path.read_bytes(), before)

    def test_requires_existing_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                audit_archive(Path(td))
