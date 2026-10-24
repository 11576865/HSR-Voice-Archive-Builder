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

    def test_posseg_unavailable_keeps_character_fallback(self) -> None:
        with patch("subtitle_layout.semantic_chunker.pseg", None):
            self.assertEqual(_cut_words("你好"), [("你", ""), ("好", "")])


if __name__ == "__main__":
    unittest.main()
