from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path

from app.project import create_project
from app.reference_workbench import (
    decorate_subtitles,
    export_reference_pack,
    load_reference_annotations,
    resolve_reference_audio,
    save_reference_annotation,
)


def write_wav(path: Path, seconds: float = 5.0, sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * frames)


class ReferenceWorkbenchTests(unittest.TestCase):
    def make_project(self, root: Path):
        wavs = root / "wavs"
        out = root / "output"
        out.mkdir(parents=True, exist_ok=True)
        write_wav(wavs / "chapter_a" / "line.wav", seconds=5.0)
        write_wav(wavs / "chapter_b" / "line.wav", seconds=6.0)
        manifest = {
            "entries": [
                {
                    "index": 1,
                    "filename": "line.wav",
                    "source_member_id": "chapter_a/line.wav",
                    "logical_id": "chapter-a-line",
                    "source_text": "First official line.",
                    "start_seconds": 0.0,
                    "audio_end_seconds": 5.0,
                    "display_end_seconds": 5.0,
                },
                {
                    "index": 2,
                    "filename": "line.wav",
                    "source_member_id": "chapter_b/line.wav",
                    "logical_id": "chapter-b-line",
                    "source_text": "Second official line.",
                    "start_seconds": 5.0,
                    "audio_end_seconds": 11.0,
                    "display_end_seconds": 11.0,
                },
            ]
        }
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        cfg = create_project(
            root,
            name="March7th",
            index_csv="index.csv",
            wav_source="wavs",
            output_dir="output",
        )
        return cfg, out

    def test_annotation_persists_without_mutating_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, out = self.make_project(root)

            saved = save_reference_annotation(
                cfg,
                out,
                {
                    "id": 2,
                    "selected": True,
                    "emotion": "surprised",
                    "intensity": 0.8,
                    "quality": "A",
                },
            )

            self.assertEqual(saved["key"], "member:chapter_b/line.wav")
            annotations = load_reference_annotations(cfg)
            annotation = annotations["member:chapter_b/line.wav"]
            self.assertTrue(annotation["selected"])
            self.assertEqual(annotation["emotion"], "surprised")
            self.assertEqual(annotation["intensity"], 0.8)
            self.assertEqual(annotation["quality"], "A")

            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["entries"][1]["source_text"], "Second official line.")

    def test_decorate_subtitles_exposes_reference_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, out = self.make_project(root)
            save_reference_annotation(
                cfg,
                out,
                {
                    "id": 1,
                    "selected": True,
                    "emotion": "neutral",
                    "intensity": 0.4,
                    "quality": "B",
                },
            )
            subtitles = decorate_subtitles(
                cfg,
                out,
                [
                    {
                        "id": 1,
                        "source_member_id": "chapter_a/line.wav",
                        "logical_id": "chapter-a-line",
                    }
                ],
            )
            self.assertTrue(subtitles[0]["reference_selected"])
            self.assertEqual(subtitles[0]["reference_emotion"], "neutral")
            self.assertEqual(subtitles[0]["reference_intensity"], 0.4)
            self.assertEqual(subtitles[0]["reference_quality"], "B")

    def test_audio_resolution_uses_source_member_id_for_duplicate_basenames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, out = self.make_project(root)
            audio, entry = resolve_reference_audio(cfg, out, 2)
            self.assertEqual(entry["source_member_id"], "chapter_b/line.wav")
            self.assertEqual(audio.relative_to(root / "wavs").as_posix(), "chapter_b/line.wav")

    def test_reference_pack_exports_selected_audio_and_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, out = self.make_project(root)
            save_reference_annotation(
                cfg,
                out,
                {
                    "id": 2,
                    "selected": True,
                    "emotion": "surprised",
                    "intensity": 0.75,
                    "quality": "A",
                },
            )

            report = export_reference_pack(cfg, out, speaker="March7th")
            self.assertEqual(report["selected"], 1)
            self.assertEqual(report["exported"], 1)
            self.assertEqual(report["rejected"], 0)
            self.assertEqual(report["outside_recommended_duration"], 0)

            destination = Path(report["output"])
            self.assertTrue((destination / "audio" / "000001.wav").is_file())
            payload = json.loads(
                (destination / "reference_catalog.json").read_text(encoding="utf-8")
            )
            reference = payload["references"][0]
            self.assertEqual(reference["source_member_id"], "chapter_b/line.wav")
            self.assertEqual(reference["text"], "Second official line.")
            self.assertEqual(reference["emotion"], "surprised")
            self.assertEqual(reference["intensity"], 0.75)
            self.assertEqual(reference["quality"], "A")
            self.assertTrue(reference["recommended_duration"])

    def test_reference_pack_requires_human_selection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, out = self.make_project(root)
            with self.assertRaisesRegex(ValueError, "No reference audio has been selected"):
                export_reference_pack(cfg, out, speaker="March7th")

    def test_zero_intensity_is_preserved_in_review_and_reference_pack(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, out = self.make_project(root)
            save_reference_annotation(
                cfg, out,
                {"id": 1, "selected": True, "emotion": "other", "intensity": 0.0, "quality": "C"},
            )
            subtitle = decorate_subtitles(
                cfg, out, [{"id": 1, "source_member_id": "chapter_a/line.wav"}]
            )[0]
            self.assertEqual(subtitle["reference_intensity"], 0.0)
            report = export_reference_pack(cfg, out)
            catalog = json.loads(Path(report["catalog"]).read_text(encoding="utf-8"))
            self.assertEqual(catalog["references"][0]["intensity"], 0.0)

    def test_legacy_reference_values_are_normalized_without_touching_training_data(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, out = self.make_project(root)
            legacy = {
                "schema_version": 1,
                "entries": {
                    "member:chapter_a/line.wav": {
                        "subtitle_id": "1",
                        "source_member_id": "chapter_a/line.wav",
                        "logical_id": "chapter-a-line",
                        "filename": "line.wav",
                        "selected": True,
                        "emotion": "melancholy",
                        "intensity": 0.6,
                        "quality": "good",
                    }
                },
            }
            (root / "reference_annotations.json").write_text(
                json.dumps(legacy, ensure_ascii=False),
                encoding="utf-8",
            )

            subtitles = decorate_subtitles(
                cfg,
                out,
                [{"id": 1, "source_member_id": "chapter_a/line.wav", "logical_id": "chapter-a-line"}],
            )
            self.assertEqual(subtitles[0]["reference_emotion"], "other")
            self.assertEqual(subtitles[0]["reference_quality"], "A")

            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["entries"][0]["source_text"], "First official line.")

    def test_invalid_annotation_values_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, out = self.make_project(root)
            with self.assertRaisesRegex(ValueError, "Intensity"):
                save_reference_annotation(
                    cfg,
                    out,
                    {
                        "id": 1,
                        "selected": True,
                        "emotion": "neutral",
                        "intensity": 1.5,
                        "quality": "A",
                    },
                )

            with self.assertRaisesRegex(ValueError, "Quality"):
                save_reference_annotation(
                    cfg,
                    out,
                    {
                        "id": 1,
                        "selected": True,
                        "emotion": "neutral",
                        "intensity": 0.5,
                        "quality": "excellent",
                    },
                )


if __name__ == "__main__":
    unittest.main()
