# HSR ASS evidence: infrastructure completion boundary

Status: Draft PR #134, still stacked on #133 and #132.

## Workflow

1. Human text/timing/style edits are explicitly saved. The existing export path produces `HSR_Voice_Archive.ass` and `subtitle_export_receipt.json`; `read_subtitle_export_health` must report `current` and `ass_required=true`.
2. On explicit **验证已导出 ASS（抽样）**, server verifies the current receipt, file size, and output SHA; creates a snapshot of the actual exported ASS bytes (not an unsaved preview), reads real `Dialogue` time ranges and chooses up to eight distributed, bounded subtitle windows from the export. The stored report counts all dialogue events and unexamined unique time windows.
3. A project-scoped, persistent `app.jobs` job renders each selected time on black and white FFmpeg/libass backgrounds, computes the visible-ink bounding box and compares the same horizontal/vertical SafeArea. A progress/cancel control polls the existing `/api/jobs/{id}` contract.
4. Before publishing, the server reloads current project settings and verifies that the original ASS bytes, export receipt SHA, policy fingerprint and output path are *still identical*. It writes a separate atomic `ass_render_evidence.json` receipt. `GET /api/subtitle-layout/exported-evidence` rechecks these identities after app restart, and returns `stale` when any changes.
5. Failures, cancellation and FFmpeg timeouts do not publish a new proof. Only the ASS evidence job is allowed to use the extended cooperative job cancellation API. Other build/update task types cannot be canceled by this endpoint.

## Scope, gaps, and conservative wording

The report always says `full_interval_verified=false` and `scope=bounded_exported_ass_samples`. A hash-valid report is *not* a certificate of visual correctness. There may be many unexamined subtitle windows, and each sampled event is only checked at its midpoint. Single-frame evidence retains RGB threshold 16 and does not reconstruct completely clipped/off-frame ink. Source fonts and output device may differ from the CI runner. Exact output hash identity does not imply that old ASS was automatically regenerated when style settings changed; the style hash is a policy check at inspection time.

The verification is bounded to at most 12 sampled windows and 64 MiB ASS text input. The full playback timeline, source WAV identity, on-device font parity and video-container identity are **not** certified. The old geometry-only preview remains available for editing; neither preview nor the export evidence result is automatically treated as a successful final export.

## Tests

`tests/test_export_ass_infrastructure.py`: real ASS event planning; invalid/oversized inputs; hash-bound proof; changed style/bytes invalidation; cancel-before-publication; job ownership; FastAPI contract; UI wiring. Existing Chromium and libass CI should remain green. No Canonical UIGS update on a single repo change.


## Render policy provenance gap closed (2026-10-12)

A stale exported ASS may have a `current` text/clock receipt while its style settings have changed. The receipt now includes `render_policy_sha256`, computed **at the time the ASS writer actually runs** from the effective `SubtitleRenderConfig` dataclass and the exported source/target languages. Exported ASS inspection rejects absent (legacy) or mismatching fingerprints and instructs a fresh ASS export; it cannot retroactively attest old style inputs. Ordinary historical SRT/ASS export-health semantics remain unchanged. This is a fail-closed upgrade of the *render evidence* contract, not a claim that every old export must be rebuilt to remain readable.

Regression checks: current writer-derived receipt fingerprint matches current project; a changed style without a new render is rejected; an old receipt lacking the fingerprint is rejected for raster evidence; identical source time/text but changed style cannot obtain false current render evidence.


## Dynamic events in the **actual exported** ASS (2026-10-12)

The exported-ASS evidence planner previously chose only the *midpoint* of up to eight distinct Dialogue time windows. This systematically missed non-midpoint displacement (\move), Fade (\fad / \fade), transforms (\t), and centisecond Karaoke (\k / \kf / \ko) transitions even when the exact final ASS was hashed.

