# HSR Voice Archive Builder

Local-first tooling for turning fragmented **Honkai: Star Rail** character voice files into a reproducible continuous voice archive with bilingual metadata, subtitles, integrity checks, and update planning.

The repository contains the **builder**, not redistributed game assets. Audio packages, LAB files, extracted resources, and full dialogue datasets stay local and are ignored by Git.

## Status

Current development version: **v0.9-G**.

The first regression corpus is a 379-line Evanescia/绯英 English voice archive. It is not included in this repository; it is used only as a local validation set.

## Fixed web entry

The repository now includes a static GitHub Pages launcher in `docs/`.

Expected public entry after Pages is enabled:

```text
https://11576865.github.io/HSR-Voice-Archive-Builder/
```

One-time GitHub setting:

```text
Repository → Settings → Pages
Source: Deploy from a branch
Branch: main
Folder: /docs
```

The public page is intentionally a launcher/documentation page, not the audio-processing runtime. Its “进入本机控制台” button navigates to:

```text
http://127.0.0.1:8765/
```

A hosted HTTPS page is not used as a hard dependency for direct `fetch()` access to localhost/LAN services, because browser Local Network Access / CORS behavior is not stable enough across browsers and versions.

## Design rule

**Browser UI, local-first processing.**

The browser is a control plane. Archive extraction, hashing, WAV/FLAC work, FFmpeg, translation calls, manifests, subtitles, and final files are handled by the local Python backend.

Two control modes are supported:

- **Local control:** browser and processor are the same Windows/Linux/macOS/Android-Termux device.
- **LAN control:** a phone or tablet can open the dashboard over the local network while the computer/Android host performs the actual work. LAN mode uses a generated control token.

No internet processing server is required.

## Reliability hardening

v0.4-v0.9-G add failure-driven hardening based on upstream documentation, issue reports, and security advisories:

- no temporary continuous RIFF/WAV file during FLAC builds;
- raw PCM is streamed directly into FFmpeg, avoiding the classic ~4 GiB RIFF size ceiling;
- FLAC is written as `.partial.flac`, decoded and PCM-hash verified, then atomically promoted;
- manifest/report/subtitle/project-state writes use atomic replacement where practical;
- `py7zr>=1.1.3` is required;
- ZIP/7z extraction rejects suspicious traversal paths and symbolic links;
- extraction size and archive-member count are bounded;
- background job metadata is journaled locally;
- jobs left running when the process exits are reported as `interrupted` after restart rather than disappearing;
- GitHub Pages stays a static launcher instead of depending on cross-origin localhost requests;
- local and LAN control APIs use a per-process token and Host allowlist;
- PCM `WAVE_FORMAT_EXTENSIBLE` works consistently on Python 3.11 and 3.12;
- remote XLSX downloads and decompressed workbook size are bounded, with `defusedxml` installed;
- AI translation batches are checkpointed and reused after later failures/restarts;
- launchers run an offline dependency preflight and no longer reinstall packages on every start;
- LAN startup checks port conflicts and supports `--display-host` for multi-NIC/offline networks;
- Termux/Android interruption risk is surfaced rather than hidden;
- v0.9-B writes an input-bound, artifact-verified stage chain and resumes completed work after a process restart;
- v0.9-C records Responses API usage, enforces optional per-build token/USD budgets before each new request, and caches successful structured-output capability probes;
- v0.9-D adds validated local glossary overlays, structural neighbor context, sparse semantic verification/repair, and checkpointed semantic-QA artifacts;
- v0.9-E adds recent-project switching, project-scoped task history, explicit language roles, multilingual EN/CHS/JP/KR Quick indexes, and optional second-package reference text;
- v0.9-F separates internal resumable state from user-facing output files through a project-local `.state` directory;
- v0.9-G reports project-source health and can safely relink moved voice packages after content-fingerprint verification.

See [docs/reliability.md](docs/reliability.md) for the failure cases and upstream references that motivated these choices.

## Project dashboard

A project directory contains a local `.hsr-voice-project.json` file with source paths and build settings.

The dashboard can:

