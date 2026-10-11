# HSR Archive Corpus Audit — read-only chapter/source overview

Date: 2026-10-11

## User workflow

The existing **Archive / Build Records（档案与构建记录）** area contains
**检查章节与来源覆盖**. It reads the active project's persisted
`manifest.json` and displays group/chapter coverage without adding a
separate workspace, changing source files, or invoking a build.

Both web backends expose the same read-only route:

`GET /api/project/{project_id}/archive/audit`

The response includes:

- groups in manifest order, with entry count and source/target text coverage;
- the number of manifest source WAV SHA-256 values that are syntactically valid;
- how many distinct WAV basename strings repeat (normal for parallel character trees);
- bounded issue records: duplicate entry ID, duplicate source member, missing
  member identity, missing source/target text, invalid hash, invalid source
  time, and nonmonotonic start order;
- optional artifact *presence* flags, not output integrity certifications.

## Invariants

A repeated basename is not a conflict when `source_member_id` distinguishes
the files. Duplicate member identities or indexes are flagged.

A missing target text is **coverage** information and may be intentional;
the canonical manifest's `target_text` is reported without borrowing human
`final_chs` overrides. Source audio positions are not adjusted.

The audit is intentionally cheap and **manifest-structural-only**. It does
not reread actual WAV bytes to recompute SHA-256, verify the assembled FLAC's
decoded PCM, inspect source packages, or assert that an optional ASS has been
visually approved. Presence of a well-formed hash is not evidence of byte
integrity. Failures do not rewrite files.

## Existing Karaoke improvements in this increment

- The ASS `k` / `kf` compiler preserves real word onsets with `kt`.
  Initial silence and pauses are not compressed to consecutive voiced durations.
- The derived alignment sidecar records the manifest original-WAV SHA-256,
  and an audio revision invalidates the cached alignment even if the text
  and duration are unchanged.
- Existing word timing controls and ASS preview are reused. There is no new
  Karaoke tab or extra subtitle-layout workbench surface.

Remaining possibilities (not part of this delivery): multilingual forced-alignment evaluation, optional per-member pre-normalization provenance, and full live-project acceptance.


## Optional physical original-WAV checksum verification

The same existing archive-record panel now offers **逐个核验原始 WAV 字节**.
Both backend variants implement:

`POST /api/project/{project_id}/archive/verify-sources`

This starts an `archive-verify` progress job; it does not hold the UI thread
while scanning a character package. It resolves the original configured source
tree once (a ZIP/7z may need the existing `.state/reference-preview` extraction
cache), uses full package-relative `source_member_id` first, and permits a
basename fallback only when it is unambiguous. It streams WAV bytes through
SHA-256, checks per-file stat identity before/after hashing, and compares the
observed SHA-256 against `manifest.json`. Package archive stat identity is
also checked around the verification run.

Results distinguish:

- `verified`: source WAV bytes matched the Manifest SHA-256;
- `mismatch`: the source exists but its content hash differs, with no recorded PCM normalization in this build;
- `normalization_ambiguous`: a source hash differs in a build that normalized some WAVs; without per-member pre-normalization hashes this does not prove corruption and is never treated as trusted;
- `missing`: exact member or unique-basename WAV not found;
- `ambiguous`: multiple same-basename sources with no explicit member ID;
- `unverifiable`: bad/missing expected hash, unsafe symlink path, or I/O failure;
- `changed_during_verify`: WAV size/mtime changed while being streamed;
- `source_package_changed`: ZIP/7z stat changed during the run, so all prior
  partial results are *not* a trustworthy all-source certification.

A normalized build's aggregate conversion count cannot identify the specific members rewritten. In such a build, mismatches are classified as inconclusive rather than incorrectly called corrupted. This result does not establish original-to-normalized byte equivalence.

A `trusted_complete` result only means all referenced **original WAV member
bytes** were available and matched the manifest at this run. It does not prove
the archive's assembled FLAC has the same decoded PCM, nor that derived ASS/SRT,
AI translation or human proofreading is correct.

