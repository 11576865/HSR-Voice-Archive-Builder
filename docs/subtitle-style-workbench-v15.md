# HSR Subtitle Style Workbench — UI v15

Date: 2026-10-10  
Scope: `app/static/index.html`, workbench UI tests, responsive UIGS capture contract.

## Product intention

Keep the subtitle workbench a *single editing surface*, not another tab or another simplified product. The existing 1920×1080 subtitle geometry preview and FFmpeg/libass verification still operate on the same unsaved trial settings. The real-render preview does **not** save those settings. Saving settings and generating an ASS remain explicit, separate operations.

This iteration adds a three-stage information hierarchy:

1. **Style trial** — immediate editable text, fonts, margins, readability and effect parameters.
2. **Render evidence** — the large 16:9 preview has a persistent evidence panel that names the origin and currency of the displayed pixels.
3. **Persist / publish** — existing Save Settings and Save & Generate ASS actions remain at the top of the Inspector.

## Production UI changes

- Desktop keeps a broad 16:9 primary stage and bounded right-side inspector; the stage region and inspector independently stay available during vertical scrolling.
- Tablet reflows into preview followed by a two-column inspector.
- Phone moves to preview followed by the one-column inspector. The primary actions remain reachable without a separate UI mode.
- Primary editor controls are more legible and have less cramped hit areas; typography and workbench hierarchy have been redrawn.
- The new evidence panel explicitly separates browser **geometry** (layout-solver preview) from **actual FFmpeg/libass** pixels. It reports `geometry`, `rendering`, `libass`, `stale` or `failed`.
- Geometric success, a visually rendered frame, saved configuration and exported ASS are distinct states, not interchangeable verification levels.
- Original control IDs, payload fields, endpoint URLs and subtitle engine are retained.

## Fixed race / ownership boundaries

**Real renderer**: every request uses a monotonically increasing `libassPreviewGeneration` and a project + payload identity. Edit, project change, invalidation or explicit switch to geometry takes precedence over a previously started async render. Old responses cannot install an image URL, change the visible mode or report success/failure for the new trial.

**Geometry renderer**: `geometryPreviewGeneration` changes as soon as a delayed preview is queued, not just when it starts. A late response from an older edit is discarded before updating SVG or corpus status.

**Saved settings**: the response to Save Settings is associated with the exact submitted payload. The UI does not mark a newer edit as saved simply because the previous POST succeeded.

This is a UI/application-state slice only. It does not add cancellation or global server-side backpressure to FFmpeg/libass jobs; a stale result can still consume backend resources before being discarded.

## Evidence and acceptance

- `tests/test_subtitle_style_workbench_ui.py`: stable IDs, redesigned hierarchy, modes, generation guards, snapshots and responsive CSS contracts.
- `tests/test_subtitle_style_preview_races.py`: Node VM exercises the actual production preview JavaScript with deferred backend responses and manual geometry selection. This test is skipped on Python-only hosts without Node.
- `tests/test_uigs_visual_capture.py`: responsive capture contracts.
- `.uigs/ui-visual-capture.json`: existing desktop layout capture plus tablet (1024×900) and phone (390×844) viewport captures; these are *declarations*, not already registered Foundry visual evidence.

Visual capture with the production UI is `production-rendered`; it cannot prove the FFmpeg/libass backend path unless an explicit runtime-backed capture is separately established.

### Manual acceptance walkthrough

1. Open a project, go to **排版**, confirm long text and the safe area in browser geometry mode.
2. Request **实际 ASS 渲染** and verify the image origin label changes only when the response arrives.
3. Change fontsize while the render is still running. The old request must not restore an outdated image.
4. Request a new actual render, then select **几何预览** before it finishes. The explicit selection must remain in effect.
5. Change several controls during a Save Settings request. A later unsent change must still show as unsaved.
6. At 390px, 1024px and a wide desktop viewport, verify the 16:9 stage stays proportionate and all inspector actions are reachable without horizontal scrolling.
7. Generate ASS from saved settings and inspect the actual output independently of the workbench preview.

## Explicit non-goals / remaining evidence

No subtitle layout algorithm, ASS export schema, source text provenance, word-alignment rule, renderer implementation or archive pipeline is modified here. This PR does not claim CI has completed, screenshots have been registered, or physical-device acceptance has occurred. These require separate evidence after submission.


## Follow-up: sample ownership, decoded image evidence and project-safe saves (2026-10-10)

The second work unit builds on the same UI and PR; it does not create another presentation mode.

**Sample ownership.** The editable Chinese and source-text fields have an explicit *example / project sample / manual trial* provenance. Manual input invalidates borrowed project word-timing references. Project entry and asynchronously completed sample searches do not replace manual trial text. A deliberate “重新载入项目样本” action replaces it with a selected corpus stress sample. Sample selection is bound to request generation, project root and manual-text revision.

**Rendered-image evidence.** A successful HTTP/PNG response is not yet a displayed image. The actual-render mode is published only after the image has decoded (using `img.decode()`, with load/error fallback). Failure keeps the real-render image hidden and returns an explicit error rather than claiming FFmpeg/libass pixels. Stale requests are guarded by project-root/payload identity and client-side `AbortController`. Aborting the client request does *not* prove the server job has been cancelled.

**Save transaction.** Multiple save clicks are serialized at the UI boundary; the Save and Save & Generate actions are disabled while the prior settings POST is pending. The exact submitted payload remains the saved identity. The server and the lightweight Termux backend also accept an optional `expected_project_root` and reject an attempted save if the active project identity has changed. Older API callers not providing the field continue to work.

**Regression coverage added.** `tests/test_subtitle_style_sample_ownership.py` drives production JS with deferred sample/save responses; `tests/test_subtitle_style_preview_races.py` checks actual decoded-image readiness/failure; `tests/test_subtitle_preview.py` covers both the accepted and mismatched project-root fence; existing UI contract tests assert the added affordances.

**Verification boundary.** JavaScript syntax compilation and focused source-level V8 simulations have passed in this development session. The updated PR's complete GitHub Actions result, actual tablet/phone screenshots and runtime-backed FFmpeg/libass/device acceptance are separate and not yet claimed for this commit. No new canonical UI requirements are inferred.