- create or reopen a project;
- select a recent project explicitly and open it without implicitly replacing the current project just by changing the selector;
- remove a project from the switcher without deleting files, or delete the selected project with ownership-aware cleanup;
- remember recent projects and keep task history associated with the project that created each job;
- edit build settings once instead of re-entering paths every run;
- launch a build as a background job;
- show current archive counts and generated outputs;
- scan a local TXT/JSON/CSV candidate list;
- check the current AI-Hobbyist English XLSX index for a configured character;
- classify candidates as exact existing files, variants of existing logical lines, or genuinely new logical lines;
- save the comparison as `update_plan.json` without modifying the current manifest;
- open the output directory on the processing host.

Quick Mode project roots are marked as app-managed. Deleting one may remove that dedicated project directory, but original voice packages outside the project root are not touched. Manual project roots are treated conservatively: deletion removes the project marker, `.generated`, and an in-root output directory while retaining unrelated user files. “Remove from list” is non-destructive and can be reversed by reopening the project.

Remote index data can now resolve playback order and source text for Quick Mode, and it remains available for update discovery. The project still does not auto-download or splice new game audio into an existing archive.

## Requirements

- Python 3.11+
- FFmpeg on `PATH` when building FLAC
- packages in `requirements.txt`

```bash
python -m pip install -r requirements.txt
```

## Start locally

### Windows

```text
run_windows.bat
```

### Termux / Android

```bash
chmod +x run_termux.sh
./run_termux.sh
```

Termux uses a dependency-light stdlib HTTP server plus native 7-Zip. It intentionally avoids the FastAPI/Pydantic stack because current Pydantic v2 pulls `pydantic-core`, which can fall back to a Rust/maturin source build on Android. Vendor Python SDKs are not required: AI translation calls an OpenAI-compatible Responses endpoint directly over HTTPS using Python's standard library.

Default local URL:

```text
http://127.0.0.1:8765/
```

## Control the host from a phone or tablet

On Windows:

```text
run_windows_lan.bat
```

On Termux:

```bash
chmod +x run_termux_lan.sh
./run_termux_lan.sh
```

Or directly:

```bash
python -m app.launch --lan --no-browser
```

The launcher prints a URL containing a temporary token, for example:

```text
http://192.168.1.20:8765/?token=...
```

Open that URL on another device on the same LAN. The LAN entry URL carries a temporary token once; the local server then injects the per-process API token into the dashboard. API requests require that token, and unexpected Host values are rejected. Audio and generated files remain on the host device.

## Quick mode

The default dashboard workflow is package-first and language-aware:

```text
Choose primary voice package
        ↓
Choose primary-audio language + source-text language
        ↓
Read-only scan / preflight
        ↓
Resolve source text from local data or AI-Hobbyist EN / CHS / JP / KR index
        ↓
Optional target-language LAB package
        ↓
Optional second voice/text package used only as semantic reference
        ↓
Auto-create filtered internal index/project
        ↓
Build continuous FLAC from the primary package only
        ↓
Generate bilingual metadata/subtitles and translate missing target text
```

On Termux, the dashboard discovers top-level `.7z` / `.zip` packages in the normal Download locations and passes filesystem paths to the local Python backend. It does not re-upload large archives through the browser.

Quick Mode places new Termux projects in shared storage by default at `/storage/emulated/0/Download/HSR_Voice_Test/<project-name>/output`. Existing projects keep their saved paths. Set `HSR_VOICE_PROJECTS_DIR` to override the default project base.

The source-text language is independent from the translation target. Built-in remote discovery currently maps `en` → `EN.xlsx`, `zh-CN` → `CHS.xlsx`, `ja` → `JP.xlsx`, and `ko` → `KR.xlsx`. A same-stem LAB in the primary package is preferred for non-English source text when available; otherwise the selected language index can supply the text.

A second reference package never contributes PCM to `continuous.flac`. If it contains same-stem LAB files, those texts are supplied to the translator as semantic reference. For audio-only EN/CHS/JP/KR reference packages, Quick Mode can recover text from the corresponding AI-Hobbyist index and align it to primary lines by exact filename or a conservative structural identity. This avoids requiring ASR for indexed game resources; unmatched reference lines are reported rather than guessed.

