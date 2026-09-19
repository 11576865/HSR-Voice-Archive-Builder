# HSR Voice Archive Builder

Local-first tooling for turning fragmented **Honkai: Star Rail** character voice files into a reproducible continuous voice archive with bilingual metadata, subtitles, integrity checks, and update planning.

The repository contains the **builder**, not redistributed game assets. Audio packages, LAB files, extracted resources, and full dialogue datasets stay local and are ignored by Git.

## Status

Current development version: **v0.3**.

The first regression corpus is a 379-line Evanescia/绯英 English voice archive. It is not included in this repository; it is used only as a local validation set.

## Design rule

**Browser UI, local-first processing.**

The browser is a control plane. Archive extraction, hashing, WAV/FLAC work, FFmpeg, translation calls, manifests, subtitles, and final files are handled by the local Python backend.

Two control modes are supported:

- **Local control:** browser and processor are the same Windows/Linux/macOS/Android-Termux device.
- **LAN control:** a phone or tablet can open the dashboard over the local network while the computer/Android host performs the actual work. LAN mode uses a generated control token.

No internet processing server is required.

## v0.3 dashboard

v0.3 adds persistent projects. A project directory contains a local `.hsr-voice-project.json` file with source paths and build settings.

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

Remote index checking is currently **metadata/update discovery only**. v0.3 does not yet auto-download and splice new game audio into an existing archive.

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

Open that URL on another device on the same LAN. The browser stores the token only for that session and sends it with API requests. The audio and generated files remain on the host device.

## Build pipeline

The underlying v0.2 deterministic builder remains available through the v0.3 project dashboard and CLI:

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
--translation-model gpt-5.6-luna
--translation-batch-size 80
```

## Chinese text precedence

```text
same-stem official Chinese LAB
        ↓
existing bilingual-index Chinese text
        ↓
GPT fallback, only when explicitly enabled
        ↓
missing
```

The API key is read only from the local environment:

```bash
export OPENAI_API_KEY="..."
```

Do not put API keys in project files or the browser UI.

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

## Output

```text
output/
├── manifest.json
├── manifest.csv
├── bilingual_index_corrected.csv
├── bilingual.srt
├── build_report.json
├── update_plan.json          # after an update check
└── continuous.flac           # when enabled
```

The manifest is the durable machine-readable result. PDF/ASS/LRC and other presentation formats should be derived from it rather than used as primary data.

## Data integrity

The builder checks expected WAV presence, duplicate filename conflicts, optional SHA-256 values, a common PCM format, sample-accurate timeline positions, and decoded-FLAC PCM identity after encoding.

## Tests

```bash
python -m unittest discover -s tests -v
```

GitHub Actions runs the synthetic test suite on Python 3.11 and 3.12. No game data is required.

## Current regression validation

On the local 379-line Evanescia corpus, the deterministic archive pipeline reproduced 379/379 WAVs, 181 same-stem official Chinese LAB matches, 198 existing Chinese entries, 0 missing Chinese lines, 4 filename variants, and 141,442,893 output samples at 48 kHz mono 16-bit PCM. A full FLAC rebuild passed decoded PCM SHA-256 identity verification.

The earlier 29-line pending list separates into 4 variant counterparts and 25 genuinely new logical lines.

## Legal / project scope

See [NOTICE.md](NOTICE.md). This is an unofficial processing utility. Do not commit extracted game audio or full game text datasets to this repository.
