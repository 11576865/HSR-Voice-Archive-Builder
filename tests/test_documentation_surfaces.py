from __future__ import annotations

import unittest
from pathlib import Path

from app.version import APP_VERSION


class DocumentationSurfaceTests(unittest.TestCase):
    def test_readme_is_compact_and_current(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")

        self.assertIn(f"Current development version:** v{APP_VERSION}", readme)
        self.assertIn("continuous.flac", readme)
        self.assertIn("ASS and black MKV are on-demand finished outputs", readme)
        self.assertIn("confirmed incremental `Chinese(PRC)` text as official Chinese target text", readme)
        self.assertIn("docs/reliability.md", readme)
        self.assertLess(len(readme), 12000)
        self.assertNotIn("v0.9-H", readme)

    def test_pages_launcher_is_compact_and_not_a_dashboard_duplicate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "docs" / "index.html").read_text(encoding="utf-8")

        self.assertIn(f"v{APP_VERSION}", page)
        self.assertIn("Voice Archive Builder", page)
        self.assertIn("Local-first archive workbench", page)
        self.assertIn("进入本机控制台", page)
        self.assertIn("日常启动", page)
        self.assertIn("成品", page)
        self.assertIn('rel="icon" type="image/svg+xml" href="./favicon.svg?v=1"', page)
        self.assertTrue((root / "docs" / "favicon.svg").is_file())
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
