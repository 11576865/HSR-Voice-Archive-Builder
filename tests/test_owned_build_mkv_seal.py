"""Owned build-marker regression: final MKV sealing must not block itself.

No fake 'verified' status is injected. Tests exercise real receipt and
signature verification, while media bytes are fixtures; FFmpeg codec checks
remain covered by the independent exporter integration suite.
"""
from __future__ import annotations

import json
from contextvars import copy_context
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.archive_bundle_lifecycle import (
    OUTPUT, RECEIPT, bundle_export_status, record_bundle_publication,
)
from app.delivery_artifacts import LEGACY_BLACK, seal_complete_delivery
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
        temp_flac = self.output / ".candidate-continuous.flac"
        temp_flac.write_bytes(b"fake-source-flac-" + text.encode())
        os.replace(temp_flac, self.output / "continuous.flac")
        record_subtitle_export(self.output, state="current", ass_required=True)
        temp_mkv = self.output / ".candidate-bundle.mkv"
        temp_mkv.write_bytes(video)
        os.replace(temp_mkv, self.output / OUTPUT)
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
        # Legacy standalone successful export is safe to seal when there is
        # no build in progress; keep this API backward compatible.
        seal_complete_delivery(self.output)
        self.assertFalse((self.output / "continuous.flac").exists())
        self.assertEqual(bundle_export_status(self.output)["state"], "current")

        # A marker created by an unrelated/other process is never ownership.
        self.make_sources(text="another")
        (self.output / BUILD_MARKER).touch()
        try:
            with self.assertRaisesRegex(ValueError, "Cannot discard working files"):
                seal_complete_delivery(self.output)
            self.assertTrue((self.output / "continuous.flac").exists())
        finally:
            (self.output / BUILD_MARKER).unlink()

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

    def test_unsafe_legacy_output_never_seals_or_discards_working_sources(self):
        self.make_sources()
        legacy = self.output / LEGACY_BLACK
        legacy.write_bytes(b"must-not-modify")
        receipt_before = (self.output / RECEIPT).read_bytes()
        real_is_symlink = Path.is_symlink

        def simulate_symlink(path):
            if path == legacy:
                return True
            return real_is_symlink(path)

        with patch.object(Path, "is_symlink", new=simulate_symlink):
            with self.assertRaisesRegex(ValueError, "Unsafe historical black-MKV"):
                seal_complete_delivery(self.output)

        # This must be rejected BEFORE receipt publication or any cleanup,
        # including standalone MKV exports without the outer rollback guard.
        self.assertEqual((self.output / RECEIPT).read_bytes(), receipt_before)
        self.assertFalse(json.loads(receipt_before)["sealed"] if
                         "sealed" in json.loads(receipt_before) else False)
        self.assertEqual(legacy.read_bytes(), b"must-not-modify")
        for name in ("continuous.flac", "HSR_Voice_Archive.ass",
                     "HSR_Voice_Archive.srt"):
            self.assertTrue((self.output / name).is_file(), name)
        self.assertEqual(bundle_export_status(self.output)["state"], "current")

    def test_final_marker_unlink_failure_rolls_back_previous_sealed_bundle(self):
        self.make_sources(text="old", video=b"trusted-old-mkv")
        seal_complete_delivery(self.output)
        old_receipt = (self.output / RECEIPT).read_bytes()
        previous = (self.output / OUTPUT).read_bytes()
        old_unlink = Path.unlink
        errors = []

        def deny_once(path, *args, **kwargs):
            if path.name == BUILD_MARKER and not errors:
                errors.append(True)
                raise PermissionError("injected final build-marker commit failure")
            return old_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=deny_once):
            with self.assertRaisesRegex(PermissionError, "build-marker commit"):
                with preserve_published_archive(self.output, self.state):
                    self.make_sources(text="new", video=b"uncommitted-new-mkv")
                    seal_complete_delivery(self.output)

        self.assertEqual(errors, [True])
        self.assertEqual((self.output / OUTPUT).read_bytes(), previous)
        self.assertEqual((self.output / RECEIPT).read_bytes(), old_receipt)
        self.assertFalse((self.output / "continuous.flac").exists())
        self.assertFalse((self.output / BUILD_MARKER).exists())
        self.assertEqual(bundle_export_status(self.output)["state"], "current")
        self.assertFalse(owns_published_build(self.output))

    def test_persistent_marker_unlink_failure_restores_old_bundle_but_keeps_busy(self):
        self.make_sources(text="old", video=b"trusted-old-mkv")
        seal_complete_delivery(self.output)
        previous = (self.output / OUTPUT).read_bytes()
        previous_receipt = (self.output / RECEIPT).read_bytes()
        original_unlink = Path.unlink
        attempts = []

        def always_deny_marker(path, *args, **kwargs):
            if path.name == BUILD_MARKER:
                attempts.append(1)
                raise PermissionError("persistent marker removal denial")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=always_deny_marker):
            with self.assertRaisesRegex(PermissionError, "marker removal denial"):
                with preserve_published_archive(self.output, self.state):
                    self.make_sources(text="new", video=b"new-candidate")
                    seal_complete_delivery(self.output)

        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual((self.output / OUTPUT).read_bytes(), previous)
        self.assertEqual((self.output / RECEIPT).read_bytes(), previous_receipt)
        self.assertTrue((self.output / BUILD_MARKER).exists())
        self.assertEqual(bundle_export_status(self.output)["state"], "stale")
        self.assertFalse(owns_published_build(self.output))

    def test_copied_transaction_context_loses_verification_authority_after_exit(self):
        self.make_sources()
        with preserve_published_archive(self.output, self.state):
            inherited = copy_context()
            self.assertTrue(inherited.run(owns_published_build, self.output))
            self.assertTrue(owns_published_build(self.output))
        self.assertFalse(inherited.run(owns_published_build, self.output))
        self.assertFalse(owns_published_build(self.output))

        # A later independent build has a fresh marker. The old context,
        # possibly retained in a spawned async task, must never be able to
        # suppress its build-busy status or impersonate the new build owner.
        with preserve_published_archive(self.output, self.state):
            self.assertEqual(bundle_export_status(self.output)["state"], "stale")
            with self.assertRaisesRegex(RuntimeError, "owned build transaction"):
                inherited.run(bundle_export_status, self.output, _owned_build=True)
            self.assertTrue(owns_published_build(self.output))
            self.assertTrue((self.output / BUILD_MARKER).is_file())
        self.assertEqual(bundle_export_status(self.output)["state"], "current")

    def test_copied_context_is_revoked_when_original_build_rolls_back(self):
        self.make_sources()
        inherited = None
        with self.assertRaisesRegex(ValueError, "injected rollback"):
            with preserve_published_archive(self.output, self.state):
                inherited = copy_context()
                self.assertTrue(inherited.run(owns_published_build, self.output))
                raise ValueError("injected rollback")
        assert inherited is not None
        self.assertFalse(inherited.run(owns_published_build, self.output))
        self.assertFalse((self.output / BUILD_MARKER).exists())
        with self.assertRaisesRegex(RuntimeError, "owned build transaction"):
            inherited.run(bundle_export_status, self.output, _owned_build=True)

    def test_nested_same_output_build_cannot_clear_outer_marker_early(self):
        self.make_sources()
        with preserve_published_archive(self.output, self.state):
            self.assertTrue((self.output / BUILD_MARKER).is_file())
            with self.assertRaisesRegex(RuntimeError, "Nested protected builds"):
                with preserve_published_archive(self.output, self.state):
                    self.fail("nested build should never enter")
            self.assertTrue((self.output / BUILD_MARKER).is_file())
            self.assertTrue(owns_published_build(self.output))
            self.assertEqual(bundle_export_status(self.output)["state"], "stale")
        self.assertFalse((self.output / BUILD_MARKER).exists())

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
