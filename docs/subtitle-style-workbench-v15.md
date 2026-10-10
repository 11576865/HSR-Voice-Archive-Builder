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


## Subtitle studio expansion: corpus explorer, selective QA and history (2026-10-10)

The earlier v15 split-pane arrangement was a *style form with a renderer*. The new structural iteration changes the interaction workflow and adds functionality rather than treating another CSS skin as a redesign.

### New interactive tasks

- A dedicated subtitle explorer remains visible next to the primary preview on wide desktops, with searchable real project subtitles, item times, group/context labels, source text and revision/word-time indicators. Selecting a row loads that exact text and its optional verified word-timing into the existing preview.
- Previous/next navigation respects the active result set; larger corpora are paginated at 80 rendered rows per page so a project does not inject thousands of navigation buttons into the DOM.
- **High-pressure** filtering ranks text using the existing width heuristic only, and is clearly labeled as an estimate. It never reports those entries as actual collisions.
- **Geometry QA** runs the real, existing `/api/subtitle-layout/preview` solver against *the currently filtered project subtitle rows and current trial style parameters*. Three workers run concurrently and at most 60 items are checked per invocation. It distinguishes verified geometry overflow, solver-enforced downscaling, unaffected rows, and request errors. Results include exact coverage (`checked / filtered total`), an explicit stop control, and a "需处理" filter to navigate failures.
- A bounded 40-state client-session history supports Undo/Redo of global style control changes. It does not claim to undo subtitle text edits, source provenance, saved server settings, or ASS export operations.
- Mobile prioritizes the **preview stage**, then a collapsed-on-entry subtitle explorer, then the Inspector. Tablet/desktop retain multi-pane simultaneous context.

### Evidence and remaining boundaries

- CI for the previous iteration exposed a brittle signature-string assertion, rather than a renderer algorithm failure. The UI contract assertion has been updated to recognize the evolved loader function.
- New: `tests/test_subtitle_style_explorer.py` executes the production corpus/navigation/QA JavaScript with deterministic solver responses.
- This batch QA is **not yet a full-project one-click audit**: it caps each run at 60 filtered entries, and the operator can narrow the search or filter to inspect other groups. No uninspected entry is treated as verified.
- The preview is still a style/layout workbench rather than a full timeline editor. This change does **not** implement frame-stepping, audio waveform/spectrogram editing, subtitle split/merge, per-event style overrides, or render-backed video playback; none should appear as shipped features.
- Visual fixture captures are separately available for composition review but are **not** production screenshots or active application acceptance evidence. Production visual and runtime-backed checks remain pending at this branch checkpoint.

The new workflow should be validated in an actual project with mixed short/long subtitles and a real libass render; source-level checks alone cannot establish legibility, keyboard usability or the practical speed of the authoring loop.


## Cue editor and original WAV waveform (continuation: 2026-10-10)

This increment creates a genuinely **persisting per-item subtitle text editor** in the stage column; it does not misrepresent project timing or fake a complete video NLE.

### User tasks now implemented

1. Select a real project subtitle in the corpus explorer. The cue editor presents its source text, current effective Chinese translation, immutable source start/end/duration metadata, and a **segment-relative 5–95% preview seek control** linked to the existing ASS-render timestamp.
2. Edit the final Chinese text independently of the global style form. Edits update the geometry-preview draft without automatically writing project files. Drafts are kept per cue during navigation and surfaced as *未保存正文* in the explorer. Switching back restores that specific draft. Typing in the old quick-preview fields explicitly detaches the project cue, so scratch trial text cannot accidentally become a persisted edit.
3. Click **保存当前字幕** or press Ctrl/Command+Enter while in the cue editor. The existing `POST /api/project/{project_id}/subtitles` persists a single `final_chs` override and refreshes SRT/existing ASS through the existing subtitle regeneration pipeline. Successful responses are checked for `updated_count == 1`; an in-flight save captures its exact submitted snapshot and never erases newer typing. Failure leaves the unsaved draft intact. Style-settings persistence is a separate transaction.
4. Click **加载音频** to fetch the actual selected subtitle's original WAV using the existing authenticated audio endpoint. Playback is native HTML audio. A browser-decoded waveform is rendered on a canvas and allows click-to-seek and 0.5 s keyboard seeking. If waveform decode is not supported (or the WAV exceeds the 12 MiB visualization limit), native playback remains usable; decoded peaks are never synthesized from dummy data.
5. Refresh project subtitles explicitly from the corpus rail. Unsaved session drafts remain owned by their respective cue; manual quick-preview text is not overwritten.
6. On unload, the browser is asked to warn if unsaved cue text exists. This browser-standard warning is advisory and not a durable autosave.

### Safety and explicit boundaries

