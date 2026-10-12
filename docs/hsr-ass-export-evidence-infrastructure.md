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
