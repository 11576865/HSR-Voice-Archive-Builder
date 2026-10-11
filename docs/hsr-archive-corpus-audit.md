# HSR Archive Corpus Audit — read-only chapter/source overview

Date: 2026-10-11

## User workflow

The existing **Archive / Build Records（档案与构建记录）** area contains
**检查章节与来源覆盖**. It reads the active project's persisted
\`manifest.json\` and displays group/chapter coverage without adding a
separate workspace, changing source files, or invoking a build.

Both web backends expose the same read-only route:

\`GET /api/project/{project_id}/archive/audit\`

The response includes:

- groups in manifest order, with entry count and source/target text coverage;
- the number of manifest source WAV SHA-256 values that are syntactically valid;
- how many distinct WAV basename strings repeat (normal for parallel character trees);
- bounded issue records: duplicate entry ID, duplicate source member, missing
  member identity, missing source/target text, invalid hash, invalid source
  time, and nonmonotonic start order;
- optional artifact *presence* flags, not output integrity certifications.

## Invariants

A repeated basename is not a conflict when \`source_member_id\` distinguishes
the files. Duplicate member identities or indexes are flagged.

A missing target text is **coverage** information and may be intentional;
the canonical manifest's \`target_text\` is reported without borrowing human
\`final_chs\` overrides. Source audio positions are not adjusted.

The audit is intentionally cheap and **manifest-structural-only**. It does
not reread actual WAV bytes to recompute SHA-256, verify the assembled FLAC's
decoded PCM, inspect source packages, or assert that an optional ASS has been
visually approved. Presence of a well-formed hash is not evidence of byte
integrity. Failures do not rewrite files.

## Existing Karaoke improvements in this increment

- The ASS \`k\` / \`kf\` compiler preserves real word onsets with \`kt\`.
  Initial silence and pauses are not compressed to consecutive voiced durations.
- The derived alignment sidecar records the manifest original-WAV SHA-256,
  and an audio revision invalidates the cached alignment even if the text
  and duration are unchanged.
- Existing word timing controls and ASS preview are reused. There is no new
  Karaoke tab or extra subtitle-layout workbench surface.

Remaining possibilities (not part of this delivery): optional actual-WAV
hash scans with progress and cancellation, more complete multilingual
forced-alignment evaluation, and full live-project acceptance.


## Optional physical original-WAV checksum verification

The same existing archive-record panel now offers **逐个核验原始 WAV 字节**.
Both backend variants implement:

\`POST /api/project/{project_id}/archive/verify-sources\`

This starts an \`archive-verify\` progress job; it does not hold the UI thread
while scanning a character package. It resolves the original configured source
tree once (a ZIP/7z may need the existing \`.state/reference-preview\` extraction
cache), uses full package-relative \`source_member_id\` first, and permits a
basename fallback only when it is unambiguous. It streams WAV bytes through
SHA-256, checks per-file stat identity before/after hashing, and compares the
observed SHA-256 against \`manifest.json\`. Package archive stat identity is
also checked around the verification run.

Results distinguish:

- \`verified\`: source WAV bytes matched the Manifest SHA-256;
- \`mismatch\`: the source exists but its content hash differs;
- \`missing\`: exact member or unique-basename WAV not found;
- \`ambiguous\`: multiple same-basename sources with no explicit member ID;
- \`unverifiable\`: bad/missing expected hash, unsafe symlink path, or I/O failure;
- \`changed_during_verify\`: WAV size/mtime changed while being streamed;
- \`source_package_changed\`: ZIP/7z stat changed during the run, so all prior
  partial results are *not* a trustworthy all-source certification.

A \`trusted_complete\` result only means all referenced **original WAV member
bytes** were available and matched the manifest at this run. It does not prove
the archive's assembled FLAC has the same decoded PCM, nor that derived ASS/SRT,
AI translation or human proofreading is correct.

A byte audit is deliberately **opt-in**, not an automatic preliminary step
on every panel render. A user switching to another project while the task runs
must not see its result attached to the new project's archive view. Full
actual-device and large-archive acceptance remains independent of CI.
