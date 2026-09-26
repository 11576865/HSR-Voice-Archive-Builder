import csv
import hashlib
import wave
import pytest
from pathlib import Path
from types import SimpleNamespace

from app import lite_server


def test_lite_export_endpoint_uses_corrected_text_and_can_export_twice(tmp_path, monkeypatch):
    source = tmp_path / "wav" / "chapter" / "line.wav"
    source.parent.mkdir(parents=True)
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 9600)
    output = tmp_path / "output"
    output.mkdir()
    for filename, fields, row in [
        ("manifest.csv", ["index", "filename", "source_member_id", "english"],
         ["1", "line.wav", "chapter/line.wav", "Original"]),
        ("bilingual_index_corrected.csv", ["index", "filename", "source_member_id", "source_text"],
         ["1", "line.wav", "chapter/line.wav", "Corrected"]),
    ]:
        with (output / filename).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerow(row)
    monkeypatch.setattr(
        lite_server, "_active_config",
        lambda: SimpleNamespace(name="Firefly", root=str(tmp_path), wav_source="wav", output_dir="output"),
    )

    class Handler:
        path = "/api/project/active/export/gpt-sovits?speaker=Firefly&language=en"

        def _json(self, result):
            self.result = result

    for _ in range(2):
        handler = Handler()
        lite_server.Handler._handle_api_post(
            handler, "/api/project/active/export/gpt-sovits", {},
        )
        assert handler.result["ok"]
        report = handler.result["report"]
        assert (report["exported"], report["rejected"]) == (1, 0)
        audio_path, speaker, language, text = Path(report["list_file"]).read_text(encoding="utf-8").strip().split("|")
        assert (speaker, language, text) == ("Firefly", "en", "Corrected")
        assert Path(audio_path).is_file()
        assert Path(audio_path).parent.parent == Path(report["output"])
        with Path(report["provenance_file"]).open(encoding="utf-8-sig") as stream:
            provenance = list(csv.DictReader(stream))
        assert provenance[0]["source_member_id"] == "chapter/line.wav"
        assert provenance[0]["audio_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize("source_language,audio_language", [
    ("zh-CN", "zh-CN"), ("en", "zh-CN"),
])
def test_export_rejects_language_mismatch(tmp_path, source_language, audio_language):
    from app.gpt_sovits_exporter import export_project_dataset

    config = SimpleNamespace(
        name="Voice", root=str(tmp_path), wav_source="wavs", output_dir="output",
        source_text_language=source_language, audio_language=audio_language,
    )
    with pytest.raises(ValueError, match="requires English"):
        export_project_dataset(config, language="en")
    assert not (tmp_path / "output").exists()


def test_export_rejects_audio_changed_since_archive_build(tmp_path):
    from app.gpt_sovits_exporter import export_gpt_sovits_dataset

    wav_root = tmp_path / "wavs"
    wav_root.mkdir()
    source = wav_root / "line.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 9600)
    report = export_gpt_sovits_dataset([{
        "index": "1", "filename": "line.wav", "source_text": "Hello",
        "sha256": "a" * 64,
    }], wav_root, tmp_path / "export", speaker="Voice")
    assert report["exported"] == 0
    with (tmp_path / "export" / "rejected.csv").open(encoding="utf-8-sig") as stream:
        assert list(csv.DictReader(stream))[0]["reason"] == "audio_changed_since_build"
