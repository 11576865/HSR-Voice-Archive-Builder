# HSR Voice Archive Builder

Local tooling for turning fragmented **Honkai: Star Rail** character voice files into a reproducible continuous voice archive with bilingual metadata and subtitles.

The repository contains the **builder**, not redistributed game assets. Audio packages, LAB files, extracted resources, and full dialogue datasets stay local and are ignored by Git.

## What it does

- Reads a canonical CSV order for a character voice archive.
- Matches English WAV files and verifies optional SHA-256 values.
- Uses same-stem Chinese `.lab` text as the preferred Chinese source when available.
- Falls back to an existing bilingual index, with an optional GPT translation helper for genuinely missing Chinese text.
- Inserts configurable silence between lines and groups.
- Generates `manifest.json`, `manifest.csv`, a bilingual index, SRT subtitles, and a build report.
- Optionally assembles a continuous FLAC and verifies its decoded PCM against the assembled source PCM.
- Accepts local directories, ZIP archives, and 7z archives as input sources.

## Status

Current version: **v0.1**.

The first regression corpus was a 379-line Evanescia/绯英 English voice archive. That corpus is **not included** in this repository; it is used only as a local validation set.

## Requirements

- Python 3.11+
- FFmpeg available on `PATH` when building FLAC
- Packages in `requirements.txt`

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Web UI

### Windows

Run:

```text
run_windows.bat
```

Then open:

```text
http://127.0.0.1:8765/
```

### Termux

```bash
chmod +x run_termux.sh
./run_termux.sh
```

Then open the same local address in the browser.

The browser page is only a controller. Audio processing, archive extraction, hashing, FFmpeg, LAB matching, and output generation happen locally.

## CLI

```bash
python -m app.builder \
  --index "full_index.csv" \
  --bilingual "bilingual_index.csv" \
  --chs "chinese_lab_pack.7z" \
  --wavs "english_wavs.zip" \
  --out "output"
```

Useful options:

```text
--same-gap 0.40
--group-gap 1.20
--no-flac
```

## Output

A normal build produces:

```text
output/
├── manifest.json
├── manifest.csv
├── bilingual_index_corrected.csv
├── bilingual.srt
├── build_report.json
└── continuous.flac          # when FLAC build is enabled
```

The manifest is the canonical machine-readable result. PDF/ASS/LRC and other presentation formats should be derived from it rather than used as primary data.

## Chinese text precedence

The builder currently uses this order:

```text
same-stem official Chinese LAB
        ↓
existing bilingual-index Chinese text
        ↓
missing (eligible for translation fallback)
```

`app/translator.py` is deliberately separate from the deterministic builder. It is intended only for records that truly lack Chinese text.

## GPT fallback translation

Set the API key through the environment rather than storing it in the project:

```bash
export OPENAI_API_KEY="..."
```

The translation helper preserves record IDs and returns structured Chinese text for automatic write-back. The core archive build does not require an API key.

## Data integrity

The builder can verify:

- expected WAV presence;
- duplicate filename conflicts;
- optional SHA-256 values from the canonical index;
- common PCM format across source WAV files;
- deterministic sample positions for the timeline;
- decoded FLAC PCM identity after final encoding.

## Tests

The repository includes a synthetic-data regression test; no game data is required:

```bash
python -m unittest discover -s tests -v
```

## Roadmap

Planned work includes stronger identity handling for voice variants, incremental dataset updates, resume/retry support for translation jobs, richer validation reports, and additional subtitle/document exporters.

## Legal / project scope

See [NOTICE.md](NOTICE.md). This is an unofficial processing utility. Do not commit extracted game audio or full game text datasets to this repository.
