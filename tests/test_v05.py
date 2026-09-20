from __future__ import annotations

import os
import shutil
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from app.builder import Entry, _parse_7z_slt, build_continuous_flac
from app.security import api_token
from app.pipeline import _translate_missing
from app.preflight import dependency_status
from app.remote_index import read_ai_hobbyist_xlsx
from app.server import app
from app.wavpcm import PCM_SUBFORMAT_GUID_LE, parse_wav_pcm
from openpyxl import Workbook


def write_extensible_pcm16(path: Path, samples: list[int], sample_rate: int = 8000) -> None:
    channels = 1
    bits = 16
    block_align = channels * (bits // 8)
    byte_rate = sample_rate * block_align
    pcm = b"".join(int(x).to_bytes(2, "little", signed=True) for x in samples)
    fmt = (
        struct.pack(
            "<HHIIHHH",
            0xFFFE,
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits,
            22,
        )
        + struct.pack("<HI", bits, 0)
        + PCM_SUBFORMAT_GUID_LE
    )
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def make_entry(filename: str, frames: int) -> Entry:
    sr = 8000
    return Entry(
        index=1,
        group="g",
        filename=filename,
        source="test",
        source_detail="",
        english="Hello",
        chinese="你好",
        chinese_source="test",
        sample_rate=sr,
        channels=1,
        sample_width_bits=16,
        source_frames=frames,
        source_duration_seconds=frames / sr,
        start_sample=0,
        audio_end_sample=frames,
        next_start_sample=frames,
        start_seconds=0,
        audio_end_seconds=frames / sr,
        display_end_seconds=frames / sr,
        sha256="",
    )


class V05SecurityAndWavTests(unittest.TestCase):
    def test_local_api_requires_process_token_and_rejects_bad_host(self) -> None:
        old_lan = os.environ.get("HSR_VOICE_LAN_MODE")
        old_allowed = os.environ.get("HSR_VOICE_ALLOWED_HOSTS")
        try:
            os.environ["HSR_VOICE_LAN_MODE"] = "0"
            os.environ["HSR_VOICE_ALLOWED_HOSTS"] = "127.0.0.1,localhost,::1"
            token = api_token()
            with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertNotIn("__HSR_API_TOKEN__", page.text)
                self.assertIn(token, page.text)

                denied = client.get("/api/status")
                self.assertEqual(denied.status_code, 401)

                allowed = client.get("/api/status", headers={"X-HSR-Token": token})
                self.assertEqual(allowed.status_code, 200)

                bad_host = client.get(
                    "/",
                    headers={"host": "evil.example"},
                )
                self.assertEqual(bad_host.status_code, 400)
        finally:
            if old_lan is None:
                os.environ.pop("HSR_VOICE_LAN_MODE", None)
            else:
                os.environ["HSR_VOICE_LAN_MODE"] = old_lan
            if old_allowed is None:
                os.environ.pop("HSR_VOICE_ALLOWED_HOSTS", None)
            else:
                os.environ["HSR_VOICE_ALLOWED_HOSTS"] = old_allowed

    def test_lan_dashboard_requires_entry_token(self) -> None:
        old_lan = os.environ.get("HSR_VOICE_LAN_MODE")
        old_token = os.environ.get("HSR_VOICE_TOKEN")
        old_allowed = os.environ.get("HSR_VOICE_ALLOWED_HOSTS")
        try:
            os.environ["HSR_VOICE_LAN_MODE"] = "1"
            os.environ["HSR_VOICE_TOKEN"] = "temporary-test-token"
            os.environ["HSR_VOICE_ALLOWED_HOSTS"] = "127.0.0.1"
            with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                denied = client.get("/")
                self.assertEqual(denied.status_code, 401)
                granted = client.get("/?token=temporary-test-token")
                self.assertEqual(granted.status_code, 200)
                refreshed = client.get("/")
                self.assertEqual(refreshed.status_code, 200)
        finally:
            if old_lan is None:
                os.environ.pop("HSR_VOICE_LAN_MODE", None)
            else:
                os.environ["HSR_VOICE_LAN_MODE"] = old_lan
            if old_token is None:
                os.environ.pop("HSR_VOICE_TOKEN", None)
            else:
                os.environ["HSR_VOICE_TOKEN"] = old_token
            if old_allowed is None:
                os.environ.pop("HSR_VOICE_ALLOWED_HOSTS", None)
            else:
                os.environ["HSR_VOICE_ALLOWED_HOSTS"] = old_allowed


    def test_native_7z_listing_parser_ignores_archive_header(self) -> None:
        sample = """Path = archive.7z
Type = 7z

----------
Path = folder/file.wav
Size = 123
Attributes = A_ -rw-r--r--

Path = folder/line.lab
Size = 8
Attributes = A_ -rw-r--r--
"""
        records = _parse_7z_slt(sample)
        self.assertEqual([r["Path"] for r in records], ["folder/file.wav", "folder/line.lab"])
        self.assertEqual(records[0]["Size"], "123")

    def test_translation_checkpoint_survives_later_batch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            checkpoint = Path(td) / ".translation_checkpoint.json"
            entries = [
                SimpleNamespace(filename="a.wav", english="Alpha", chinese="", chinese_source="missing"),
                SimpleNamespace(filename="b.wav", english="Beta", chinese="", chinese_source="missing"),
            ]

            def flaky(batch, model, client):
                if batch[0]["id"] == "b.wav":
                    raise RuntimeError("simulated 429")
                return [{"id": "a.wav", "chinese": "阿尔法"}]

            with patch("app.translator.make_client", return_value=object()), patch(
                "app.translator.translate_records", side_effect=flaky
            ):
                with self.assertRaises(RuntimeError):
                    _translate_missing(entries, "test-model", 1, checkpoint)

            self.assertTrue(checkpoint.is_file())
            saved = checkpoint.read_text(encoding="utf-8")
            self.assertIn("a.wav", saved)
            self.assertNotIn('"b.wav"', saved)

            def stable(batch, model, client):
                self.assertEqual(batch[0]["id"], "b.wav")
                return [{"id": "b.wav", "chinese": "贝塔"}]

            with patch("app.translator.make_client", return_value=object()), patch(
                "app.translator.translate_records", side_effect=stable
            ):
                report = _translate_missing(entries, "test-model", 1, checkpoint)

            self.assertEqual(report["count_gpt_checkpoint_reused"], 1)
            self.assertEqual(report["count_gpt_api_translated"], 1)
            self.assertEqual([e.chinese for e in entries], ["阿尔法", "贝塔"])

    def test_xlsx_container_expansion_limit_is_checked_before_openpyxl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "index.xlsx"
            wb = Workbook()
            ws = wb.active
            ws.append(["语音哈希", "语音文件名", "角色", "语音文本"])
            ws.append(["1", "a", "Evanescia", "Hello"])
            wb.save(path)
            wb.close()

            with patch("app.remote_index.MAX_XLSX_UNCOMPRESSED_BYTES", 1):
                with self.assertRaises(ValueError):
                    read_ai_hobbyist_xlsx(path, "Evanescia")

    def test_termux_preflight_does_not_require_openai_sdk(self) -> None:
        import app.preflight as preflight

        real_import = preflight.importlib.import_module
        real_which = shutil.which

        def fake_import(name, *args, **kwargs):
            if name == "openai":
                raise ImportError("simulated Android jiter incompatibility")
            return real_import(name, *args, **kwargs)

        with patch.dict(
            os.environ,
            {"TERMUX_VERSION": "0.119", "PREFIX": "/data/data/com.termux/files/usr"},
            clear=False,
        ), patch("app.preflight.shutil.which") as which, patch(
            "app.preflight.importlib.import_module",
            side_effect=fake_import,
        ):
            which.side_effect = lambda name: (
                "/data/data/com.termux/files/usr/bin/7zz"
                if name in {"7zz", "7z"}
                else "/data/data/com.termux/files/usr/bin/ffmpeg"
                if name == "ffmpeg"
                else real_which(name)
            )
            status = dependency_status()

        self.assertTrue(status["ok"], status["issues"])
        self.assertFalse(status["openai_sdk"])
        self.assertTrue(any("OpenAI Python SDK" in x for x in status["warnings"]))

    def test_runtime_preflight_dependencies_are_importable(self) -> None:
        status = dependency_status()
        self.assertTrue(status["ok"], status["issues"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_extensible_pcm_wav_works_on_supported_python_versions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            wavs.mkdir()
            source = wavs / "extensible.wav"
            samples = [100, -100, 200, -200] * 200
            write_extensible_pcm16(source, samples)

            info = parse_wav_pcm(source)
            self.assertTrue(info.extensible)
            self.assertEqual(info.frames, len(samples))
            self.assertEqual(info.valid_bits_per_sample, 16)

            output = root / "continuous.flac"
            report = build_continuous_flac(
                [make_entry(source.name, len(samples))],
                wavs,
                output,
            )
            self.assertTrue(output.is_file())
            self.assertTrue(report["lossless_pcm_verified"])
            self.assertEqual(
                report["assembled_pcm_sha256"],
                report["decoded_flac_pcm_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
