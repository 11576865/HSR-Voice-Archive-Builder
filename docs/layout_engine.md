# ASS Subtitle Layout Engine

## Overview & Architecture

The ASS Subtitle Layout Engine formats bilingual (Chinese primary and English/other primary) continuous voice subtitles for 1920x1080 video output. It produces ASS dialogue events mapped accurately to audio timestamps with safe area margins and rule-based line breaking.

### Module Responsibilities

- **`safe_area.py`**: Defines the $1920 \times 1080$ canvas dimensions and safe area margins ($10\%$ horizontal margin = $192\text{px}$, $5\%$ vertical margin = $54\text{px}$).
  - Horizontal bounds: $192\text{px} \le x \le 1728\text{px}$ (Maximum printable width: $1536\text{px}$)
  - Vertical bounds: $54\text{px} \le y \le 1026\text{px}$ (Maximum printable height: $972\text{px}$)
- **`measure.py`**: Character-width and line-height measurement functions based on CJK/Latin character width ratios.
- **`breaker.py`**: Rule-based scoring line breaker with backtracking. Breaks text at optimal punctuation or whitespace boundaries while preserving protected phrases.
- **`font_scale.py`**: Discrete font scale factor evaluation ($100\% \to 95\% \to 90\% \to 85\%$).
- **`collision.py`**: Symmetric 4-bound collision detection for both Chinese and Primary text blocks (top overflow, bottom overflow, horizontal overflow, and central gap safety check).
- **`layout_solver.py`**: Iteratively calculates text block positions and font scales, returning a `SolvedLayout` or marking extreme overflow when $85\%$ scale fails safe-area constraints.
- **`ass_writer.py`**: Generates full ASS script headers, styles (`CHS`, `Primary`), and dialogue lines with absolute alignment/position tags (`\an8\pos(x,y)\fs...`). Writes `ass_layout_overflow_report.json` if invalid layouts occur.

---

## Output Architecture & Pipeline Switch

ASS generation is opt-in to decouple subtitle processing dependencies:
- Default output format remains **SRT** (`HSR_Voice_Archive.srt`).
- Toggle `generate_ass` option in `ProjectConfig`, CLI (`--generate-ass`), Quick Mode, and Advanced Project Settings.
- When `generate_ass=False` (default), ASS generation and `subtitle_layout` execution are skipped.
- When `generate_ass=True`, `HSR_Voice_Archive.ass` is built alongside SRT.

---

## Summary of Modified & Added Files

| File | Changes Made |
| :--- | :--- |
| `app/project.py` | Added `generate_ass: bool = False` to `ProjectConfig` and `ass_layout_overflow_report.json` to summary outputs. |
| `app/builder.py` | Added `generate_ass` parameter to `write_manifest()`. |
| `app/pipeline.py` | Propagated `generate_ass` CLI flag and pipeline configuration option. |
| `app/subtitles.py` | Updated `update_project_subtitles()` to honor `config.generate_ass` and exposed overflow flags in `get_project_subtitles()`. |
| `app/quick.py` | Set default `generate_ass=False` during quick project creation. |
| `app/static/index.html` | Added ASS generation checkbox toggle to Advanced Project Settings. |
| `subtitle_layout/breaker.py` | Implemented scoring-based line breaker with punctuation priorities and protected phrase penalties. |
| `subtitle_layout/collision.py` | Implemented symmetric 4-bound safety area check (`check_bilingual_collision_with_reason`). |
| `subtitle_layout/layout_solver.py` | Added failure tracking and scale attempt history when $85\%$ font scale fails bounds. |
| `subtitle_layout/ass_writer.py` | Added `ass_layout_overflow_report.json` generation upon layout failures. |
| `scripts/verify_ass_render.py` | Added automated headless FFmpeg render verification script with PNG pixel bounds analysis. |
| `tests/test_subtitle_layout.py` | Added comprehensive unit tests for scoring line breaking, 4-bound collision checks, render verification helpers, and pipeline options. |

---

## Sample ASS Output

```ass
[Script Info]
Title: HSR Voice Archive
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CHS,汉仪旗黑,52,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,0,8,192,192,54,1
Style: Primary,Noto Sans,42,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,0,8,192,192,54,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 1,0:00:05.00,0:00:07.50,CHS,,0,0,0,,{\an8\pos(960,54)\fs52}愿此行，终抵群星。
Dialogue: 0,0:00:05.00,0:00:07.50,Primary,,0,0,0,,{\an8\pos(960,1026)\fs42}May this journey lead us starward.
```

---

## Player Compatibility Strategy (libass)

- **Encoding**: UTF-8 with BOM (`utf-8-sig`) is enforced for maximum compatibility across Windows media players (mpv, VLC, MPC-HC) and libass builds.
- **Font Fallback Hierarchy**: Recommended system font stack includes `汉仪旗黑` / `Noto Sans CJK SC` / `Source Han Sans CN` for Chinese subtitles and `Noto Sans` / `Arial` for Primary text.
- **Positioning**: Uses standard ASS v4.00+ `\an8` (top-center alignment) paired with explicit `\pos(x,y)` coordinates relative to the $1920 \times 1080$ frame.

---

## Verification & Test Results

Run unit test suite:
```bash
python3 -m unittest discover -s tests
```
Result: **182 tests passed**.

Run automated ASS render verification (requires FFmpeg):
```bash
python3 scripts/verify_ass_render.py --ass output/HSR_Voice_Archive.ass --out docs/artifacts
```
Result: **PASS: Zero visual bleed into safe area margins**.
