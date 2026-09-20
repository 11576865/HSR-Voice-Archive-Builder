# Changelog

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