A byte audit is deliberately **opt-in**, not an automatic preliminary step
on every panel render. A user switching to another project while the task runs
must not see its result attached to the new project's archive view. Full
actual-device and large-archive acceptance remains independent of CI.

## Remote incremental voice updates: selective adoption and identity fence

The existing Update review list now allows each **new** remote voice to be
selected or deselected, including filtered-by-chapter/character/text bulk
selection. Existing-source changes, ambiguous identity and variant candidates
remain review-only; they are not silently promoted into the download list.
The existing **Apply remote updates** API accepts an optional JSON array
`selected_filenames` (form field). An omitted field retains backward-compatible
all-new selection, while `[]` is explicitly rejected (never treated as all).

Before starting any network work, the backend confirms all selected names
belong to the current `new_logical` plan and are unambiguous WAV basenames,
that the plan is a **remote** index comparison, and that its embedded
`manifest_sha256` still matches the current archive. Old update plans
without that digest require a fresh check. The server also refuses an
incremental addition whose basename matches any existing member in the
manifest or current project index (case-insensitive). This conservative guard
is necessary because the current remote audio provider resolves filenames,
not full package-relative source-member identities.

At commit time the original extracted WAV members preserve their source
subdirectories on **both** FastAPI and Termux Lite. The system checks actual
source WAV basenames before copying and aborts rather than overwriting a
different original member. Only the files confirmed successful **in this
application run** enter the combined directory; stale WAV files left in
`.generated/incremental_audio` from earlier jobs cannot sneak into a
user's restricted selection. On success, the existing explicit rebuild
boundary still applies. Human subtitle overrides remain independent output
sidecars; this update does not rewrite them or auto-accept changed source
lines. This is not yet a cross-process transactional update mechanism.

### Evidence

- `app/remote_update_guard.py`, `app/remote_index.py`
- `app/server.py`, `app/lite_server.py`
- Existing `app/static/index.html` Update list
- `tests/test_remote_update_guard.py`

The remote audio dataset is still filename-keyed. Distinct same-basename
voices cannot safely be imported through this incremental path until
provider records supply disambiguating member IDs. Those can continue to
be built from a local voice package with its original path hierarchy.


## Human Chinese correction retention across official-source revisions

The official EN/JA/KO source transcript remains **read-only**. Human editors
can change only the derived Chinese `final_chs` subtitle text. New
`subtitles_overrides.json` records retain a normalized SHA-256 of the
corresponding official source transcript (`source_text_sha256`).

When the original WAV member is still identifiable but its *official source
text* changes during a later build, the recorded human correction is **not
silently applied** to the newly worded sentence. The override remains
byte-for-byte in the separate project sidecar for manual comparison. The
current subtitle and derived ASS/SRT use the new base translation until a
human explicitly reviews and saves a new correction. A prior review
confirmation is not displayed as valid for stale source text.

The existing proofreading workspace shows a source-revision warning and the
previous human Chinese sentence as **read-only comparison material**. It does
not offer an official-source editor, does not apply Karaoke to Chinese, and
does not auto-reaccept prior human text. New saves bind the new source-text
fingerprint; unless the operator explicitly reconfirms review, stale prior
confirmation is cleared. Legacy overrides that lack a fingerprint remain
supported to avoid dropping earlier user data; without provenance they
cannot promise the same source-revision safeguard.

Stale translation diagnostics and the previous human correction are
**query/UI-only derived evidence**. They must never be serialized as new
canonical Manifest fields. Tests cover a saved human correction, changed
official source, preserved sidecar, corrected derived exports, manual
re-review, harmless whitespace, and legacy fallback.

The corrected bilingual CSV also resolves rows by unambiguous
`source_member_id`; historic rows lacking member identity can use a unique
filename or (if the manifest has no filename) a unique index, but never a
duplicate WAV basename or an explicitly stale member fallback. The original
source transcript is never mutated by this maintenance operation.
