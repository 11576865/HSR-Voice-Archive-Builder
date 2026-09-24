# Palette's Journal - Critical UX & Accessibility Learnings

## 2026-03-31 - Subtitle Navigation & UI Contract Testing
**Learning:** `test_progress_ui.py` contains contract assertions (`test_core_flac_ass_and_subtitle_editor_ui_contracts`) that strictly enforce clean button text and element-level event binding rather than global `document.addEventListener('keydown'`).
**Action:** Always scope shortcut keydown listeners directly to interactive input elements (like `#subFinalEditor`) and attach shortcut hints via `aria-label` and `title` attributes rather than embedding text directly inside button labels.
