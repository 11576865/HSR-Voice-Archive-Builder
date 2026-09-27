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

    def test_pages_launcher_is_modern_platform_entry(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "docs" / "index.html").read_text(encoding="utf-8")

        self.assertIn(f"v{APP_VERSION}", page)
        self.assertIn("Voice Archive Builder", page)
        self.assertIn("语音归档 / 字幕 / 增量更新", page)
        self.assertIn("Pages 只提供项目入口与启动说明", page)
        self.assertIn('data-platform="windows"', page)
        self.assertIn('data-platform="termux"', page)
        self.assertIn("github.com/11576865/HSR-Voice-Archive-Builder", page)
        self.assertIn("Python 3.11+", page)
        self.assertIn("FFmpeg", page)
        self.assertIn("run_windows.bat", page)
        self.assertIn("run_windows_lan.bat", page)
        self.assertNotIn('id="cmd-windows"', page)
        self.assertIn("run_termux.sh", page)
        self.assertIn("requirements-termux.txt", (root / "run_termux.sh").read_text(encoding="utf-8"))
        self.assertIn("127.0.0.1:8765", page)
        self.assertIn('href="http://127.0.0.1:8765/"', page)
        self.assertIn("打开本地 Web UI", page)
        self.assertIn("continuous.flac", page)
        self.assertIn("ASS / libass", page)
        self.assertIn("Incremental Update", page)
        self.assertIn('role="tabpanel"', page)
        self.assertIn("PLATFORM_KEY='hsr-pages-platform-v1'", page)
        self.assertIn('rel="icon" type="image/svg+xml" href="./favicon.svg?v=1"', page)
        self.assertTrue((root / "docs" / "favicon.svg").is_file())
        self.assertLess(len(page), 22000)

        for stale in (
            "v0.9-H",
            "CURRENT WORKFLOW",
            "RUNTIME MODEL",
            "打开本机控制台",
            'href="#launch">启动',
            "把角色语音整理成可复现的本地档案",
        ):
            self.assertNotIn(stale, page)


if __name__ == "__main__":
    unittest.main()
