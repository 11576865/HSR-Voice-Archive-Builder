# Changelog

## v0.9-E

- Added explicit recent-project selection with separate Open, New/Import, Remove-from-list, and Delete actions. Removing only hides a project from the switcher; reopening restores it. Deleting a Quick Mode managed project removes its dedicated project root, while manual-project deletion removes only app-owned config/generated/output data and preserves unrelated user files. Deleted-project task history is purged.
- Added explicit language roles for primary audio, source text, translation target and optional reference text. Translation prompts, semantic verification, stage fingerprints and translation checkpoints now include the selected source/target languages.
- Quick Mode can resolve source text from AI-Hobbyist EN, CHS, JP or KR indexes according to the selected source language instead of assuming English.
- Added an optional second voice/text package used only as translation reference. Same-stem LAB is preferred; for audio-only EN/CHS/JP/KR packages, Quick Mode can recover indexed text and align it to the primary package by exact filename or a conservative group + numeric-tail identity. Reference audio is never mixed into the output FLAC.
- Non-English source text may also come from same-stem LAB in the primary package when available; otherwise the selected language index remains usable.
- Translation QA and sparse semantic-risk detection are now language-aware enough to avoid applying English-only residue heuristics to non-Chinese targets and to cover common negation/condition patterns in Chinese, Japanese and Korean.
- Build controls provide immediate launch feedback, stale diagnostic errors are cleared after successful jobs, and failed task history shows concise failure reasons.

## v0.9-D

- Added validated local glossary overlays in CSV or JSON. Project glossaries override the conservative built-in terminology map, are fingerprinted deterministically, and participate in stage/checkpoint validation.
- Translation context is now structural rather than blindly adjacent: previous/next English lines are supplied only when they share an explicit group or source-detail relation with the target.
- Added sparse semantic QA for high-risk lines containing negation, quantities/comparatives, conditional logic, or mixed grammatical-person references.
- High-risk translations receive a structured semantic verifier pass. Failed rows receive one targeted translation repair followed by one re-verification; persistent semantic failures stop the build.
- Semantic QA state is checkpointed per row, so verified rows are not re-audited after restart. `semantic_qa.json` is bound into the validated translation-QA stage and tampering forces stage recovery without discarding reusable paid translations.
- Added Quick/Advanced project wiring for an optional glossary path and expanded regression coverage for glossary parsing, structural context, semantic verifier behavior, targeted repair, checkpoint reuse, and QA-artifact recovery.

## v0.9-C

- Added per-build translation token and USD budget controls. A projected over-budget request is stopped before the request is sent; completed batches and checkpoints remain reusable.
- Parse and persist Responses API usage after every paid capability/translation/repair call in `translation_usage.json`, including input/output/total tokens plus cached-input and reasoning-token detail when supplied.
- Quick Scan now estimates pending translation count and token workload before a build. Estimates are explicitly heuristic; actual provider usage remains authoritative.
- Added cost estimation for the official OpenAI route using a versioned local price table. Relays/custom providers do not inherit OpenAI prices; explicit per-MTok environment overrides are required for USD-budget enforcement when provider pricing is unknown.
- Structured-output capability smoke tests are cached for seven days by provider + Base URL + model + schema fingerprint. Unchanged routes no longer repeat the paid probe; `python -m app.credentials test --force` bypasses the cache.
- Added Quick Mode and Advanced UI controls for translation budgets plus estimated/actual token and API-call reporting.
- Kept 401/403 and other non-retryable HTTP failures fail-fast while retaining bounded retry behavior for 408/409/429/5xx and network failures.

## v0.9-B

- Added input-bound stage recovery for scans, metadata, translation, translation QA, manifests, verified audio, and final reports.
- Completed stages are reused only when the source content, build settings, translation route/model, stage schema, and recorded artifacts still validate.
- Restarting after a Termux/process interruption no longer repeats completed metadata or translation work; archive extraction is also skipped when no unfinished stage needs it.
- A completed FLAC is reused only after its size and SHA-256 match the recorded audio state. Interrupted or modified audio is rebuilt from the beginning; mid-stream FFmpeg resume is not claimed.
- Every run reports which stages were resumed or rebuilt in `build_report.json`.

## v0.9-A

- Quick Mode now falls back to the configured AI-Hobbyist English index when no complete local CSV is available.
- Remote fallback resolves exact WAV filenames directly, avoiding English/Chinese character-name mismatches. It requires a dominant remote character label (at least 75% while allowing story aliases) and one non-empty English record for every WAV; duplicate or partial matches remain blocking failures.
- Remote character slices are cached for 24 hours, with a bounded seven-day stale-cache fallback for temporary network failures.
- Quick Scan records a content-level SHA-256 fingerprint for each source package (or a deterministic tree hash for directories) and revalidates it before project creation.
- Generated remote indexes preserve workbook order, derive stable groups from filenames, and are bound to a remote-record fingerprint so changed metadata cannot be silently reused.

## v0.8

- Added context-aware AI translation: immediate neighboring English lines are supplied for disambiguation while only the target line is returned.
- Added a conservative built-in HSR terminology set and inject only terms relevant to the current batch.
- Added deterministic translation QA for control-token structure, terminology, English residue and extreme length anomalies.
- Suspicious new translations receive one targeted repair pass; persistent major failures stop the build and are written to `translation_qa.json`.
- Existing paid translation checkpoints are revalidated against current QA rules before reuse.

- Added package-first Quick Mode as the default dashboard workflow.
- Auto-discovers local `.7z` / `.zip` voice packages in common Termux Download paths without browser re-upload.
- Added read-only package inventory for WAV/LAB counts, duplicate-basename detection and character hints.
- Auto-selects a local CSV index by actual WAV coverage and English-text completeness, then generates a filtered internal index.
- Quick build creates the local project automatically and enables configured AI translation without exposing index/project internals.
- Quick scan blocks rather than guessing when playback order cannot be established reliably.
- Retained the v0.7 manual project form under an advanced section.

