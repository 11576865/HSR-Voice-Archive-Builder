"""Source-product/bundle contract and safe 16 MiB background upload regression."""
from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.archive_bundle_backgrounds import MAX_BUNDLE_JSON_BYTES, MAX_IMAGE_BYTES
from app.archive_bundle_exporter import decode_bundle_background
from app.archive_bundle_lifecycle import (
    OUTPUT, bundle_export_status, load_previous_bundle_image, record_bundle_publication,
)
from app.subtitle_export_status import record_subtitle_export
from app.lite_server import Handler


PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class BackgroundUploadBudgetTests(unittest.TestCase):
    def test_uploaded_image_larger_than_old_4mib_is_accepted_and_replayable(self):
        data = PNG_HEADER + b"Q" * (5 * 1024 * 1024)
        self.assertEqual(decode_bundle_background(base64.b64encode(data).decode("ascii")), data)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "continuous.flac").write_bytes(b"flac fixture")
            (root / "HSR_Voice_Archive.ass").write_bytes(b"ass fixture")
            (root / "HSR_Voice_Archive.srt").write_bytes(b"srt fixture")
            (root / "manifest.json").write_text('{"entries":[]}')
            (root / OUTPUT).write_bytes(b"mkv fixture")
            record_subtitle_export(root, state="current", ass_required=True)
            record_bundle_publication(
                root, background_color="#010203", background_image=data,
                image_extension=".png", include_fonts=False,
                background_spec={"mode": "image"},
            )
            self.assertEqual(load_previous_bundle_image(root), data)
            self.assertEqual(bundle_export_status(root)["state"], "current")
            self.assertTrue(bundle_export_status(root)["can_generate"])

    def test_image_limit_boundary_and_rejected_oversized_decode(self):
        self.assertEqual(MAX_IMAGE_BYTES, 16 * 1024 * 1024)
        self.assertEqual(MAX_BUNDLE_JSON_BYTES, 24 * 1024 * 1024)
        valid = PNG_HEADER + b"a" * (MAX_IMAGE_BYTES - len(PNG_HEADER))
        self.assertEqual(len(decode_bundle_background(base64.b64encode(valid).decode("ascii"))), MAX_IMAGE_BYTES)
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            decode_bundle_background(base64.b64encode(valid + b"x").decode("ascii"))

    def test_lite_json_payload_budget_is_endpoint_specific(self):
        def request(path, payload):
            fake = SimpleNamespace(
                path=path,
                headers={"Content-Length": str(len(payload))},
                rfile=io.BytesIO(payload),
            )
            return Handler._read_body(fake)
        payload = b"Q" * (8 * 1024 * 1024 + 1)
        for path in ("/api/output/archive-bundle", "/api/output/archive-bundle/background-preview"):
            self.assertEqual(len(request(path, payload)), len(payload))
        with self.assertRaisesRegex(ValueError, "too large"):
            request("/api/status", payload)
        too_big = SimpleNamespace(
            path="/api/output/archive-bundle",
            headers={"Content-Length": str(MAX_BUNDLE_JSON_BYTES + 1)},
            rfile=io.BytesIO(b""),
        )
        with self.assertRaisesRegex(ValueError, "too large"):
            Handler._read_body(too_big)


class FastApiArchiveBodyBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_content_length_is_bounded_by_actual_bytes(self):
        from app.server import _read_bounded_bundle_json

        class FakeRequest:
            def __init__(self, data, content_length=None):
                self.headers = {} if content_length is None else {"content-length": str(content_length)}
                self.data = data

            async def stream(self):
                for start in range(0, len(self.data), 256 * 1024):
                    yield self.data[start:start + 256 * 1024]

        # 8 MiB was formerly the hard cap. A 9 MiB JSON body must now pass.
        body = json.dumps({"background_image_base64": "X" * (9 * 1024 * 1024)}).encode()
        got = await _read_bounded_bundle_json(FakeRequest(body))
        self.assertEqual(len(got["background_image_base64"]), 9 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "24 MiB"):
            await _read_bounded_bundle_json(FakeRequest(body, content_length=MAX_BUNDLE_JSON_BYTES + 1))
        # Forged/missing Content-Length cannot evade the streamed-byte limit.
        too_big = b"X" * (MAX_BUNDLE_JSON_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "24 MiB"):
            await _read_bounded_bundle_json(FakeRequest(too_big))


class BundleAsDerivedDeliverableTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        (self.root / "manifest.json").write_text('{"entries":[]}')
        (self.root / "continuous.flac").write_bytes(b"flac")
        (self.root / "HSR_Voice_Archive.ass").write_bytes(b"ass")
        (self.root / "HSR_Voice_Archive.srt").write_bytes(b"srt")
        record_subtitle_export(self.root, state="current", ass_required=True)
        (self.root / OUTPUT).write_bytes(b"mkv")
        record_bundle_publication(
            self.root, background_color="#000000", background_image=None,
            image_extension=None, include_fonts=False,
        )

    def test_current_bundle_depends_on_current_flac_and_subtitle(self):
        info = bundle_export_status(self.root)
        self.assertTrue(info["can_generate"])
        self.assertTrue(info["prerequisites"]["subtitles_current"])
        self.assertIn("continuous.flac", info["depends_on"])
        self.assertEqual(info["state"], "current")

        (self.root / "continuous.flac").unlink()
        info = bundle_export_status(self.root)
        self.assertEqual(info["state"], "stale")
        self.assertFalse(info["can_generate"])
        self.assertTrue(info["blockers"])
        self.assertTrue((self.root / OUTPUT).exists(), "Never delete last successful MKV")

    def test_ass_revision_blocks_repack_but_keeps_old_product(self):
        (self.root / "HSR_Voice_Archive.ass").write_text("changed content")
        status = bundle_export_status(self.root)
        self.assertEqual(status["state"], "stale")
        self.assertFalse(status["prerequisites"]["subtitles_current"])
        self.assertFalse(status["can_generate"])
        self.assertTrue((self.root / OUTPUT).exists())

    def test_zero_byte_sources_block_generation_but_keep_published_mkv(self):
        for filename, prereq in (
            ("continuous.flac", "continuous_flac"),
            ("HSR_Voice_Archive.ass", "ass"),
            ("HSR_Voice_Archive.srt", "srt"),
        ):
            with self.subTest(filename=filename):
                path = self.root / filename
                original = path.read_bytes()
                try:
                    path.write_bytes(b"")
                    result = bundle_export_status(self.root)
                    self.assertFalse(result["can_generate"])
                    self.assertFalse(result["prerequisites"][prereq])
                    self.assertTrue(result["blockers"])
                    self.assertEqual(result["state"], "stale")
                    self.assertEqual((self.root / OUTPUT).read_bytes(), b"mkv")
                finally:
                    path.write_bytes(original)
                    record_subtitle_export(self.root, state="current", ass_required=True)
                    # Rebase the receipt against the restored fixtures for
                    # independent test subcases.
                    record_bundle_publication(
                        self.root, background_color="#000000", background_image=None,
                        image_extension=None, include_fonts=False,
                    )

    def test_large_saved_background_status_hashing_avoids_eager_image_read(self):
        image = PNG_HEADER + b"z" * (5 * 1024 * 1024)
        record_bundle_publication(
            self.root, background_color="#000000", background_image=image,
            image_extension=".png", include_fonts=False,
            background_spec={"mode": "image"},
        )
        # Full content needs to be loaded only when the user explicitly
        # requests reuse; project summary should hash in bounded chunks.
        with patch.object(Path, "read_bytes", side_effect=AssertionError("eager read")):
            status = bundle_export_status(self.root)
        self.assertEqual(status["state"], "current")
        self.assertEqual(status["image_saved"], True)

    def test_legacy_mkv_is_not_the_complete_bundle(self):
        project = (Path(__file__).resolve().parents[1] / "app/project.py").read_text(encoding="utf-8")
        self.assertIn('"playback_bundle"', project)
        self.assertIn('"independent_products"', project)
        self.assertIn('"legacy_outputs"', project)
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn("旧文件·已过期", html)
        self.assertIn("独立于完整 MKV", html)
        self.assertIn("status:mkvStatus", html)
        self.assertIn("settings.can_generate===false", html)
        # Prevent a TDZ ReferenceError while the existing project view is
        # revealing old successful outputs from a partially rebuilt project.
        self.assertIn("const availableProducts=p.output_groups?.final_products||p.outputs||{}", html)
        self.assertIn("Object.entries(availableProducts)", html)
        self.assertIn("status:mkvStatus", html)
        self.assertIn("旧版黑屏视频，独立于完整 MKV", html)
        self.assertIn("16*1024*1024", html)
        self.assertIn("最多 16 MiB", html)
        self.assertNotIn("最多 4 MiB", html)


if __name__ == "__main__":
    unittest.main()
