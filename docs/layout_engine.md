# ASS Subtitle Layout Engine

## Current model

The ASS subtitle system is a derived presentation layer over the resolved archive Timeline. SRT remains the always-generated subtitle artifact; ASS is generated on demand from the same final text, timestamps, project settings, and optional word-level timing sidecar.

The engine targets a 1920 × 1080 canvas. The current default safe area is:

- horizontal margin: **3%** on each side (about 58 px);
- vertical margin: **5%** on each side (54 px);
- minimum central gap: **20 px**;
- after reserving the central gap, the remaining safe-area height is divided **60% to the source/primary subtitle region above** and **40% to the Chinese target subtitle region below**.

The browser geometry preview and the Python layout solver use the same 60/40 boundary calculation.

## Modules

- **`safe_area.py`** defines the frame and safe-area bounds.
- **`measure.py`** measures text with a resolved host font when available and falls back to deterministic width estimates.
- **`breaker.py`** performs rule-based line breaking with punctuation and protected-phrase penalties.
- **`font_scale.py`** evaluates the discrete scale sequence 100% → 95% → 90% → 85%.
- **`collision.py`** checks horizontal overflow, top/bottom overflow, and the protected central gap.
- **`layout_solver.py`** places primary/source lines above the central gap and Chinese target lines below it.
- **`ass_writer.py`** writes ASS styles/events, optional readability layers, animation, Karaoke, and Archive HUD.
- **`preview.py`** produces geometry-preview data using the same layout constraints.
- **`app/ass_preview.py`** renders a representative frame through FFmpeg/libass for final visual confirmation.
- **`app/word_alignment.py`** validates and attaches optional word-level timing without mutating canonical manifest text/provenance.

## Fonts and browser preview

The processing host remains authoritative for font discovery and ASS rendering.

The browser UI lists host font families through the local backend. When a font is selected, the dashboard can fetch the resolved installed font file through the authenticated same-origin font endpoint and register it with the browser `FontFace` API. SVG/Canvas geometry preview therefore uses the real host font when the browser supports that font format.

This reduces browser/libass mismatch, but FFmpeg/libass remains the final rendering authority because browser text metrics and libass/FreeType can still differ.

Font files are not committed to the repository.

## Readability controls

The workbench exposes:

- Chinese and primary/source font family;
- Chinese and primary/source base size;
- horizontal and vertical safe margins;
- central gap;
- outline width (`\bord`);
- shadow depth (`\shad`);
- blur radius (`\blur`);
- optional multi-layer soft outline;
- optional translucent vector backdrop;
- backdrop opacity.

ASS `\blur` applies to subtitle glyph/outline/shadow rendering. It does **not** blur the underlying video image.

## Entry and exit motion

Fade and transform motion are separate controls.

The normal fade path uses `\fad(in,out)`, optionally shortened according to the available inter-voice gap.

The optional soft-entry path uses a restrained `\t(...)` transform. Its default shape is approximately:

```ass
\fscx98\fscy98\blur1.5
\t(0,160,\fscx100\fscy100\blur0)
```

If a non-zero base blur is configured, the transform returns to that base blur rather than forcing zero.

No bounce or large positional motion is added by default.

## Karaoke modes

Karaoke is only emitted when complete, monotonic word timing covers the source text exactly enough for the renderer's validation rules. Missing, stale, partial, non-monotonic, or out-of-duration timing automatically falls back to ordinary sentence subtitles.

Available modes:

- **`\k`** — discrete word switching;
- **`\kf`** — smooth fill/sweep behavior provided by libass;
- **dynamic clip** — a separate highlight layer using `\clip(...)` plus timed `\t(...)` changes.

Dynamic clip is currently restricted to a single rendered line. Multi-line text falls back rather than inventing an ambiguous sweep path.

For non-Chinese source projects, word timing normally applies to the primary/source-language subtitle. Chinese target text is not assigned source-language word timing.

## Word-level timing sidecar

Word timing is intentionally separate from canonical manifest text provenance.

Imported timing is stored in:

```text
output/word_alignments.json
```

A record is bound to a manifest entry using stable identity in this order where available:

