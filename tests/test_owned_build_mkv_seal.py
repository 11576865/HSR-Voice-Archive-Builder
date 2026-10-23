"""Owned build-marker regression: final MKV sealing must not block itself.

No fake 'verified' status is injected. Tests exercise real receipt and
signature verification, while media bytes are fixtures; FFmpeg codec checks
remain covered by the independent exporter integration suite.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.archive_bundle_lifecycle import (
    OUTPUT, RECEIPT, bundle_export_status, record_bundle_publication,
)
from app.delivery_artifacts import seal_complete_delivery
from app.publishing import BUILD_MARKER, owns_published_build, preserve_published_archive
from app.subtitle_export_status import record_subtitle_export


class OwnedBuildSealTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / "output"
        self.state = self.root / ".state"
        self.output.mkdir()
        self.state.mkdir()

    def make_sources(self, *, text: str = "hello", video: bytes = b"verified-mkv-bytes"):
        (self.output / "manifest.json").write_text(
            '{"entries":[]}', encoding="utf-8"
        )
        (self.output / "HSR_Voice_Archive.ass").write_text(
            "[Events]\n" + text, encoding="utf-8"
        )
        (self.output / "HSR_Voice_Archive.srt").write_text(
            "1\n" + text, encoding="utf-8"
        )
        (self.output / "continuous.flac").write_bytes(b"fake-source-flac-" + text.encode())
        record_subtitle_export(self.output, state="current", ass_required=True)
        (self.output / OUTPUT).write_bytes(video)
        record_bundle_publication(
            self.output, background_color="#000000",
            background_image=None, image_extension=None, include_fonts=False,
        )

    def test_complete_build_seals_own_mkv_while_public_health_stays_busy(self):
        with preserve_published_archive(self.output, self.state):
            self.assertTrue(owns_published_build(self.output))
            self.make_sources()
            self.assertTrue((self.output / BUILD_MARKER).exists())
            public = bundle_export_status(self.output)
            self.assertEqual(public["state"], "stale")
            self.assertIn("项目正在构建", public["message"])
            internal = bundle_export_status(self.output, _owned_build=True)
            self.assertEqual(internal["state"], "current", internal)
            self.assertFalse(internal["needs_rebuild"])

            seal_complete_delivery(self.output)
            self.assertTrue(json.loads((self.output / RECEIPT).read_text())["sealed"])
            for name in ("continuous.flac", "HSR_Voice_Archive.ass",
                         "HSR_Voice_Archive.srt"):
                self.assertFalse((self.output / name).exists(), name)
            self.assertEqual(bundle_export_status(self.output)["state"], "stale")
            self.assertEqual(bundle_export_status(
                self.output, _owned_build=True)["state"], "current")

        self.assertFalse(owns_published_build(self.output))
        self.assertFalse((self.output / BUILD_MARKER).exists())
        self.assertEqual(bundle_export_status(self.output)["state"], "current")
        self.assertTrue((self.output / OUTPUT).exists())

    def test_internal_exception_cannot_escape_its_owned_build_transaction(self):
        self.make_sources()
        # A caller may not provide the internal flag without being inside the
        # exact protected publishing scope.
        with self.assertRaisesRegex(RuntimeError, "owned build transaction"):
            bundle_export_status(self.output, _owned_build=True)
        with self.assertRaisesRegex(RuntimeError, "owned build transaction"):
            seal_complete_delivery(self.output)

        another = self.root / "other"
        another.mkdir()
        with preserve_published_archive(self.output, self.state):
            self.assertFalse(owns_published_build(another))
            with self.assertRaisesRegex(RuntimeError, "owned build transaction"):
                bundle_export_status(another, _owned_build=True)

    def test_source_identity_mismatch_refuses_discard_and_rolls_back(self):
        self.make_sources(text="old", video=b"previous-known-mkv")
        previous_receipt = (self.output / RECEIPT).read_bytes()
        previous_flac = (self.output / "continuous.flac").read_bytes()
        with self.assertRaisesRegex(ValueError, "Cannot discard working files"):
            with preserve_published_archive(self.output, self.state):
                self.make_sources(text="new", video=b"new-mkv")
                # Deliberately modify FLAC AFTER publication receipt. The
                # owned-build exemption must not bypass source verification.
                (self.output / "continuous.flac").write_bytes(b"tampered-FLAC")
                self.assertNotEqual(bundle_export_status(
                    self.output, _owned_build=True)["state"], "current")
                seal_complete_delivery(self.output)

        self.assertFalse((self.output / BUILD_MARKER).exists())
        self.assertEqual((self.output / OUTPUT).read_bytes(), b"previous-known-mkv")
        self.assertEqual((self.output / RECEIPT).read_bytes(), previous_receipt)
        self.assertEqual((self.output / "continuous.flac").read_bytes(), previous_flac)
        self.assertEqual(bundle_export_status(self.output)["state"], "current")

    def test_failed_initial_build_keeps_sources_if_seal_rejects(self):
        self.make_sources()
        (self.output / RECEIPT).unlink()
        with preserve_published_archive(self.output, self.state):
            for name in ("continuous.flac", "HSR_Voice_Archive.ass",
                         "HSR_Voice_Archive.srt"):
                self.assertTrue((self.output / name).exists())
            with self.assertRaisesRegex(ValueError, "Cannot discard working files"):
                seal_complete_delivery(self.output)
            # A failed pre-seal MUST leave all working sources untouched.
            for name in ("continuous.flac", "HSR_Voice_Archive.ass",
                         "HSR_Voice_Archive.srt"):
                self.assertTrue((self.output / name).exists())


if __name__ == "__main__":
    unittest.main()