## v0.7

- Added a deterministic 50-line direct translation-quality benchmark with dry-run mode, integrity checks and review-ready CSV/JSON output; official Chinese localization is not treated as a gold answer.
- Set `gpt-5.6-sol` as the default translation model after validating the configured V-API route; existing project files retain their saved model until changed.
- Generalized translation from OpenAI-only to OpenAI-compatible Responses API providers.
- Added built-in provider presets for official OpenAI and V-API (`https://api.gpt.ge/v1`), plus custom HTTPS/loopback endpoints.
- Added local credential management with hidden terminal input and best-effort `0600` key storage under `~/.hsr-voice-archive-builder/`; API keys are not written to project files or served to the browser.
- Added `python -m app.credentials status` and a one-line structured-output smoke test.
- Translation checkpoints are now bound to provider + Base URL + model so changing relay/provider cannot silently reuse cached translations from a different route.
- Dashboard/runtime wording now reports generic AI API translation rather than assuming OpenAI.
- Kept the v0.6 official OpenAI environment variable path backward compatible.

## v0.6

- Replaced the OpenAI Python SDK dependency with a small standard-library HTTPS client for the official Responses API.
- Enabled GPT fallback translation on Termux/Android without `jiter`, `pydantic-core`, `maturin`, or Rust.
- Kept Structured Outputs with JSON Schema, exact ID round-trip validation, non-empty translation validation, and per-batch checkpoints.
- Added bounded retry handling for transient network errors and HTTP 408/409/429/5xx, including `Retry-After` support.
- Removed the unused `openai` package from desktop requirements as well.
- Added raw Responses payload parsing and REST transport regression tests.
- Updated desktop and Termux servers to require only `OPENAI_API_KEY` when GPT translation is enabled.
- Updated runtime status to report direct REST translation and whether an API key is configured.

## v0.5

### Termux hotfixes

- Termux no longer installs py7zr; native 7-Zip is used instead.
- Termux no longer installs the OpenAI Python SDK because its jiter/Rust dependency is not reliably buildable on Android.
- Termux no longer installs FastAPI/Uvicorn/Pydantic. A standard-library HTTP server preserves the browser control UI without pulling pydantic-core/maturin/Rust.
- Desktop builds keep the existing FastAPI/Uvicorn backend.
- Added regression coverage that blocks FastAPI/Uvicorn/Pydantic imports while importing the Termux lightweight server.

- Added a per-process control token for local mode as well as LAN mode; all control/data APIs require the custom token header.
- Added Host allowlisting, explicit LAN-only non-loopback binds, no-store/referrer/CSP response hardening, and a LAN dashboard entry gate.
- Added a project-owned RIFF/RF64 PCM parser so PCM `WAVE_FORMAT_EXTENSIBLE` works on Python 3.11 as well as 3.12+.
- Added remote XLSX streamed-download limits, ZIP expansion/member limits, HTTPS redirect enforcement, structural checks, and `defusedxml`.
- Added finite OpenAI retry/timeout settings plus per-batch local translation checkpoints keyed by model and English-text SHA-256.
- Moved large build/extraction working directories to the output filesystem instead of the system temp partition.
- Added a stdlib-only runtime dependency preflight; startup scripts only install missing/outdated dependencies instead of hitting package servers on every launch.
- Added clear FFmpeg/API-key preflight failures before a build starts.
- Added port-conflict detection, multi-interface LAN address discovery, and `--display-host`.
- Added Termux/Android interruption warnings while retaining atomic outputs and interrupted-job journaling.
- Expanded CI tests for localhost security, extensible WAV, XLSX expansion limits, translation checkpoint recovery, and runtime dependencies.
- Expanded `docs/reliability.md` with the internet failure cases that motivated these changes.

## v0.4

- Added a static GitHub Pages launcher in `docs/` with a stable public entry and direct navigation to the local console.
- Deliberately avoided making hosted-HTTPS-to-localhost `fetch()` a runtime dependency after reviewing current Local Network Access / CORS failures.
- Replaced the temporary continuous WAV with direct raw-PCM streaming into FFmpeg.
- Added atomic `.partial.flac` promotion only after decoded PCM verification.
- Added atomic writes for primary generated metadata and project state.
- Added a persistent local job journal; queued/running jobs become `interrupted` after an application restart.
- Pinned `py7zr>=1.1.3` after reviewing 2026 archive-extraction security advisories.
- Added ZIP/7z traversal checks, symlink rejection, member-count limits, and an extraction-size ceiling.
- Added regression tests for malicious ZIP paths, extraction-size limits, and streamed FLAC verification.
- Added `docs/reliability.md` documenting failure cases and upstream references.

## v0.3

- Reworked the browser page into a persistent project dashboard instead of a one-shot parameter form.
- Added local project files (`.hsr-voice-project.json`) and last-project restore.
- Added background jobs so long FLAC builds and remote update checks do not keep one browser request open.
- Added local candidate update scanning from TXT, JSON, or CSV.
- Added optional remote update checks against the AI-Hobbyist English XLSX index.
- Kept update checking non-destructive: it writes an `update_plan.json` and does not alter the existing manifest.
- Added local/LAN launch modes. LAN control uses a generated token while processing remains on the host device.
- Added Windows and Termux LAN launchers.
- Added an output-folder action for the host device.
- Added GitHub Actions tests for Python 3.11 and 3.12.
- Added project persistence, XLSX parsing, update classification, and background-job tests.

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
