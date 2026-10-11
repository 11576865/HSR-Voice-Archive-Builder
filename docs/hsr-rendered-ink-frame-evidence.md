# Single-frame FFmpeg/libass rendered-ink evidence

This continues [HSR PR #134](https://github.com/11576865/HSR-Voice-Archive-Builder/pull/134), stacked on #133 and #132.

## Why the timebase matters

The old preview used a 1 fps color source followed by `-ss`. A fractional Karaoke/kinetic timestamp was not guaranteed to be evaluated at that instant: PTS had already been quantized to the one-second input timebase. The snapshot now renders a single frame via `settb=AVTB,setpts=PTS+time/TB,subtitles=...`. Its timestamp has a microsecond-scale timebase. A real FFmpeg/libass motion regression compares two subsecond positions.

## Real, bounded safety evidence

An explicit **检测当前帧边界** action creates an ASS file from the current unsaved trial settings and evaluates the *same frame* on black and white neutral backgrounds via libass. RGB24 frames are compared to their respective neutral baseline. Pixels with absolute channel contrast ≥16/255 form a half-open XY ink bbox, checked against the **same SafeArea** as the geometry solver. White foreground and dark shadow effects are both considered, including any visible card/HUD rendering if enabled.

The API is `POST /api/subtitle-layout/inspect-rendered-ink` on FastAPI and Termux Lite. Its JSON result states `scope=single_timestamp_only`, `evidence=single_frame_dual_background_rgb`, and one of `inside_safe_area`, `outside_safe_area`, `no_visible_ink`. An empty visible frame is **not** treated as verified-safe. All inputs remain in the existing preview intent; no new persisted subtitle state is added. The browser rejects stale requests by project/payload identity and clears results when edit/time changes.

## Explicit limits

- This verifies a **visible-pixel bbox at ONE instant**, not complete character contours, whole karaoke duration, or moving-media acceptance. A fully off-frame glyph cannot be reconstructed from clipped pixels.
- The RGB cutoff ignores very faint glows below the declared threshold. The result is threshold-dependent, not mathematically exact alpha coverage.
- Lack of FFmpeg, absent fonts, libass failure, geometry overflow, decode errors, timeouts or invalid input must be reported as failure/unknown rather than safety success.
- Actual subtitle exposure on the final video can differ when the final render configuration, fonts, and timing differ. Preview evidence never certifies saved exports.
- Existing `scripts/verify_ass_render.py` uses a one-background heuristic and is **not** elevated to this two-background evidence level merely by this addition.

Tests cover black/white contrast, invalid frame sizes, finite timestamps, endpoint wiring, and fractional-time kinetic position through real FFmpeg/libass when available.
