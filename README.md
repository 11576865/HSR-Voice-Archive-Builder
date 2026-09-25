# HSR Voice Archive Builder

Local-first archive builder for **Honkai: Star Rail** character voice packages.

It turns indexed voice resources into a reproducible archive with a continuous FLAC, timed subtitles, manifests, update metadata, and optional translation assistance. The browser UI is only a controller; source voice packages and finished audio remain on the processing device.

**Current development version:** v0.9-L  
**Web entry:** https://11576865.github.io/HSR-Voice-Archive-Builder/

> Unofficial processing utility. Do not commit extracted game audio or full game text datasets to this repository.

## What it does

The normal project workflow can:

- scan a primary character voice package and resolve its indexed dialogue order;
- build one verified `continuous.flac` as the core archive artifact;
- generate `HSR_Voice_Archive.srt` from the same resolved Timeline;
- generate ASS later as an optional finished output;
- generate an optional black-video MKV containing the FLAC, without burning subtitles;
- use official Chinese text when a safe filename/path match exists;
- use confirmed incremental `Chinese(PRC)` text as official Chinese target text before API fallback;
- translate only lines that still lack target text;
- preserve human subtitle corrections across later rebuilds;
- check remote indexes for new voice lines, download confirmed additions, and rebuild only affected stages;
- resume validated stages after interruption.

The deterministic manifest and Timeline remain the durable machine-readable archive state. Presentation outputs are derived from them.

## Recommended workflow

### Android / Termux

First install:

```bash
pkg install -y git
cd ~
git clone https://github.com/11576865/HSR-Voice-Archive-Builder.git
cd HSR-Voice-Archive-Builder
bash run_termux.sh
```

Normal start:

```bash
cd ~/HSR-Voice-Archive-Builder
git pull --ff-only
bash run_termux.sh
```

Then open:

```text
http://127.0.0.1:8765/
```

The Termux path uses the lightweight stdlib server and native 7-Zip path documented in [docs/termux.md](docs/termux.md).

### Windows

1. Install Python 3.11 or newer and FFmpeg, and make sure both `python --version` (or `py -3 --version`) and `ffmpeg -version` work in a new Command Prompt. Git is only needed if you plan to clone or update with Git.
2. Download this repository with **Code → Download ZIP**, extract the ZIP, and open the extracted project folder. Or use:

   ```powershell
   git clone https://github.com/11576865/HSR-Voice-Archive-Builder.git
   cd HSR-Voice-Archive-Builder
   ```

3. Double-click `run_windows.bat` in that folder, or run `.\run_windows.bat` from PowerShell. The launcher checks Python and installs missing Python packages from `requirements.txt` on first start. Keep the console window open while using the dashboard.
4. Open `http://127.0.0.1:8765/` on the same computer if the browser does not open automatically. The hosted GitHub Pages site is a launcher; processing takes place in this local dashboard.

Place the voice-package archives in your Windows `Downloads` folder for automatic discovery, or choose their local file paths manually. Termux project paths such as `/storage/emulated/0/Download/...` do not point to Windows files. Copy the source packages to this computer and create or relink the project using their Windows paths.

If the console reports that port 8765 is occupied, start with `python -m app.launch --port 8766` (or `py -3 -m app.launch --port 8766`) and open the URL it prints. Run `python -m app.preflight` to inspect dependencies and FFmpeg. If the console closes after an error, run the batch file from an open Command Prompt so the message remains visible.

For LAN control from a phone or another device, run `run_windows_lan.bat` and use the tokenized URL printed by the host. Audio processing and project files remain on the Windows computer.

## Project flow

```text
primary voice package + canonical index
                │
                ▼
        resolve ordered entries
                │
        ┌───────┴────────┐
        ▼                ▼
 official/known text   missing target text
        │                │
        │          optional API translation
        └───────┬────────┘
                ▼
        resolved Timeline
                │
        ┌───────┼───────────────┐
        ▼       ▼               ▼
    manifest   SRT        continuous FLAC
        │
        ├── human subtitle overrides
        ├── optional ASS
        └── optional black MKV
```

Continuous FLAC is a fixed project output. ASS and black MKV are on-demand finished outputs.

## Remote index cache and offline use

Quick Mode resolves dialogue order from the AI-Hobbyist text index workbook. That workbook is cached **by URL** under `~/.hsr-voice-archive-builder/remote-index-cache/`, so one successful download serves every character: fresh for 24 hours, then up to seven more days from the stale copy with a warning when a refresh fails.

For a device without usable network access, download `EN.xlsx` (or `CHS/JP/KR.xlsx`) from the AI-Hobbyist index repository on any networked machine, copy it over, and point the environment variable at it before starting the app:

```bash
export HSR_VOICE_INDEX_FILE=/path/to/EN.xlsx
bash run_termux.sh
```

## Incremental updates

The dashboard can check the configured remote source-text index for additions and resolve confirmed files against the Hugging Face voice dataset.

For Simplified Chinese target projects:

1. new primary-language audio is resolved and downloaded;
2. a same-path `Chinese(PRC)` row is checked;
3. when the Chinese audio is actually available, its official transcription becomes target Chinese text;
4. only remaining unmatched lines are sent to the configured translation API.

Existing v0.9-K incremental indexes that stored this Chinese text only as `reference_text` are migrated during rebuild; the audio does not need to be downloaded again.

## Finished outputs

Typical project output:

```text
output/
├── manifest.json
├── manifest.csv
├── bilingual_index_corrected.csv
├── timeline_resolved.json
├── build_report.json
├── update_plan.json
├── HSR_Voice_Archive.srt
└── continuous.flac
```

Optional outputs:

```text
HSR_Voice_Archive.ass
HSR_Voice_Archive_Black.mkv
```

Internal checkpoints, QA state, translation usage, and stage recovery files live under the project `.state/` directory instead of the finished-output directory.

## Subtitle review

The dashboard includes a single-entry proofreading workspace.

- source text and official/API references are read-only;
- only the final Chinese subtitle is editable;
- edits are stored as a non-destructive override layer;
- official/API source provenance is preserved;
- later full rebuilds re-apply saved overrides;
- SRT and any already-generated ASS are refreshed from the same final text layer.

## Translation configuration

Translation is optional and only used when target text is missing or when explicitly enabled official-text review requires it.

Configure a compatible provider locally:

```bash
python -m app.credentials configure --provider custom --base-url https://example.com/v1
python -m app.credentials test
```

Credentials are stored locally and are not written into project JSON or the browser UI.

More detail on translation QA, budgets, checkpoint behavior, source relinking, archive safety, and recovery semantics is maintained in [docs/reliability.md](docs/reliability.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Reliability and recovery](docs/reliability.md)
- [Reference audio workbench](docs/reference_workbench.md)
- [Termux notes](docs/termux.md)
- [ASS layout engine](docs/layout_engine.md)
- [Changelog](CHANGELOG.md)
- [Project notice](NOTICE.md)

The GitHub Pages site is intentionally a compact launcher and setup reference, not a duplicate of this README or the in-app dashboard.

## Development and tests

Install test dependencies and run:

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

GitHub Actions validates Python 3.11, 3.12, 3.13, plus the lightweight Termux import surface.

The test suite uses synthetic fixtures; no game data is required.

## Project scope

This repository builds and validates archives from user-supplied/local source material. It does not ship extracted game audio or full game text datasets.

See [NOTICE.md](NOTICE.md) for the project notice.
