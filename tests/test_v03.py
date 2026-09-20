from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.diff import classify_names
from app.jobs import create_job, get_job
from app.project import create_project, load_project, project_summary, update_project
from app.remote_index import read_ai_hobbyist_xlsx, read_ai_hobbyist_xlsx_for_filenames


class V03Tests(unittest.TestCase):
    def test_project_config_is_persistent_and_paths_become_relative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            index = root / "index.csv"
            wavs = root / "wavs"
            index.write_text("filename,english\na.wav,Hello\n", encoding="utf-8")
            wavs.mkdir()

            config = create_project(
                root,
                name="Test",
                index_csv=str(index),
                wav_source=str(wavs),
                remote_character="Evanescia",
            )
            self.assertEqual(config.index_csv, "index.csv")
            self.assertEqual(config.wav_source, "wavs")

            update_project(config, same_group_gap=0.5, output_dir="archive-output")
            loaded = load_project(root)
            self.assertEqual(loaded.same_group_gap, 0.5)
            self.assertEqual(loaded.output_dir, "archive-output")
            summary = project_summary(loaded)
            self.assertFalse(summary["has_manifest"])

    def test_remote_index_parser_filters_character_and_normalizes_wav_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "EN.xlsx"
            wb = Workbook()
            ws = wb.active
            ws.append(["语音哈希", "语音文件名", "角色", "语音文本", "数据来源", "是否为战斗语音"])
            ws.append(["111", "chapter5_42_evanescia_101", "Evanescia", "Little Raccoon...", "", ""])
            ws.append(["222", "chapter5_42_other_101", "Other", "No.", "", ""])
            wb.save(path)
            wb.close()

            rows = read_ai_hobbyist_xlsx(path, "evanescia")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["filename"], "chapter5_42_evanescia_101.wav")
            self.assertEqual(rows[0]["english"], "Little Raccoon...")

    def test_remote_index_parser_can_match_filename_across_localized_role_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "EN.xlsx"
            wb = Workbook()
            ws = wb.active
            ws.append(["语音哈希", "语音文件名", "角色", "语音文本", "数据来源", "是否为战斗语音"])
            ws.append(["111", "chapter5_42_evanescia_101", "绯英", "Little Raccoon...", "", ""])
            ws.append(["222", "chapter5_42_other_101", "其他", "No.", "", ""])
            wb.save(path)
            wb.close()

            rows = read_ai_hobbyist_xlsx_for_filenames(
                path, {"chapter5_42_evanescia_101.wav"}
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["character"], "绯英")
            self.assertEqual(rows[0]["english"], "Little Raccoon...")

    def test_variant_aware_name_classification_deduplicates_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest = Path(td) / "manifest.json"
            manifest.write_text(
                json.dumps({"entries": [{"filename": "chapter5_3_evanescia_125_f.wav"}]}),
                encoding="utf-8",
            )
            result = classify_names(
                manifest,
                [
                    "chapter5_3_evanescia_125.wav",
                    "chapter5_42_evanescia_101.wav",
                    "chapter5_42_evanescia_101.wav",
                ],
            )
            self.assertEqual(result["counts"]["variant_of_existing"], 1)
            self.assertEqual(result["counts"]["new_logical"], 1)
            self.assertEqual(result["candidate_unique_count"], 2)

    def test_background_job_completes(self) -> None:
        job = create_job("test", lambda: {"value": 42})
        deadline = time.time() + 3
        state = None
        while time.time() < deadline:
            state = get_job(job.id)
            if state and state["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        self.assertIsNotNone(state)
        self.assertEqual(state["state"], "succeeded")
        self.assertEqual(state["result"], {"value": 42})


if __name__ == "__main__":
    unittest.main()
