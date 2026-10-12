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

## Bug-fix audit: exported evidence integrity and UI job mutual exclusion (2026-10-12)

**Historical batch integrity.** Previously the coverage reader only verified the last batch receipt. Corrupting a first or middle receipt could still return `window_pass_complete` with `verified=true`. The reader now traverses the SHA-256 receipt chain through **every** committed batch, checks run identity and contiguous cursors from genesis, recomputes per-sample missing-ink and violation totals plus cumulative counters, and rejects aggregate checkpoint mismatches. This is an intentional O(number of batches) status/resume audit; long-archive performance needs separate measurement. It does not authenticate evidence against an attacker who can rewrite the entire chain and ledger.

**Dynamic-event accounting.** Generic animated-entry and animated-exit fallback probes are no longer counted as an actual decoded effect transition. Unsupported `\kt` timestamps remain explicitly truncated/unsupported and report zero effect-timestamp coverage even when the parent Dialogue midpoint was sampled.

**Concurrent workbench actions.** Global exported ASS smoke verification and the resumable batch coverage verification share the same project-scoped job kind. Both UI start handlers now claim a synchronous in-flight guard before awaiting network responses; the other control is disabled until the job finishes. Server-side project-scoped exclusion still enforces authority.

Regression tests cover modified earlier receipt, missing intermediate receipt, falsified previous hash despite recomputed last-receipt hash, forged checkpoint counters, unsupported Karaoke timing, and the existing real-FFmpeg smoke render. No whole-timeline or per-segment effect editing claim follows.

 
## Persistent batch-history memory and restart durability (2026-10-12)

The persistent `app.jobs` journal was previously capped at 100 jobs, but the in-memory `_JOBS` registry was **unbounded**. A long archive requiring thousands of sequential eight-frame batches would accumulate terminal jobs and slow duplicate-task scans despite writing a small journal. `_prune_finished_locked()` now bounds in-memory history to the same budget after every journal write, first dropping ordinary completed jobs, then retaining `interrupted` and `awaiting_input` records ahead of older routine completions. Queued/running jobs are never pruned. If active jobs alone exceed 100, all are kept in the journal, even temporarily exceeding the normal history limit. Restart recovery reads every previously running/queued record plus the most recent history and preserves its interrupted state.

`tests/test_jobs_history_pruning.py` verifies trimming old successes, retaining queued/running jobs, a real >100-job sequential lifecycle, preserving >100 concurrent active records on disk, and retaining an older interrupted task across restart. As before, ordinary terminal job history is a bounded cache; old finished jobs can disappear from the UI without affecting ASS coverage receipts, which are separately durable. This does not benchmark thousands of actual FFmpeg renders or certify continuous playback frames.


## Late cancellation after committed evidence: status correctness

A cancellation request can arrive after an ASS evidence job has durably written its proof/checkpoint but before the background runner records its terminal state. Earlier `app.jobs` behavior discarded a successfully returned result when `cancel_event` was set, incorrectly reporting `canceled` even though the persisted artifact/cursor had advanced. A **successful return by the writer** now determines `succeeded`, retaining `cancel_requested=true` with an explicit explanation if the user requested cancellation too late. Before-commit cooperative cancellation still terminates without a new proof.

`tests/test_jobs_cancel_commit_boundary.py` forces the precise ordering with threading barriers: writes a durable marker, pauses before returning, sends cancel, releases the successful result, and asserts `succeeded` plus saved marker. A separate case cancels before persistence and requires `canceled` and no marker. Both tests have dedicated execution in the real FFmpeg/libass CI workflow, alongside the job-history resource tests. This contract does not make all filesystem writes atomic against every possible process crash.


## Long-chain audit race and I/O hardening (2026-10-12)

The coverage status reader previously loaded each historical batch receipt *twice*: first for SHA-256, then again to decode JSON. A concurrent receipt rewrite between those reads could permit the hash of one body to be checked while the contents of another were used to calculate verified coverage, and the redundant reads amplified full-history audit cost. The audit now reads each bounded (256 KiB maximum) receipt's raw bytes **once**, hashes and parses precisely those bytes, and never treats a mismatch or overlong record as current evidence.

