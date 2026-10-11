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


## Reliability repair: source-bound human history and revision-safe remote publication

**Source authority and immutability are separate.** The editor never changes
the source-language transcript. An imported script can originate in a package
LAB file or in a third-party index; its read-only state does **not** certify
that it is an authenticated official game transcript. All Karaoke still
belongs to source-language Primary; Chinese translations remain plain text.

### Chinese proofreading revision control

The live `subtitles_overrides.json` is a current-state cache, not a revision
history. On replacing a human translation, resetting a subtitle to its source
translation, or reusing a numbered slot for a different WAV, the previous
record is now **durably appended first** to
`subtitles_override_history.jsonl` with its original source identity,
provenance and old Chinese text. Failure to append blocks the change. The
journal is retained outside the canonical Manifest; it does not expose an
English editing path.

New source-text fingerprints normalize **whitespace boundaries**, rather
than removing all whitespace; `now here` differs from `nowhere`. Both the
proofreader autosave and Layout cue editor include the source fingerprint
observed at load. The backend checks source/member/text baseline before
writing; stale editor drafts are rejected instead of silently claiming that
a former Chinese translation belongs to revised source text. Existing
confirmations are stored with the full original WAV member and source-text
fingerprint; bare legacy boolean confirmations stay on disk, but cannot be
displayed as verified for an unproven reindexed entry.

When a **fingerprinted** human Chinese override no longer matches the current
source transcript, the old text stays available for comparison and is
excluded from the new derived subtitles. ASS/SRT export is **blocked as
failed** until the operator explicitly reviews/saves it; an old output
cannot be newly certified under the wrong source. Old source-member-only
overrides without a saved transcript fingerprint remain a compatibility
limitation: the previous source revision cannot be proven from absent
provenance. A review/migration workflow is still required for strong
historical guarantees. Do not silently assert those records are fully safe.

### Remote updates and recovery

Both FastAPI and Termux Lite now prevent simultaneous background build and
remote Apply jobs for the *same project within this process*. An update
candidate selection begins **empty**, and the backend revalidates the
current Manifest and selected identities immediately before the Apply
publish step after downloading.

Each successful Apply writes a new immutable
`.generated/combined_audio_versions/combined-<version>/` tree and
`.generated/quick_index_incremental_<version>.csv` index. Previous
combined audio and indexes are no longer removed or overwritten. The
project configuration changes only after the new source and CSV exist, so
an interrupted publication retains the old source files. Older versions
consume disk space until deliberately archived/pruned; the code does not
blindly reclaim them. This is not a guarantee of an atomic cross-process
transaction covering OS termination, config and every dependent artifact.

Verified text recovery packages now include existing human subtitle override,
review state and append-only history files. If those files exist but no AI
translation checkpoint does, the automatic recovery process still creates a
validated text-only backup. Importing a human-only backup **quarantines
and preserves** its verified bytes without applying possibly stale
translations to another package. Manual source-aware restoration remains a
separate operation; importing a backup is not a claim that old subtitles were
already restored into the active Manifest.

### Evidence and known boundaries

- `tests/test_subtitle_source_revision.py`: new source revision,
  `now here` / `nowhere`, stale saves, identity-bound review,
  history preservation, reused ID and export fail-closed
- `tests/test_jobs_update_mutation.py`: same-project build/apply exclusion
- `tests/test_remote_update_guard.py`: versioned publication regression
- `tests/test_recovery.py`: manual-subtitle-only external backup/import
- `tests/test_subtitle_editor.py`: valid stable-member rebuild migration

These tests are synthetic unit/UI/fixture evidence. Real user archives, full
crash-injection, multi-process writers and physical Android devices remain
unverified. Do not merge into `main` solely on the basis of green CI.


## Human Chinese recovery: provenance-gated explicit selection

Backup import and **restoring selected human edits into a live archive** are now
separate operations. The existing Translation Recovery section includes
"预览人工中文字幕恢复" and "恢复选中的安全匹配项". The preview performs no
write, displays saved Chinese text as recovery evidence and starts with every
checkbox unchecked. Both FastAPI and Termux Lite implement the same
`/api/recovery/human/preview` and `/api/recovery/human/apply` contract.

Restoration is permitted only if:

