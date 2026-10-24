"""Regression for jieba.posseg.pair objects in subtitle line splitting."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_layout.semantic_chunker import _cut_words, split_chinese_semantic


class pair:
    """Match jieba.posseg.pair: iterable, but not subscriptable."""

    def __init__(self, word: str, flag: str):
        self.word = word
        self.flag = flag

    def __iter__(self):
        return iter((self.word, self.flag))


def fake_cut(text: str):
    return (pair(char, "n") for char in text)


class JiebaPairRegressionTests(unittest.TestCase):
    def test_posseg_pairs_are_normalized_to_tuples(self) -> None:
        with patch("subtitle_layout.semantic_chunker.pseg", SimpleNamespace(cut=fake_cut)):
            words = _cut_words("你好吗")
        self.assertEqual(words, [("你", "n"), ("好", "n"), ("吗", "n")])
        self.assertEqual(words[-1][0], "吗")

    def test_orphan_protection_does_not_subscript_jieba_pair(self) -> None:
        # The comma qualifies as a split boundary at 4 characters, but
        # trimming it leaves a three-character orphan. The orphan guard then
        # inspects words1[-1][0], which raised TypeError on raw jieba pairs.
        text = "你好吗，接下来还会有很多内容作为字幕示例"
        with patch("subtitle_layout.semantic_chunker.pseg", SimpleNamespace(cut=fake_cut)):
            result = split_chinese_semantic(
                text, max_chars_per_line=8, min_chars_line=4
            )
        self.assertEqual(result, text)

    def test_archive_build_writes_ass_with_non_subscriptable_pos_pairs(self) -> None:
        """Exercise the real build -> subtitle layout -> ASS writer path."""
        import tempfile
        import wave
        from pathlib import Path

        from app.pipeline import build_project_v02

        chinese = "你好吗，" + "接下来还会有很多内容作为字幕示例" * 4
        pos_inputs: list[str] = []

        def cut(text: str):
            pos_inputs.append(text)
            return (pair(char, "n") for char in text)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wavs = root / "wavs"
            labs = root / "labs"
            wavs.mkdir()
            labs.mkdir()
            name = "archive_evanescia_1"
            with wave.open(str(wavs / f"{name}.wav"), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\\x00\\x00" * 8000)
            (labs / f"{name}.lab").write_text(chinese, encoding="utf-8")
            index = root / "index.csv"
            index.write_text(
                f"filename,english\\n{name}.wav,Hello.\\n",
                encoding="utf-8-sig",
            )
            out_dir = root / "out"
            with patch(
                "subtitle_layout.semantic_chunker.pseg",
                SimpleNamespace(cut=cut),
            ):
                report = build_project_v02(
                    index, wavs, out_dir,
                    chs_source=labs, make_flac=False, generate_ass=True,
                    translate_missing=False,
                )

            self.assertEqual(report["count_total"], 1)
            # Confirm the short-line orphan guard actually consumed POS pairs;
            # a successful build alone could otherwise miss the crash branch.
            self.assertIn("你好吗", pos_inputs)
            ass = (out_dir / "HSR_Voice_Archive.ass").read_text(
                encoding="utf-8-sig"
            )
            self.assertIn("Dialogue:", ass)
            self.assertIn("Hello.", ass)
            self.assertIn("接下来", ass)

    def test_posseg_unavailable_keeps_character_fallback(self) -> None:
        with patch("subtitle_layout.semantic_chunker.pseg", None):
            self.assertEqual(_cut_words("你好"), [("你", ""), ("好", "")])


if __name__ == "__main__":
    unittest.main()