\`app/ass_export_sampling.py\` now parses the exported \`[Events]\` and its declared \`Format\` (including reordered columns, with \`Text\` last), deduplicates identical time windows, and classifies dynamic ASS override tags inside braces only. It selects a bounded mix of globally distributed Dialogue window midpoints, distinct dynamically affected windows, and representative motion/fade/transform/Karaoke event candidates. It does not render or inspect every effect transition; per-window tag candidates have an explicit cap. The total FFmpeg rendering budget remains at most 12, default 8.

Evidence includes \`dynamic_window_count\`, \`targeted_dynamic_window_count\`, \`unexamined_dynamic_windows\`, \`effect_candidates_truncated_windows\`, \`candidate_count\`, \`omitted_candidate_count\`, \`candidate_count_scope=window_midpoints_plus_targeted_effect_candidates\`, and \`max_sample_gap_seconds\`. Candidate counts explicitly omit expansion of unselected dynamic windows. Each real sampled frame reports \`active_dialogue_count\` and \`compositing_scope=all_active_ass_dialogues\`: a bounding box is for the **composited libass frame**, not exclusive attribution to the targeted Dialogue.

The \`full_interval_verified=false\` contract is unchanged. Unrecognized or malformed effect variants can still cause missed transitions; the planner is a deterministic heuristic, not formal temporal extrema analysis or a certified exhaustive render. A final MKV self-contained-font playback proof also remains separate.


## Evidence schema migration

The previous midpoint-only verifier used evidence schema v1. Risk-aware exported animation sampling changes the meaning of coverage and the evidence metadata, so new reports use `schema_version=2` and `sampling_algorithm=exported-ass-events-v2`. Readers explicitly reject earlier v1 sidecars as stale rather than silently displaying old midpoint-only results as though animation events had been sampled. Re-run the opt-in exported-ASS inspection to migrate the evidence; the existing ASS/SRT exports themselves are unchanged.

 
## Parser ownership, abort propagation, and coverage precision (2026-10-12)

The parser's first pass preserves time-window identities, count and tag classification but **not** full per-Dialogue source text. A second pass collects exact override text only for selected dynamic time windows; both passes share the same declared Events Format grammar. The underlying corpus string remains in memory during planning, and all time windows still have compact metadata. The overlap counter uses sorted start/end arrays with half-open interval rules, rather than rechecking every raw Dialogue for each sampled frame.

Cancellation and the global 120-second job deadline are now checked within long-corpus parsing at least once per 256 input lines in both passes. This is cooperative cancellation for parsing; expensive operations within one single line and Python sorting cannot be interrupted at every instruction.

The evidence also distinguishes a dynamic window that happened to be selected at a midpoint (`targeted_dynamic_window_count`) from a dynamic window for which an actual effect candidate timestamp was sampled (`dynamic_effect_sampled_window_count`). `effect_sample_count` and `unexamined_dynamic_effect_windows` expose remaining risk; neither proves the full effect range is safe.

 
## Large synthetic archive: bounded planner performance evidence (2026-10-12)

A repeatable 12,000-window / 24,000-Dialogue **synthetic** corpus mixes duplicate time windows, dual-language layers, motion, fades, and Karaoke. `tests/test_ass_export_sampling_scale.py` measures plan-only elapsed seconds and Python `tracemalloc` allocation peak and writes a machine-readable JSON artifact during the CI libass job. Explicitly validates that skipped dynamic windows remain reported. Synthetic planner timing is environment-specific; it measures neither FFmpeg rendering cost nor real game voice corpus acceptance, and is not a throughput guarantee.

The independently exercised real FFmpeg/libass move-from-outside-SafeArea fixture remains in `tests/test_export_ass_infrastructure.py`. Device memory, real large-corpus rendering latency and end-to-end mux/video verification remain pending field work. `full_interval_verified=false` remains unconditional.


## Resumable and cancellable window-pass coverage (2026-10-12)

The earlier globally distributed eight-frame smoke check remains available and **separate**. A new explicit \`继续覆盖检查（下一批）\` command advances a cursor over **unique ASS [Events] Dialogue time windows**, ordered by their actual start/end times. Each batch renders at most 8 dual-background FFmpeg/libass timepoints by default (12 maximum server-side): one midpoint per static time window; a midpoint and one selected override-event candidate (\move, Karaoke, fade, etc.) per dynamic window where distinguishable. The next batch resumes at the next unvisited window. Repeating the command eventually visits every time **window**, not every continuous instant or every effect state.

The new \`ass_render_coverage_checkpoint.json\` persists cumulative window/frame counts, dynamic-event coverage, observed violations, missing ink, total execution time, run identity, and the checksum of the most recently committed batch receipt. Each independently named run stores bounded per-batch records in \`ass_render_coverage_batches/<run_id>-batch-NNNNNN.json\` with exact timestamps and detected bboxes. New runs do **not** overwrite old receipts, including if a restart is canceled. Receipt and checkpoint are written atomically in order; a crash/cancel between their writes can leave a harmless **orphan receipt** not reflected by the old cursor. The status API checks latest receipt checksum and ASS/export-receipt/saved-style identities before accepting any progress.

On cancellation/failure **during rendering**, the previous checkpoint remains unchanged. Normal application restart returns the last committed cursor even if an incomplete job was marked \`interrupted\` by \`app.jobs\`. The existing project-scoped allowlisted task and cancel endpoint are reused; both FastAPI and Termux Lite expose new GET status and POST batch-start routes. A stale ledger (changed ASS, receipt or saved style) never resumes silently; the user must explicitly restart, normally after regenerating the ASS with the updated style. A completed **window pass** only means sampled instants in every unique time window were visited. \`full_interval_verified=false\` is always explicit.

Limits: one batch is capped at 120 seconds, with per-frame process timeout and cooperative cancellation; large projects may need many manually initiated batches, and the planner reparses the source each time. This is not an unattended task queue, a proof of complete temporal animation safety, verification of the muxed video, or validation of device fonts. Older aggregate eight-frame \`ass_render_evidence.json\` v2 reports retain their own interpretation; neither status is silently upgraded.
