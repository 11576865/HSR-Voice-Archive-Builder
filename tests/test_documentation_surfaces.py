from __future__ import annotations

import unittest
from pathlib import Path


class DocumentationSurfaceTests(unittest.TestCase):
    def test_readme_is_compact_and_current(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")

        self.assertIn("Current development version:** v0.9-L", readme)
        self.assertIn("continuous.flac", readme)
        self.assertIn("ASS and black MKV are on-demand finished outputs", readme)
        self.assertIn("confirmed incremental `Chinese(PRC)` text as official Chinese target text", readme)
        self.assertIn("docs/reliability.md", readme)
        self.assertLess(len(readme), 12000)
        self.assertNotIn("v0.9-H", readme)

    def test_pages_launcher_is_compact_and_not_a_dashboard_duplicate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "docs" / "index.html").read_text(encoding="utf-8")

        self.assertIn("v0.9-L", page)
        self.assertIn("星穹铁道角色语音归档工具", page)
        self.assertIn("进入本机控制台", page)
        self.assertIn("Termux 日常启动", page)
        self.assertIn("项目会生成什么", page)
        self.assertLess(len(page), 12000)

        for stale in (
            "v0.9-H",
            "CURRENT WORKFLOW",
            "进度不再藏在任务历史",
            "为什么网页不能直接启动 Termux",
            "处理原则",
            "检测到 Android",
        ):
            self.assertNotIn(stale, page)


if __name__ == "__main__":
    unittest.main()