Quick scan is read-only. It records content fingerprints for package inputs and revalidates them before project creation. Remote slices are cached for 24 hours, with bounded stale-cache fallback for temporary network failures.

The old manual project form remains under **Advanced / Manual project**.

## Build pipeline

The deterministic builder remains available through the project dashboard and CLI:

```bash
python -m app.pipeline \
  --index "index.csv" \
  --bilingual "bilingual.csv" \
  --chs "Chinese.7z" \
  --wavs "English-WAV.zip" \
  --out "output"
```

The bilingual CSV and Chinese LAB source are optional.

Useful options:

```text
--same-gap 0.40
--group-gap 1.20
--no-flac
--translate-missing
--translation-model gpt-5.6-sol
--translation-batch-size 80
--translation-token-budget 0
--translation-budget-usd 0
--glossary /path/to/terms.csv
--reference /path/to/reference-package.7z
--audio-language ja
--source-language ja
--target-language zh-CN
--reference-language en
--state-dir /path/to/project/.state
```

## Translation quality benchmark

To test the configured provider directly without treating official Chinese localization as a single gold answer, run:

```bash
python -m app.translation_benchmark --sample-size 50
```

If no recent project is available, pass an index explicitly:

```bash
python -m app.translation_benchmark \
  --index "/path/to/index.csv" \
  --sample-size 50 \
  --model gpt-5.6-sol
```

The default 50-line benchmark is deterministic and deliberately spreads samples across terminology-heavy, long/complex, short/context-sensitive, expressive, tag/placeholder and general dialogue. It uses one 50-line API batch by default; batching is internal and requires no manual "continue" interaction.

To inspect the selected sample without spending API balance:

```bash
python -m app.translation_benchmark --sample-size 50 --dry-run
```

Outputs are written to `translation_benchmark/`:

```text
benchmark_report.json
benchmark_results.csv
benchmark_sample.json
```

`benchmark_results.csv` contains blank review fields for semantic fidelity, omission/addition, terminology, Chinese fluency, character tone, severity and notes. Official Chinese text is intentionally not used as the scoring target.

## Translation automation and QA

Production AI translation now adds three acceptance layers:

1. **Structural context + terminology**: a missing line receives its immediate previous/next English line only when the neighbor shares an explicit group or source-detail relation. Only glossary terms that actually occur in the batch are injected into the prompt.
2. **Deterministic QA + targeted repair**: returned Chinese is checked for control-tag structure, required terminology, likely untranslated English residue and extreme length anomalies. Only suspicious rows receive one repair pass.
3. **Sparse semantic QA**: lines carrying higher semantic-risk signals—negation, quantities/comparatives, conditional logic, or mixed grammatical-person references—receive a separate structured verifier pass. A failed row gets one targeted repair and one re-verification; persistent semantic mismatches stop the build.

The built-in glossary is intentionally small and conservative. Current hard constraints include stable terms such as Evanescia→绯英, Planarcadia→二相乐园, Phantasmoon Games→幻月游戏, Wishpower→愿力, Supplicant→谒者, Graphia→绘世, Yao Guang→爻光, Fulwish→满愿 and Stellar Jade→星琼.

A project can add or override terminology with `--glossary` (CSV columns `English,Chinese` / `source,target`, or an equivalent JSON mapping/list). The merged glossary is fingerprinted, and row checkpoints are revalidated against the terminology relevant to that source line.

Translation builds write:

```text
output/translation_qa.json
output/semantic_qa.json
```

If deterministic or semantic QA still has a hard failure after its one repair pass, the build stops before accepting the bad translation. Earlier paid batches and successfully verified semantic rows remain checkpointed, so a later restart does not force the whole character through the API again.

## Moving source packages without rebuilding the project

A project may outlive the original filesystem location of its voice packages. The dashboard therefore reports source health separately from build completion:

- **primary**: the package whose WAV files feed the continuous FLAC;
- **target**: the optional official target-language LAB package;
- **reference**: the optional second-language reference package;
- **index / bilingual / glossary**: supporting text/configuration inputs.

For Quick Mode projects, the primary/target/reference package fingerprints are recorded when the project is created. If one of those packages is moved later, **Verify and relink** scans the candidate package and compares its content fingerprint with the stored identity. The project path is updated only on an exact match. A file with the same name but different bytes is rejected.