- The recovery package passes its original size/hash/file integrity checks
  and contains `output/subtitles_overrides.json` (the backed-up **current**
  human CHS corrections).
- A saved **complete source_member_id** uniquely identifies one current WAV
  member and one unique subtitle entry ID; no sibling of the same basename
  is silently selected.
- The saved transcript evidence uses the **whitespace-preserving v2**
  fingerprint scheme and exactly matches the current read-only source text.
  Legacy v1/unknown hashes remain review-only because removing all word
  boundaries previously made distinct English expressions hash alike.
- If the backup contains an original audio hash, the current Manifest must
  contain the same hash; if the current Manifest has a hash but the backup
  does not, automatic restoration also refuses unproven identity.
- No current human correction occupies the target source member or
  current item ID; conflicts are never overwritten. The old CHS must differ
  from the current base Chinese to be useful.

The Apply request contains **explicitly selected source members**, expected
backup bytes hash, expected Manifest bytes hash and expected project root.
It re-parses both files **inside the existing subtitle edit lock** and rejects
a changed or ambiguous preview before calling the ordinary subtitle saver,
which checks the current source member, base Chinese and transcript hash
again. The original validated package is preserved under
`.state/recovery_imports/` before any edit. Restoration re-saves only the
Chinese `final_chs` value, **not** the official/source transcript, previous
confirmation receipt, source audio, Karaoke timings, or unrelated sidecars.
A repaired record is treated as an unconfirmed current edit until it has
been reviewed in the present project.

Importantly, `subtitles_override_history.jsonl` contains **past revisions**
that may have been superseded or explicitly reset. This version of the
recovery UI **does not automatically replay those historical revisions**:
they are preserved inside the backup for manual investigation. A human-only
backup imported through the old generic import endpoint is still
quarantined rather than automatically applied. Restoration is opt-in using
the new separate preview/apply endpoints.

Evidence: `app/subtitle_recovery.py`,
`tests/test_human_subtitle_recovery.py`,
`app/server.py`, `app/lite_server.py`,
and the existing Recovery panel in `app/static/index.html`.
The tests use synthetic member/Manifest fixtures and explicitly check
same-basename siblings, reindexed entries, legacy hashes, changed original
text or audio, missing current audio hashes, duplicated current IDs, an
existing human correction, altered Manifest between preview/Apply, and
selected-only FastAPI writes. They do **not** establish actual historical
official HSR data provenance, cross-process atomicity or a large real-game
archive restore. Legacy revision evidence remains a manual restoration
problem; the system must not claim full historic reconstruction.


## Remote incremental publish journal, interruption inspection and rollback

FastAPI and Termux Lite now share `app/remote_update_publish.py` for
incremental version publication. The current project configuration and index
are captured by SHA-256 at the beginning of an Apply job, together with the
manifest hash already bound to the remote comparison. After the downloaded WAVs
are combined into an immutable
`.generated/combined_audio_versions/combined-<uuid>/` tree and the new
`.generated/quick_index_incremental_<uuid>.csv` is written, publication
validates that:

- The original config, index, and Manifest still match the initial job
  boundary (not just the previous remote-plan Manifest SHA).
- Every explicitly selected added WAV has exactly one index record, its
  `source_member_id` equals its basename, and the staged audio file exists.
  **Every inherited row** must also resolve to a real member inside the
  combined tree, with a matching WAV basename, safe package-relative path,
  unique case-folded member identity and no symlink component. Legitimate
  chapter-specific duplicate basenames remain supported because their full
  member paths are distinct. A missing original WAV now blocks publication,
  even when all newly downloaded WAVs were valid. Termux Lite writes the same
  required `source_member_id` field as FastAPI.
- The new paths are precisely the generated paths for this revision ID.
  Pre-existing versioned resources and their indexes are never removed.

Before replacing project configuration, a revision journal is atomically
prepared at `.generated/incremental_revisions/<uuid>.json`, containing
the prior full project configuration, old and proposed config hashes, selected
names, and resource paths. The project config is then atomically replaced,
and the journal marked `committed`. **The journal is not considered proof that
a commit succeeded:** revision inspection always compares the live on-disk
project SHA against both the old and proposed SHA. Thus an interruption after
the config switch but before journal finalization is visible as
`applied_unfinalized`; an earlier interruption stays `not_applied`.