Also, verifying exported ASS identity only *before* a potentially lengthy historical chain traversal allowed changes to the ASS or the coverage checkpoint **during** the traversal to go unnoticed in that response. The reader now hashes its initial bounded checkpoint snapshot, then after auditing revalidates both the checkpoint bytes and the exported ASS / writer receipt / saved style identity. If either has changed, the response is `state=stale`, `verified=false` (no historical coverage claim). A transient alteration followed by identical byte restoration cannot be distinguished from unchanged bytes without filesystem transactions; cross-process concurrent commit locking remains a separate engineering task.

New regression tests inject ASS replacement and checkpoint update at the exact post-audit boundary, count exactly one raw read per historical batch, and reject an oversized receipt. This reduces double I/O but does **not** change the O(number_of_batches) work required to validate an uncached historical chain or solve the across-many-batches O(n^2) aggregate progression cost. No claim of real long-corpus throughput or complete timeline safety is made.


## Cross-process writer exclusion (2026-10-12)

The previous process-local `app.jobs` project exclusion and pre-commit optimistic cursor check did **not** serialize independent FastAPI/Termux Lite processes. Two service instances could read the same old checkpoint, render independently and race to commit a new batch. The second instance could overwrite the checkpoint and/or leave inconsistent receipt artifacts in the same run.

`app/ass_coverage_lock.py` adds a **stable, output-directory-scoped operating-system lock file** (`.ass-render-coverage-writer.lock`). `inspect_next_exported_ass_batch` holds it from before it reads the old cursor through the final receipt and checkpoint write. It uses nonblocking `fcntl.flock` on POSIX/Termux or `msvcrt.locking` on Windows; a concurrent writer receives an explicit error rather than racing or secretly overwriting work. The lock file must never be deleted while the application uses it: removal can create distinct lock inodes. OS locks are released when the process exits; there is no manually maintained PID "stale lock" guess. Symlink and file type checks fail closed; cancellation before lock acquisition does not mutate evidence.

`tests/test_ass_coverage_writer_lock.py` starts an **independent Python process** while a parent process holds the same directory lock, verifies that the second process cannot acquire it, and confirms acquisition succeeds after release. Additional tests cover same-process contention, cancellation before acquisition, symlink refusal, and blocked ASS batch progression with no persisted checkpoint changes.

This isolates cooperating **window-pass writers** for the same resolved output directory; it is not a global lock on subtitle export/mux and does not transact the ASS writer with the checkpoint verifier. Uncooperative external file modification and network filesystems with unreliable OS locking remain a separate limitation. The complete historical receipt-chain audit is deliberately kept intact. Its O(committed batches) per-status-read and O(n²) cumulative progression cost remain a known performance issue, not claimed solved by a file lock.


## Writer-only provisional cursor and one strict historical audit per batch (2026-10-12)

Full verification of an arbitrary set of **mutable historical receipt files** cannot generally be replaced with an O(1) checksum of the newest checkpoint: an old receipt could be corrupted without altering that checkpoint. The public `GET /api/subtitle-layout/exported-coverage` and the default `read_exported_ass_coverage()` therefore **continue validating the entire chain** before returning `verified=true`.

A single window batch previously ran a strict chain scan both **before** and **after** FFmpeg rendering. While holding the same output-scoped cross-process writer lock, the initial candidate-pointer read now uses a private `_defer_history_audit=True` path. That path validates exported ASS / writer receipt / saved style identity, the checkpoint structure, and the **latest batch receipt SHA-256**, but deliberately returns `verified=false` with `history_audit="deferred"` for existing checkpoints. It must never be shown as a fully verified result or published directly as evidence. After rendering, and **before writing** any new receipt or checkpoint, the writer runs the unchanged full historic-chain audit and fresh export identity check. A changed/missing earlier receipt or checkpoint therefore prevents publication, including mutations during the FFmpeg subprocess interval. A corrupt most-recent receipt fails fast before rendering.

This removes one O(history) scan per user-started batch while keeping the external status contract and final pre-commit integrity boundary intact. The optimization trades earlier failure detection for fewer full scans; a damaged *old* receipt may allow unneeded FFmpeg frames before the final validation rejects the attempted batch. UI status queries still perform their own strict audits, so repeated polling or many batches retain O(batch-count) per verified status read and potentially O(N²) aggregate work; the underlying asymptotic problem is **not** solved.