Older Quick Mode projects can use the source fingerprints already stored in `.generated/quick_scan.json`. Manual projects created before this metadata existed may not have a verifiable identity; in that case automatic relink is intentionally refused and the Advanced source field remains the explicit content-replacement path.

A verified relocation is different from replacing a package with new content. Manual source edits clear the old stored fingerprint so stale identity metadata cannot later approve the wrong package.

## Project state vs output

Dashboard-created projects now use two distinct locations:

```text
<project>/
├── output/      # user-facing finished artifacts
└── .state/      # checkpoints, QA, usage and validated stage state
```

The normal `output/` directory is intended for files you may actually consume or export: manifests, corrected bilingual index, subtitles, build report, update plan, and `continuous.flac` when enabled. Internal files such as `.translation_checkpoint.json`, `translation_qa.json`, `semantic_qa.json`, `translation_usage.json`, and the `stages/` chain live in `.state/`.

Existing projects are migrated conservatively on their next build. A legacy internal item is moved out of `output/` only when the corresponding destination does not already exist in `.state/`; newer state is never overwritten by migration.

Direct CLI users can opt into the same separation explicitly:

```bash
python -m app.pipeline \
  --index index.csv \
  --wavs Voice-WAV.zip \
  --out output \
  --state-dir .state
```

## Stage recovery

Each output directory contains a `stages/` chain:

```text
01_scan.json
02_metadata.json
03_translation.json
04_translation_qa.json
05_manifest.json
06_audio_state.json
final_report.json
```

Every state file is atomically written and bound to the content fingerprints of the selected inputs, relevant build settings, translation provider/Base URL/model, and the stage schema version. Recorded output artifacts are checked by size and SHA-256 before reuse. If an input or artifact changes, the affected work is rebuilt instead of silently accepting stale state.

`build_report.json` records `stage_resume.resumed` and `stage_resume.rebuilt`. In project-dashboard builds, the validated stage chain itself is stored under `.state/stages/`. Metadata and paid translation work can survive a Termux/process interruption. FLAC encoding remains all-or-nothing: only a completed, verified file is reusable; an interrupted encode starts again.

## Translation usage, budgets, and capability cache

v0.9-C makes API consumption observable instead of treating a successful translation as the only completion signal.

Each translation build writes:

```text
output/translation_usage.json
```

The usage ledger records provider/Base URL/model identity, a pre-build heuristic token estimate, every capability/translation/repair API call, and the actual `usage` fields returned by the Responses endpoint. When the provider supplies them, cached-input and reasoning-token detail are retained as well.

Two optional limits are available in Quick Mode, Advanced UI, and the CLI:

```text
--translation-token-budget 120000
--translation-budget-usd 2.50
```

A value of `0` means unlimited. Budget checks run immediately before a new request. If the next request would cross the configured limit, the build stops before sending it and previously completed checkpoints remain available for resume.

Token estimates are deliberately labeled estimates; they are not presented as exact tokenizer results. Actual API usage is the authoritative count after a response.

USD budgeting is stricter. The built-in price table is used only for the official OpenAI provider and is versioned in code. V-API and custom relays do **not** silently inherit official OpenAI prices. If a relay has known rates, configure both overrides locally:

```bash
export HSR_TRANSLATION_INPUT_USD_PER_MTOK=...
export HSR_TRANSLATION_OUTPUT_USD_PER_MTOK=...
```

Otherwise use a token budget. If a USD limit is requested while pricing is unknown, the request is blocked rather than inventing a cost.

Structured-output smoke results are cached for seven days by provider + Base URL + model + schema fingerprint. Re-running:

```bash
python -m app.credentials test --model gpt-5.6-sol
```

reuses a fresh successful capability result. Use `--force` when a new probe is intentionally required.

## Target-text precedence

```text
same-stem official target-language LAB
        ↓
existing target text in the bilingual index
        ↓
AI API translation into the configured target language
        ↓
missing
```

Translation providers are configured locally and are not stored in project files or the browser UI.

Existing project files keep their saved `translation_model`; change the dashboard field once if an older project still says `gpt-5.6-luna`.

