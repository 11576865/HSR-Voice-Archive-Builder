# Changelog

## v0.2

- Made the existing bilingual CSV optional.
- Made Chinese LAB input optional.
- Added column aliases so the pipeline is not tied to one character-specific CSV header layout.
- Added `logical_id` / `variant` identity handling for `_f` and `_m` filenames.
- Added candidate-update diffing through `python -m app.diff`.
- Connected optional GPT fallback translation to the build pipeline.
- Added translation batching and strict ID round-trip validation.
- Updated the local web UI for optional inputs and missing-translation control.
- Added synthetic v0.2 tests.
- Revalidated the 379-line Evanescia corpus and lossless FLAC PCM identity.
