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


## Bounded temporal sampling — 2026-10-12

The existing single-frame action now has **检测动态区间（抽样）** in the same subtitle workbench. It reuses **one** synthesized, unsaved ASS for up to 8 real libass sampled instants (server hard maximum 12), each checked using black and white RGB24 backgrounds and the same SafeArea. There is no new timeline or stored domain state.

`subtitle_layout/temporal_samples.py` constructs candidate instants from start/end, midpoint, stratified time anchors, kinetic fades, soft entry, and Karaoke word starts/midpoints/ends when word timings are available. At most 500 alignments contribute events. Sample selection retains important boundaries and fills large temporal gaps. The response names omitted candidates and the maximum gap between samples. It does **not** assign a fictional coverage percentage to continuous time.

FastAPI and Termux Lite expose `POST /api/subtitle-layout/inspect-rendered-interval`. Response includes `scope=bounded_sampled_timestamps_only` and `full_interval_verified=false` unconditionally, with each sampled time, observed ink bbox, status, `violation_count`, `no_visible_ink_count`, `sample_count`, `candidate_count`, `omitted_candidate_count` and `max_unsampled_gap_seconds`. `sampled_violation` is a real sampled detection; `sampled_no_violation` means **no observed sampled violation**, not whole-interval proof; all-blank results are `no_visible_evidence` rather than safe. Renderer exceptions return failure, not partial success.

FastAPI runs this bounded but potentially long rendering request in a worker thread, while Termux Lite already has request threading. Browser aborts prevent presentation of stale responses but do **not** cancel the server's FFmpeg process after launch. The PR's libass workflow runs dedicated real-FFmpeg regression tests, so a standard CI environment lacking FFmpeg is not counted as sufficient test evidence.

Still pending: full temporal raster sweep or proof based on modeled effect extrema; real target device/font parity; end-to-end saved-ASS artifact identity; high-cost long-cue latency and cancellation; detection of completely clipped offscreen glyphs. The RGB16 threshold leaves very faint effects unobserved.