`tests/test_ass_coverage_batches.py` explicitly verifies the writer candidate is never certified, only one full historical scan occurs on resume, and old-receipt mutations before and during rendering cannot advance the checkpoint. `tests/test_ass_coverage_audit_scale.py` constructs a synthetic 256-batch receipt chain and records strict/provisional latency and `tracemalloc` peak in CI; it then damages the oldest receipt and asserts the strict audit fails closed. The benchmark performs **no FFmpeg rendering**, and cannot establish real-game or target-device performance.
 

### Additional cumulative-work guard (2026-10-12)

A deterministic regression now performs **16 successive committed two-window batches** using the repository's mock RGB renderer and asserts **15 strict historical-chain audits inside the writer**, exactly one for each batch after genesis. An injected FFmpeg error must cause **zero strict history audits and zero cursor advancement**; a modified latest receipt must be rejected by the bounded lightweight checksum check **before** any rendering. The public status read remains strictly audited. These are operation-count tests rather than machine-sensitive timing assertions, and they cover 32 synthetic windows, not real long-game subtitle processing.

This only removes redundant **pre-render** history traversals; rendering latency and the remaining across-batch O(N²) sum are unchanged in asymptotic complexity.

 
## Explicit restart: eliminate unnecessary auditing of discarded evidence

A discovered performance bug remained after the one-audit-per-resumed-batch optimization: `restart=true` deliberately starts a **new run_id** and inherits *zero* historical batch receipts, but the writer still called the strict historical auditor for the old run just before writing its first new checkpoint. With a huge old archive history, clicking **重新开始覆盖** could spend significant time scanning thousands of abandoned receipts even though their validity cannot affect the new run.

The explicit-restart branch now takes a bounded SHA-256 identity snapshot of the **old checkpoint only** before rendering and requires that same identity after rendering, before committing a fresh-run receipt and checkpoint. Old receipt chains are not audited because they are **not reused**. Checkpoint symlinks and oversized checkpoint input fail closed; an external uncooperative process changing the old checkpoint mid-render aborts the restart. Old receipt files remain untouched and independent; the new run has distinct receipt names and its own genesis chain. A normal `restart=false` continuation still performs a **complete strict historical audit** before publishing and the public `verified=true` API has not been weakened.

Regression tests: restart while old receipts are corrupted (new run succeeds but does not inherit them); externally altered old checkpoint during rendering (new run refuses to commit); symlinked checkpoint (restart refused). This is a **restart-path optimization**, not a claim that the O(number of existing batches) strict audit or O(n²) cumulative cost for thousands of ordinary continued batches has been solved.


## Strict receipt-chain audit: cooperative cancel and global deadline (2026-10-13)

After bounded FFmpeg/libass rendering, a normal continuation still validates **every historical batch receipt** before publishing the next receipt/checkpoint. Previously the strict loop did not check `cancel_event` or the already defined 120-second batch deadline. If an archive accumulated many batches, the application could keep hashing them after the user canceled or after the batch time budget was exhausted. It could also reach the commit stage without accounting for time consumed by the historical audit.

The internal strict auditor now checks cancellation and the monotonic deadline **before every historical receipt** and after the final receipt, and the writer enforces those controls before the scan and immediately before both receipt and checkpoint persistence. Interrupted scans raise `InterruptedError`, expired scans raise `TimeoutError`; they are **not** converted to an ordinary `stale` status. An uncommitted batch may leave no new receipt, or an orphan receipt if cancellation occurs between the two atomic writes, but it **never advances a checkpoint** when the scan itself was interrupted. The read-only public status endpoint has no newly imposed deadline and continues auditing the full chain before returning `verified=true`.

Regressions deliberately trigger cancellation or an expired monotonic deadline **after the strict reader consumes the historical receipt**, verify no next-batch receipt/checkpoint was committed, and ensure the ordinary public history audit still works. This preserves the earlier *one strict scan per successful append* optimization but does not remove the O(number_of_batches) audit per append, and does not establish full temporal or Android-device safety.
