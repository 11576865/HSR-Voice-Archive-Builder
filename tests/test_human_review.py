from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.human_review import HumanReviewRequired, import_review_txt, write_review_txt
from app.jobs import create_job, get_job


class HumanReviewTests(unittest.TestCase):
    def test_review_txt_round_trip_updates_checkpoint_and_clears_pending_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / ".state"
            output = root / "output"
            state.mkdir()
            output.mkdir()
            row = {
                "id": "a.wav",
                "english": "I will not leave.",
                "chinese": "我会离开。",
                "issues": ["negation"],
                "note": "否定关系偏离",
                "hard_failed": True,
            }
            review = output / "semantic_review_required.txt"
            write_review_txt(review, [row])
            text = review.read_text(encoding="utf-8-sig").replace(
                "请删除这一行，并在这里填写修订后的中文字幕。",
                "我不会离开。",
            )
            (state / "semantic_qa.json").write_text(
                json.dumps({"schema_version": 1, "records": [row]}), encoding="utf-8"
            )
            (state / "translation_qa.json").write_text(
                json.dumps({"schema_version": 1, "records": [{"id": "a.wav", "hard_failed": True}]}),
                encoding="utf-8",
            )
            (state / ".translation_checkpoint.json").write_text(
                json.dumps({"schema_version": 3, "records": {"a.wav": {"chinese": "我会离开。"}}}),
                encoding="utf-8",
            )

            result = import_review_txt(
                text,
                state_dir=state,
                output_path=review,
                target_language="zh-CN",
            )

            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["remaining"], 0)
            self.assertTrue(result["ready_to_resume"])
            self.assertFalse(review.exists())
            checkpoint = json.loads((state / ".translation_checkpoint.json").read_text(encoding="utf-8"))
            saved = checkpoint["records"]["a.wav"]
            self.assertEqual(saved["chinese"], "我不会离开。")
            self.assertTrue(saved["manual_override"])
            self.assertGreater(saved["semantic_qa_version"], 0)

    def test_review_txt_rejects_stale_source_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / ".state"
            state.mkdir()
            old_row = {"id": "a.wav", "english": "Old source", "chinese": "译文", "hard_failed": True}
            row = {"id": "a.wav", "english": "New source", "chinese": "译文", "hard_failed": True}
            review = root / "review.txt"
            write_review_txt(review, [old_row])
            text = review.read_text(encoding="utf-8-sig").replace(
                "请删除这一行，并在这里填写修订后的中文字幕。", "修订"
            )
            (state / "semantic_qa.json").write_text(json.dumps({"records": [row]}), encoding="utf-8")
            (state / ".translation_checkpoint.json").write_text(
                json.dumps({"records": {"a.wav": {"chinese": "译文"}}}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "原文指纹不匹配"):
                import_review_txt(text, state_dir=state, output_path=review, target_language="zh-CN")

    def test_review_required_exception_becomes_waiting_job_state(self) -> None:
        path = Path("semantic_review_required.txt")

        def run() -> None:
            raise HumanReviewRequired(path, 2)

        job = create_job("test-review", run)
        deadline = time.time() + 2
        current = None
        while time.time() < deadline:
            current = get_job(job.id)
            if current and current["state"] == "awaiting_input":
                break
            time.sleep(0.01)
        self.assertIsNotNone(current)
        self.assertEqual(current["state"], "awaiting_input")
        self.assertEqual(current["result"]["review_count"], 2)


if __name__ == "__main__":
    unittest.main()
