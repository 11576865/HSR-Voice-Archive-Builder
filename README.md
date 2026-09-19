# HSR Voice Archive Builder

Local tooling for turning fragmented **Honkai: Star Rail** character voice files into a reproducible continuous voice archive with bilingual metadata and subtitles.

The repository contains the **builder**, not redistributed game assets. Audio packages, LAB files, extracted resources, and full dialogue datasets stay local and are ignored by Git.

## Status

Current development version: **v0.2**.

The first regression corpus is a 379-line Evanescia/绯英 English voice archive. It is not included in this repository; it is used only as a local validation set.

## What v0.2 does

- Reads a canonical CSV order using either English field names or the older Chinese field names.
- Accepts an optional existing bilingual CSV instead of requiring one.
- Matches English WAV files and verifies optional SHA-256 values.
- Uses same-stem Chinese `.lab` text as the preferred Chinese source when available.
- Detects `_f` / `_m` voice variants and records both `logical_id` and `variant` in the manifest.
- Can classify a candidate update as an exact existing file, a variant of an existing logical line, or a genuinely new logical line.
- Optionally translates only the Chinese text that is still missing after official LAB and existing bilingual data are applied.
- Inserts configurable silence between lines and groups.
- Generates `manifest.json`, `manifest.csv`, a corrected bilingual index, SRT subtitles, and a build report.
- Optionally assembles a continuous FLAC and verifies the decoded PCM against the assembled source PCM.
- Accepts local directories, ZIP archives, and 7z archives as input sources.

## Requirements

- Python 3.11+
- FFmpeg available on `PATH` when building FLAC
- Packages in `requirements.txt`

```bash
python -m pip install -r requirements.txt
```

## Web UI

### Windows

Run `run_windows.bat`, then open:

```text
http://127.0.0.1:8765/
```

### Termux

```bash
chmod +x run_termux.sh
./run_termux.sh
```

The browser page is only a controller. Audio processing, archive extraction, hashing, LAB matching, translation write-back, and FFmpeg run locally.

## CLI

v0.2 uses the higher-level pipeline:

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

## Voice identity and update diffing

The physical filename remains the exact file identity. v0.2 additionally derives a logical identity for gender/variant suffixes:

```text
chapter5_3_evanescia_125_f.wav
        ↓
logical_id = chapter5_3_evanescia_125
variant    = f
```

This prevents a counterpart such as `chapter5_3_evanescia_125.wav` from being automatically counted as a completely new dialogue line.

To classify an update list against an existing manifest:

```bash
python -m app.diff \
  --manifest output/manifest.json \
  --candidates pending.txt \
  --out update_diff.json
```

## Output

```text
output/
├── manifest.json
├── manifest.csv
├── bilingual_index_corrected.csv
├── bilingual.srt
├── build_report.json
└── continuous.flac
```

The manifest is the durable machine-readable result. PDF/ASS/LRC and other presentation formats should be derived from it rather than used as primary data.

## Data integrity

The builder checks:

- expected WAV presence;
- duplicate filename conflicts;
- optional SHA-256 values from the canonical index;
- common PCM format across source WAV files;
- deterministic sample positions for the timeline;
- decoded FLAC PCM identity after final encoding.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests use synthetic audio only and do not require game data.

## Current validation

On the local 379-line Evanescia regression corpus, v0.2 reproduced:

- 379/379 WAVs;
- 181 same-stem official Chinese LAB matches;
- 198 existing translated Chinese lines;
- 0 missing Chinese lines;
- 4 filename variants;
- 48 kHz mono 16-bit PCM;
- 141,442,893 total output samples;
- exact decoded-FLAC PCM SHA-256 equality after assembly.

The 29-line pending list also separates into 4 variant counterparts and 25 genuinely new logical lines.

## Legal / project scope

See [NOTICE.md](NOTICE.md). This is an unofficial processing utility. Do not commit extracted game audio or full game text datasets to this repository.