The existing Update panel now provides **增量更新版本与受控回退**. The read-only
`GET /api/update/revisions` shows revision status, and
`POST /api/update/rollback` queues a rollback with explicit revision ID and
the inspected live config SHA-256. The rollback uses the same in-process
per-project mutation exclusion as Build and Apply; it rechecks that the
revision is still the active project, the Manifest hasn't changed, and the
old audio/index files exist **with unchanged old and new index SHA-256**.
The saved old configuration must reproduce the prior on-disk SHA before
rollback is allowed; a tampered old configuration in the journal is rejected.
Indexes and rollback eligibility are rechecked after the preparation
journal write, immediately before replacing config. It atomically restores the exact old project
configuration while preserving both audio/index versions. A later build that
changes Manifest, an intervening config edit, a missing old source, or a
second attempted rollback is rejected.

The new `tests/test_remote_update_publish.py` fault-injects failure before
config replacement and after config replacement but before final journal
state; it verifies interrupted-state detection, safe manual rollback, wrong
project/config/manifest/index rejection, and FastAPI route behavior. It also
rejects incomplete or duplicated selected voice entries and mismatched
`source_member_id` values. Additional cases cover missing **original** WAV
members, duplicate or unsafe member paths, existing chapter-specific WAVs
with the same basename, symlinked files, changed old or new indexes, forged
rollback snapshots, and injected index edits between journal preparation and
config publication. `tests/test_jobs_update_mutation.py` covers
rollback competing with a build.

**Engineering boundary:** These tests are synthetic files and in-process
API/CI checks. The project still does *not* implement an ACID transaction
across OS processes or different machines, immutable enforcement against
manual file tampering, atomic commit of source/config/manifest/report as a
single unit, a full filesystem power-loss durability protocol or automated
old-version garbage collection. No real multi-chapter HSR package or
physical Android device was used for this acceptance. Do not delete
`.generated/incremental_revisions`, prior audio versions or prior indexes
during manual cleanup without separately verified recovery.


## Incremental source WAV attestation after staging (2026-10-12)

The versioned incremental publisher already verifies **every** row of the
candidate index against the staged source tree, including inherited chapter
WAVs with repeated basenames. Additional verification now attests the
**actual bytes** of every *selected new* WAV:

- Before writing the prepare journal, record SHA-256 for each selected WAV,
  indexed by its exact package-relative member identity, in
  `new_audio_sha256`. At the pre-config-swap boundary, recheck selected
  member paths (including symlink rejection) and their SHA-256; if even one
  changed after the original validation, **refuse to switch the project's
  configured audio directory or CSV**.
- Revision inspection hashes the same new WAVs on disk again. Distinguish
  `new_audio_unchanged: true` (matching published bytes),
  `false` (new bytes missing, replaced or corrupt), and `null` (old
  journal lacks this field, so integrity is *unknown*, not proven corrupted).
  The existing Update Revision inspector now surfaces that distinction.
- A damaged new revision can still be *rolled back* if all the prior
  revision's eligibility guards pass: this recovery action does not rely on
  the health of the discarded new audio, does not delete any package, and
  may be blocked after further builds or config changes. Inspecting a
  revision does not automatically revert it.
- Publication boundary capture refuses stale in-memory project settings
  when the on-disk config was already changed by another process. Index,
  Manifest and config digest checking still occurs again before commit.
  This closes one stale-capture scenario, **not** the general cross-process
  check-then-replace race.

`tests/test_remote_update_publish.py` injects a changed selected WAV
*after* the prepare record is written; asserts that the previous project
config is unchanged; detects post-commit audio corruption and symlink
substitution; proves old journals report unknown instead of corruption;
and tests external config modifications versus a stale loaded config.

**Boundaries:** SHA attestations are limited to the newly adopted WAVs.
Unchanged existing package WAV bytes are *not* all rehashed on every
publish, and source authenticity/playable audio format is outside this
check. The journal and atomic single-file project config replacement
still do not constitute a cross-process ACID transaction or a physical
power-loss acceptance test. No English/Japanese/Korean source text
editing or translated-Chinese Karaoke is involved.
