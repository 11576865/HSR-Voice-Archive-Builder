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

    def test_pages_launcher_is_balanced_technical_entry(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "docs" / "index.html").read_text(encoding="utf-8")

        self.assertIn(f"v{APP_VERSION}", page)
        self.assertIn("Voice Archive Builder", page)
        self.assertIn("本地语音归档与字幕处理工作台", page)
        self.assertIn("GitHub Pages 只提供启动说明与导航", page)
        self.assertIn("Windows", page)
        self.assertIn("Android / Termux", page)
        self.assertIn("Python 3.11+", page)
        self.assertIn("FFmpeg in PATH", page)
        self.assertIn(".\\run_windows.bat", page)
        self.assertIn("run_windows_lan.bat", page)
        self.assertIn("requirements.txt", page)
        self.assertIn("requirements-termux.txt", page)
        self.assertIn("app.launch --lite --no-browser", page)
        self.assertIn("http://127.0.0.1:8765/", page)
        self.assertIn("continuous.flac", page)
        self.assertIn("ASS / libass", page)
        self.assertIn("Incremental Update", page)
        self.assertIn('rel="icon" type="image/svg+xml" href="./favicon.svg?v=1"', page)
        self.assertTrue((root / "docs" / "favicon.svg").is_file())
        self.assertLess(len(page), 24000)

        for stale in (
            "v0.9-H",
            "CURRENT WORKFLOW",
            "进度不再藏在任务历史",
            "为什么网页不能直接启动 Termux",
            "处理原则",
            "检测到 Android",
            "把角色语音整理成可复现的本地档案",
        ):
            self.assertNotIn(stale, page)


if __name__ == "__main__":
    unittest.main()