- **No fake timing writes:** source start/end values are **read-only**. The preview-position scrubber and audio seek are audition controls; neither changes original audio, source clip boundaries, archive manifest timecodes or ASS cue timing. Independent per-cue retiming, split/merge, video frame transport and full audio/video timeline editing remain unimplemented.
- **No fake project save:** unbuilt projects may return preview/sample subtitles from the existing endpoint. Both FastAPI and Termux-lite now return `persistable` alongside the subtitle list. The cue Save action is disabled unless the backend confirms that an actual output `manifest.json` exists.
- **Project identity:** each cue save includes optional `expected_project_root`. FastAPI and Termux-lite reject mismatching active-project contexts before touching subtitle files. Older callers without this field remain supported.
- **Word alignment:** editing Chinese text removes the old selected sample's word-alignment reference from current renderer payload. No automatic claim of realigned karaoke is made. A subsequent alignment refresh may be required.
- **Session history:** global style Undo/Redo does not also undo persisted cue translations. Cue-text drafts remain session-local and are not a durable local cache; hard reload loses uncommitted changes.
- **WAV vs media:** the native player/waveform displays the **original isolated WAV**, not the final continuous program mix or video asset. No final-media synchronization or frame-level waveform editing is claimed.
- **CI/evidence:** the per-cue JS/endpoint tests run under the GitHub Actions pipeline; complete CI must be checked for the final exact head. No production browser screenshots or human usability/device acceptance are claimed by the change itself.

### Coverage

- `tests/test_subtitle_cue_editor.py`: production JavaScript with deferred cue save, cross-item drafts, request single-flight, save-during-edit correctness and read-only scrub position.
- `tests/test_subtitle_preview.py`: reject cross-project text-write requests, accept matching project root and identify unbuilt preview-only corpus results.
- `tests/test_subtitle_style_workbench_ui.py`: key UI controls and separation of scratch versus persistable cue editing.

The cue editor is intentionally integrated **under the real preview stage**, not added as a separate unrelated page or as decoration on the global Inspector.


## Output-only retiming: first real persistent time-edit operation (2026-10-10)

This iteration extends the cue editor with **non-destructive subtitle DISPLAY timing**. Unlike the earlier version's audition-only percentage scrubber, the new numerical start/end editor and ±0.1-second nudge buttons persist real changes used by the SRT and ASS writers.

### Data ownership and contract

- **Source truth:** `manifest.json` owns continuous audio positions (`start_seconds`, `audio_end_seconds`, `display_end_seconds` from the archive build). The retiming UI does **not** move original WAV samples or rewrite those source timecodes.
- **Derived subtitle layer:** `output/subtitle_timing_overrides.json` (schema v1) stores per-event display boundaries plus the source-time/member identity they were authored against. No timing override is written into original archive metadata.
- **Input validation:** each boundary must be a finite number, start must be nonnegative, end must be at least 100 ms later than start, and each boundary can differ from its source time by at most 5 seconds. The limits avoid accidentally placing subtitles far outside the owning audio event; they are not a complete NLE.
- **Optimistic concurrency:** POST requires the expected currently effective display start and end, as well as optional `expected_project_root`. Stale edit requests fail instead of silently overwriting newer edits. Both FastAPI and Termux-lite provide the same endpoint at `POST /api/project/{project_id}/subtitles/{item_id}/timing`.
- **Conflict recovery:** if a rebuild changes the original source member/time window, the old derived timing cannot be applied to export. Corpus GET returns the source times plus a `timing_conflict` flag instead of hiding the entire explorer. An explicit **恢复原始时间** action can remove this stale override, using the updated source clock as its optimistic concurrency reference.
- **Render/export:** `refresh_subtitle_artifacts_from_settings` applies the timing override only when constructing subtitle adapters, after source manifest/csv persistence and before SRT and optional ASS rendering. The normal archive pipeline invokes this refresh after building and resuming, so output-only retiming is reapplied to future subtitle export. It does not alter FLAC samples.
- **Karaoke safety:** modifying a cue's display boundaries disables any prior word-level timing evidence for that particular subtitle event; it cannot be represented as having valid old word timestamps. Full realignment is a separate operation.
- **Frontend:** start/end seconds, 100 ms nudges, separate Save/Discard Draft/Restore Source actions, per-item draft storage, invalid-range explanations, in-flight single-flight fences with text saving, and error/unsaved state. The existing audition scrubber remains separate and does not itself persist time changes.

### Tests and status boundaries

- `tests/test_subtitle_timing_overrides.py`: actual filesystem overlay, manifest immutability, real SRT timecodes and mocked ASS writer adapter times, invalid/nonfinite intervals, optimistic concurrency rejection, old-audio-source conflict and explicit recovery.
- `tests/test_subtitle_timing_editor.py`: isolated Node VM executes production browser JS for cue switching/draft state, conflict-checked save, post-submit edits and reset.
- `tests/test_subtitle_preview.py`: FastAPI expected-project-root guard and retiming-to-regeneration dispatch.
- `tests/test_subtitle_style_workbench_ui.py`: new edit control IDs and state contract.
- No claims of actual libass/FFmpeg visual correctness, live user-experience acceptance or physical-device validation follow from these tests alone. The final PR head requires its own CI result.

**Important remaining limits:** There is no frame-accurate synchronized video transport, multi-track event split/merge, drag-to-retime on a full-program waveform, or arbitrary overlap/collision orchestration. Retiming is subtitle-only, per-event, millisecond precision, and bounded to ±5 seconds from the original source boundaries.
