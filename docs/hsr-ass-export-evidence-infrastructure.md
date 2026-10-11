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
