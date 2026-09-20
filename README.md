# HSR Voice Archive Builder

Local-first tooling for turning fragmented **Honkai: Star Rail** character voice files into a reproducible continuous voice archive with bilingual metadata, subtitles, integrity checks, and update planning.

The repository contains the **builder**, not redistributed game assets. Audio packages, LAB files, extracted resources, and full dialogue datasets stay local and are ignored by Git.

## Status

Current development version: **v0.8**.

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

v0.4-v0.7 add failure-driven hardening based on upstream documentation, issue reports, and security advisories:

- no temporary continuous RIFF/WAV file during FLAC builds;
- raw PCM is streamed directly into FFmpeg, avoiding the classic ~4 GiB RIFF size ceiling;
- FLAC is written as `.partial.flac`, decoded and PCM-hash verified, then atomically promoted;
- manifest/report/subtitle/project-state writes use atomic replacement where practical;
- `py7zr>=1.1.3` is required;
- ZIP/7z extraction rejects suspicious traversal paths and symbolic links;
- extraction size and archive-member count are bounded;
- background job metadata is journaled locally;
- jobs left running when the process exits are reported as `interrupted` after restart rather than disappearing;
- GitHub Pages stays a static launcher instead of depending on cross-origin localhost requests.\n- v0.5 protects local and LAN control APIs with a per-process token and Host allowlist.\n- v0.5 supports PCM `WAVE_FORMAT_EXTENSIBLE` consistently on Python 3.11 and 3.12.\n- remote XLSX downloads and decompressed workbook size are bounded, with `defusedxml` installed.\n- GPT translation batches are checkpointed and reused after later failures/restarts.\n- launchers run an offline dependency preflight and no longer reinstall packages on every start.\n- LAN startup checks port conflicts and supports `--display-host` for multi-NIC/offline networks.\n- Termux/Android interruption risk is surfaced rather than hidden.

See [docs/reliability.md](docs/reliability.md) for the failure cases and upstream references that motivated these choices.

## Project dashboard

A project directory contains a local `.hsr-voice-project.json` file with source paths and build settings.

The dashboard can:

- create or reopen a project;
- remember the most recent project;
- edit build settings once instead of re-entering paths every run;
- launch a build as a background job;
- show current archive counts and generated outputs;
- scan a local TXT/JSON/CSV candidate list;
- check the current AI-Hobbyist English XLSX index for a configured character;
- classify candidates as exact existing files, variants of existing logical lines, or genuinely new logical lines;
- save the comparison as `update_plan.json` without modifying the current manifest;
- open the output directory on the processing host.

Remote index checking is currently **metadata/update discovery only**. v0.7 does not yet auto-download and splice new game audio into an existing archive.

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

The default dashboard workflow is now package-first:

```text
Choose English voice package
        ↓
Read-only scan / preflight
        ↓
Auto-detect character + covering local index
        ↓
Optional Chinese LAB package
        ↓
Auto-create internal filtered index/project
        ↓
Build FLAC + subtitles + manifest
        ↓
AI translation is enabled automatically when a provider is configured
```

On Termux, the dashboard discovers top-level `.7z` / `.zip` packages in the normal Download locations and passes filesystem paths to the local Python backend. It does not re-upload large archives through the browser. This avoids the browser file-input fake-path limitation and unnecessary localhost copies.

Quick scan is read-only. It blocks build when no WAVs are present, duplicate WAV basenames exist, or no local CSV index fully covers the package with English text. It refuses to invent a playback order when no reliable order source exists.

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

Production AI translation now adds two layers before accepting a line:

1. **Context + terminology**: each missing line can carry its immediate previous/next English line as disambiguation context. Only glossary terms that actually occur in the batch are injected into the prompt.
2. **Deterministic QA + targeted repair**: returned Chinese is checked for control-tag structure, required terminology, likely untranslated English residue and extreme length anomalies. Only suspicious rows are sent through one repair pass.

The built-in glossary is intentionally small and conservative. Current hard constraints include stable terms such as Evanescia→绯英, Planarcadia→二相乐园, Phantasmoon Games→幻月游戏, Wishpower→愿力, Supplicant→谒者, Graphia→绘世, Yao Guang→爻光, Fulwish→满愿 and Stellar Jade→星琼.

Every run writes:

```text
output/translation_qa.json
```

If a major formatting/terminology failure remains after the repair pass, the build stops before accepting the bad translation. Earlier paid batches remain checkpointed, so the failure does not force the whole character to be translated again.

## Chinese text precedence

```text
same-stem official Chinese LAB
        ↓
existing bilingual-index Chinese text
        ↓
AI API fallback, only when explicitly enabled
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

Translation checkpoints are bound to provider + Base URL + model. Switching from the official API to a relay cannot silently reuse another provider's cached translations.

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

The dashboard additionally supports a remote metadata check against:

```text
AI-Hobbyist/StarRail_Voice_Sorting_Scripts
Indexs/EN.xlsx
```

The remote URL is project-configurable and restricted to HTTPS.

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

GitHub Actions runs the synthetic test suite on Python 3.11, 3.12 and 3.13. No game data is required. The suite includes archive traversal/size-limit tests, a real FFmpeg streaming-FLAC regression test, localhost/LAN token and Host-header checks, `WAVE_FORMAT_EXTENSIBLE` input, remote-XLSX limits, runtime preflight, and translation-checkpoint recovery.

## Current regression validation

On the local 379-line Evanescia corpus, the deterministic archive pipeline reproduced 379/379 WAVs, 181 same-stem official Chinese LAB matches, 198 existing Chinese entries, 0 missing Chinese lines, 4 filename variants, and 141,442,893 output samples at 48 kHz mono 16-bit PCM. The previous full FLAC rebuild passed decoded PCM SHA-256 identity verification.

The earlier 29-line pending list separates into 4 variant counterparts and 25 genuinely new logical lines.

## Legal / project scope

See [NOTICE.md](NOTICE.md). This is an unofficial processing utility. Do not commit extracted game audio or full game text datasets to this repository.
