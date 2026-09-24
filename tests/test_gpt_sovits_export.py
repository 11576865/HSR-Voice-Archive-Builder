import csv
import wave
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
