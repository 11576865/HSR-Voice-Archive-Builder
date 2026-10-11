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
