from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ssl
import urllib.error
from app.remote_index import (
    REMOTE_INDEX_LOCAL_FILE_ENV,
    _download_remote_xlsx,
    _workbook_cache_paths,
    fetch_ai_hobbyist_index_for_filenames_cached,
    get_ai_hobbyist_workbook,
)

TEST_URL = "https://example.test/Indexs/EN.xlsx"


def make_workbook_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["语音哈希", "语音文件名", "角色", "语音文本"])
    ws.append(["h1", "a.wav", "Test", "Line A"])
    ws.append(["h2", "b.wav", "Test", "Line B"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def expected_record(filename: str, english: str, hash_value: str) -> dict[str, str]:
    return {
        "filename": filename,
        "hash": hash_value,
        "character": "Test",
        "english": english,
        "battle": "",
    }


def fake_download(payload: bytes):
    def _download(url: str, timeout=None, path: Path = None, **kwargs) -> None:
        target = path if path is not None else kwargs.get("path")
        Path(target).write_bytes(payload)

    return _download


class WorkbookCacheTests(unittest.TestCase):
    def test_workbook_is_downloaded_once_and_shared_across_filename_sets(self) -> None:
        payload = make_workbook_bytes()
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            with patch(
                "app.remote_index._download_remote_xlsx",
                side_effect=fake_download(payload),
            ) as download:
                first, first_meta = fetch_ai_hobbyist_index_for_filenames_cached(
                    {"a.wav"}, url=TEST_URL, cache_dir=cache_dir
                )
                second, second_meta = fetch_ai_hobbyist_index_for_filenames_cached(
                    {"b.wav"}, url=TEST_URL, cache_dir=cache_dir
                )

        self.assertEqual(first, [expected_record("a.wav", "Line A", "h1")])
        self.assertEqual(second, [expected_record("b.wav", "Line B", "h2")])
        self.assertEqual(download.call_count, 1)
        self.assertFalse(first_meta["cache_hit"])
        self.assertTrue(second_meta["cache_hit"])
        self.assertFalse(second_meta["stale"])
        self.assertGreaterEqual(second_meta["age_seconds"], 0.0)

    def test_failing_refresh_falls_back_to_stale_workbook(self) -> None:
        payload = make_workbook_bytes()
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            with patch(
                "app.remote_index._download_remote_xlsx",
                side_effect=fake_download(payload),
            ):
                fetch_ai_hobbyist_index_for_filenames_cached(
                    {"a.wav"}, url=TEST_URL, cache_dir=cache_dir
                )
            with patch(
                "app.remote_index._download_remote_xlsx",
                side_effect=OSError("offline"),
            ):
                records, meta = fetch_ai_hobbyist_index_for_filenames_cached(
                    {"a.wav"},
                    url=TEST_URL,
                    cache_dir=cache_dir,
                    max_age_seconds=-1,
                )
                with self.assertRaises(OSError):
                    fetch_ai_hobbyist_index_for_filenames_cached(
                        {"a.wav"},
                        url=TEST_URL,
                        cache_dir=cache_dir,
                        max_age_seconds=-1,
                        max_stale_age_seconds=-1,
                    )

        self.assertEqual(records, [expected_record("a.wav", "Line A", "h1")])
        self.assertTrue(meta["cache_hit"])
        self.assertTrue(meta["stale"])

    def test_corrupt_or_mismatched_cache_meta_is_ignored(self) -> None:
        payload = make_workbook_bytes()
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            with patch(
                "app.remote_index._download_remote_xlsx",
                side_effect=fake_download(payload),
            ) as download:
                fetch_ai_hobbyist_index_for_filenames_cached(
                    {"a.wav"}, url=TEST_URL, cache_dir=cache_dir
                )
                _, meta_path = _workbook_cache_paths(TEST_URL, cache_dir)
                meta_path.write_text(
                    '{"schema_version": 1, "url": "https://other.test/EN.xlsx"}',
                    encoding="utf-8",
                )
                fetch_ai_hobbyist_index_for_filenames_cached(
                    {"a.wav"}, url=TEST_URL, cache_dir=cache_dir
                )

        self.assertEqual(download.call_count, 2)

    def test_local_file_env_override_skips_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workbook = root / "EN.xlsx"
            workbook.write_bytes(make_workbook_bytes())
            with patch.dict(
                os.environ, {REMOTE_INDEX_LOCAL_FILE_ENV: str(workbook)}
            ), patch(
                "app.remote_index._download_remote_xlsx",
                side_effect=OSError("must not download"),
            ) as download:
                records, meta = fetch_ai_hobbyist_index_for_filenames_cached(
                    {"a.wav", "b.wav"}, url=TEST_URL, cache_dir=root / "cache"
                )
                path, direct_meta = get_ai_hobbyist_workbook(
                    TEST_URL, cache_dir=root / "cache"
                )

        self.assertEqual(
            records,
            [
                expected_record("a.wav", "Line A", "h1"),
                expected_record("b.wav", "Line B", "h2"),
            ],
        )
        self.assertTrue(meta["cache_hit"])
        self.assertTrue(meta.get("local_file"))
        self.assertEqual(download.call_count, 0)
        self.assertEqual(path, workbook)
        self.assertTrue(direct_meta.get("local_file"))

    def test_local_file_env_override_requires_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.xlsx"
            with patch.dict(os.environ, {REMOTE_INDEX_LOCAL_FILE_ENV: str(missing)}):
                with self.assertRaises(FileNotFoundError):
                    get_ai_hobbyist_workbook(TEST_URL, cache_dir=Path(td) / "cache")

    def test_download_retry_on_transient_error(self) -> None:
        payload = make_workbook_bytes()
        attempts = 0

        def failing_then_succeeding_download(req, timeout=15.0):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ssl.SSLError("SSL unexpected EOF occurred")
            # Create a mock response with valid workbook bytes
            mock_resp = unittest.mock.MagicMock()
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.status = 200
            mock_resp.geturl.return_value = TEST_URL
            mock_resp.headers = {}
            mock_resp.read.side_effect = [payload, b""]
            return mock_resp

        with tempfile.TemporaryDirectory() as td:
            target_path = Path(td) / "downloaded.xlsx"
            with patch("urllib.request.urlopen", side_effect=failing_then_succeeding_download), patch(
                "time.sleep"
            ) as mock_sleep:
                _download_remote_xlsx(TEST_URL, (5.0, 10.0), target_path, max_attempts=5, retry_delay=0.1)

            self.assertEqual(attempts, 3)
            self.assertTrue(target_path.is_file())
            self.assertEqual(mock_sleep.call_count, 2)

    def test_fallback_to_stale_cache_on_failure(self) -> None:
        payload = make_workbook_bytes()
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            # First populate cache
            with patch(
                "app.remote_index._download_remote_xlsx",
                side_effect=fake_download(payload),
            ):
                get_ai_hobbyist_workbook(TEST_URL, cache_dir=cache_dir)

            # Subsequent download fails completely
            with patch(
                "app.remote_index._download_remote_xlsx",
                side_effect=RuntimeError("Download failed completely"),
            ):
                path, meta = get_ai_hobbyist_workbook(
                    TEST_URL,
                    cache_dir=cache_dir,
                    max_age_seconds=-1,  # Force refresh attempt
                )

            self.assertTrue(meta["cache_hit"])
            self.assertTrue(meta["stale"])
            self.assertTrue(path.is_file())

    def test_corrupted_download_rejection(self) -> None:
        payload = make_workbook_bytes()
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            # Pre-populate valid cache
            with patch(
                "app.remote_index._download_remote_xlsx",
                side_effect=fake_download(payload),
            ):
                get_ai_hobbyist_workbook(TEST_URL, cache_dir=cache_dir)

            # Attempt download that writes corrupted file
            corrupt_bytes = b"not a valid zip or xlsx file"
            with patch(
                "app.remote_index._download_remote_xlsx",
                side_effect=fake_download(corrupt_bytes),
            ):
                path, meta = get_ai_hobbyist_workbook(
                    TEST_URL,
                    cache_dir=cache_dir,
                    max_age_seconds=-1,  # Force refresh
                )

            # Cache fallback should succeed and the cache file should still contain valid workbook payload
            self.assertTrue(meta["cache_hit"])
            self.assertTrue(meta["stale"])
            self.assertEqual(path.read_bytes(), payload)

    def test_detailed_error_message_no_cache(self) -> None:
        def failing_download(req, timeout=15.0):
            raise urllib.error.URLError("Connection refused")

        with tempfile.TemporaryDirectory() as td:
            target_path = Path(td) / "test.xlsx"
            with patch("urllib.request.urlopen", side_effect=failing_download), patch("time.sleep"):
                with self.assertRaises(RuntimeError) as ctx:
                    _download_remote_xlsx(
                        TEST_URL,
                        (5.0, 10.0),
                        target_path,
                        max_attempts=3,
                        retry_delay=0.1,
                        has_cache_fallback=False,
                    )

            err_msg = str(ctx.exception)
            self.assertIn("Remote index download failed", err_msg)
            self.assertIn(f"URL: {TEST_URL}", err_msg)
            self.assertIn("Attempt: 3/3", err_msg)
            self.assertIn("Connection refused", err_msg)
            self.assertIn("Fallback: No cache available", err_msg)


if __name__ == "__main__":
    unittest.main()