- `source_member_id`;
- `logical_id`;
- entry id/index;
- a unique filename fallback.

Each cached record also stores a fingerprint of the normalized source text and the source-audio duration. If the source text or duration changes materially, the cache entry is treated as stale and is not reused.

Accepted word records use the form:

```json
{
  "alignments": [
    {
      "id": 17,
      "provider": "external-aligner",
      "words": [
        {"word": "May ", "start": 0.00, "end": 0.31, "confidence": 0.98},
        {"word": "this ", "start": 0.34, "end": 0.57, "confidence": 0.97}
      ]
    }
  ]
}
```

The import path validates:

- non-empty words;
- finite start/end values;
- positive duration;
- monotonic order;
- timing within source-audio duration (with a small tolerance);
- complete normalized source-text coverage.

The cache is attached only in memory after canonical manifest/corrected-CSV persistence. It therefore does not become official/API text provenance and does not rewrite the manifest source text.

## Optional local forced alignment

The workbench can generate missing word timing through the optional local WhisperX provider in `app/local_word_alignment.py`.

This provider is intentionally outside `requirements.txt`. Installing WhisperX also installs or depends on a comparatively large PyTorch/ML stack, so normal archive building, subtitle editing, SRT generation, and ASS without Karaoke do not require it.

Enable it manually on a compatible processing host:

```bash
python -m pip install whisperx
```

The provider:

- uses the project's declared `source_text_language`; `auto` is rejected because language guessing would make alignment semantics ambiguous;
- resolves the original project WAV for each manifest entry;
- uses the existing canonical source text as the forced-alignment transcript;
- preserves already-valid cached alignments unless a forced regeneration is explicitly requested;
- validates every generated result through the same cache validator before accepting it;
- writes accepted results only to `output/word_alignments.json`;
- reports per-entry failures instead of inventing timing;
- runs as a normal background job so long alignment passes appear in the existing task center.

The dashboard endpoint reports whether the optional provider is installed. No large dependency is installed automatically. Inference is local, although WhisperX itself may need to download an alignment model on first use when the required model is not already present in its local cache.

## Word-timing diagnostics

The dashboard reports, per project:

- total subtitle entries;
- usable alignments;
- missing alignments;
- invalid alignments;
- usable percentage;
- provider counts.

A per-entry endpoint returns the validated words only for the requested subtitle.

When Karaoke is enabled, the layout workbench prefers an aligned project subtitle as its sample. The actual ASS preview can pass those real word timings to FFmpeg/libass and render at a user-selected percentage of the subtitle duration, so `\kf` and dynamic clip can be inspected at intermediate time points.

## Archive HUD

Archive HUD is an optional independent ASS layer with lower visual weight than subtitle text. It can display available archive metadata such as:

```text
CHARACTER · GROUP / CHAPTER · #ENTRY
```

The current implementation uses the project character identifier, manifest `group`, and entry id when present. HUD font size and opacity are configurable. It does not alter the subtitle text itself.

## Layer model

The current writer may use multiple ASS layers:

- Layer 0: backdrop cards / primary base and outer-outline events;
- Layer 1: Chinese base and outer-outline events;
- Layer 3: primary dynamic-clip highlight;
- Layer 4: Chinese dynamic-clip highlight;
- Layer 5: Archive HUD.

Exact visual ordering also depends on style/event ordering and whether optional features are enabled.

## Failure behavior

The layout solver tries 100%, 95%, 90%, then 85% scale. If an entry still violates safe-area or central-gap constraints, ASS generation is rejected rather than silently producing a partial archive. The failure is written to `ass_layout_overflow_report.json`.

SRT generation is independent from ASS layout validation and continues to reflect the saved final subtitle text.

## Verification

Run the complete test suite:

```bash
python -m unittest discover -s tests -v
```

For pixel-level ASS verification, use the FFmpeg/libass verification script:

```bash
python scripts/verify_ass_render.py --ass output/HSR_Voice_Archive.ass --out docs/artifacts
```

The browser geometry preview is intended for fast iteration. FFmpeg/libass rendering remains the final check for font selection, outline/blur behavior, motion, Karaoke, clipping, and Archive HUD.