For V-API:

```bash
python -m app.credentials configure --provider vapi
```

The command asks for the key with hidden input, stores it under `~/.hsr-voice-archive-builder/`, and reuses it on later starts. The configured Base URL is `https://api.gpt.ge/v1`.

For the official OpenAI API:

```bash
python -m app.credentials configure --provider openai
```

A custom OpenAI-compatible HTTPS endpoint is also supported:

```bash
python -m app.credentials configure --provider custom --base-url https://example.com/v1
```

Check the local configuration without revealing the key:

```bash
python -m app.credentials status
```

Run a tiny paid/usage-bearing structured-output smoke test before a real batch:

```bash
python -m app.credentials test --model gpt-5.6-sol
```

Environment variables `HSR_TRANSLATION_API_KEY`, `HSR_TRANSLATION_PROVIDER`, and `HSR_TRANSLATION_BASE_URL` override the saved local configuration. `OPENAI_API_KEY` remains a backward-compatible fallback only when the selected provider is `openai`.

Translation checkpoints are bound to provider + Base URL + model + source language + target language. Changing the provider, route, or language pair cannot silently reuse incompatible cached translations.

## Voice identity

The physical filename remains the exact file identity. A logical identity is also derived for gender/variant suffixes:

```text
chapter5_3_evanescia_125_f.wav
        ↓
logical_id = chapter5_3_evanescia_125
variant    = f
```

This prevents a counterpart such as `chapter5_3_evanescia_125.wav` from being automatically counted as a completely new dialogue line.

## Update checks

Local candidate files can still be checked from the CLI:

```bash
python -m app.diff \
  --manifest output/manifest.json \
  --candidates pending.txt \
  --out update_plan.json
```

The dashboard additionally supports a remote metadata check against the project's configured AI-Hobbyist source-text index. Quick Mode provides built-in mappings for `EN.xlsx`, `CHS.xlsx`, `JP.xlsx`, and `KR.xlsx`; the remote URL remains project-configurable and restricted to HTTPS.

## Archive extraction safety

The default uncompressed extraction ceiling is 32 GiB. It can be overridden for trusted larger archives:

```bash
export HSR_MAX_EXTRACT_BYTES=68719476736
```

The extractor also caps member count, validates archive member paths, and rejects symlink members.

## Output

```text
output/
├── manifest.json
├── manifest.csv
├── bilingual_index_corrected.csv
├── bilingual.srt
├── build_report.json
├── translation_qa.json
├── semantic_qa.json
├── translation_usage.json
├── update_plan.json
└── continuous.flac
```

The manifest is the durable machine-readable result. PDF/ASS/LRC and other presentation formats should be derived from it rather than used as primary data.

## Data integrity

The builder checks expected WAV presence, duplicate filename conflicts, optional SHA-256 values, a common PCM format, sample-accurate timeline positions, streamed PCM frame counts, and decoded-FLAC PCM identity after encoding.

## Tests

```bash
python -m unittest discover -s tests -v
```

GitHub Actions runs the synthetic test suite on Python 3.11, 3.12 and 3.13. No game data is required. The suite includes archive traversal/size-limit tests, a real FFmpeg streaming-FLAC regression test, localhost/LAN token and Host-header checks, `WAVE_FORMAT_EXTENSIBLE` input, remote-XLSX limits, runtime preflight, translation-checkpoint recovery, local glossary validation, structural context isolation, semantic repair/re-verification, and semantic-QA artifact recovery.

## Current regression validation

On the local 379-line Evanescia corpus, the deterministic archive pipeline reproduced 379/379 WAVs, 181 same-stem official Chinese LAB matches, 198 existing Chinese entries, 0 missing Chinese lines, 4 filename variants, and 141,442,893 output samples at 48 kHz mono 16-bit PCM. The previous full FLAC rebuild passed decoded PCM SHA-256 identity verification.

The earlier 29-line pending list separates into 4 variant counterparts and 25 genuinely new logical lines.

## Legal / project scope

See [NOTICE.md](NOTICE.md). This is an unofficial processing utility. Do not commit extracted game audio or full game text datasets to this repository.
