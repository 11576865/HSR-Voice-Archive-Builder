# Termux notes

The Android/Termux path intentionally avoids Python packages that commonly fall back to native Rust/C builds.

## Why

Current public Termux failures repeatedly show these chains:

- `py7zr -> psutil` source build rejects Android.
- `openai -> jiter -> maturin/Rust` fails on the Android target.
- `fastapi -> pydantic v2 -> pydantic-core -> maturin/Rust` fails when pip has no official Android wheel.

The Termux launcher therefore uses:

- Python stdlib HTTP server instead of FastAPI/Uvicorn/Pydantic.
- native Termux 7-Zip instead of py7zr.
- no vendor AI SDK in the Android base install; translation calls an OpenAI-compatible Responses HTTPS API directly with Python's standard library.

Desktop installs keep the richer Python dependency stack. No Rust toolchain is required for the normal Termux startup path.

## Start

```bash
cd ~/HSR-Voice-Archive-Builder
git pull
bash run_termux.sh
```

Expected end state:

```text
Python 3.13.x: OK
Python dependencies: OK
7-Zip: .../7zz
Local control URL: http://127.0.0.1:8765/
Termux lightweight control server: http://127.0.0.1:8765/
```

**Termux package split (confirmed on 2.18.1):** `fontconfig` may already be
installed while `fc-match` is missing. The `fc-match` executable belongs to
**`fontconfig-utils`**, not the core `fontconfig` package. The launcher installs
`fontconfig-utils` when `fc-match` is absent and stops with a diagnostic if
the executable remains unavailable. To repair an already-running installation:

```bash
pkg install -y fontconfig-utils
command -v fc-match
fc-match -V
python -c 'import shutil; print(shutil.which("fc-match"))'
```

Stop the existing HSR process and restart via `bash run_termux.sh` (or
`bash run_termux_lan.sh` in LAN mode). A GitHub pull, browser reload or
installation of `fontconfig` alone does not provision `fc-match`. Actual
FFmpeg/libass previews of imported fonts require its identity check; browser
geometry/SVG previews do not establish genuine ASS font selection. The font
file stays in the project and need not be installed globally.

Keep Termux running and open `http://127.0.0.1:8765/` in the browser.

## Read-only archive acceptance after a real device build

After pulling the latest code, restarting Termux, and manually running
"构建 / 更新档案" for the existing project, run this from the repository root:

```bash
python -m app.device_acceptance \
  --project "/storage/emulated/0/Download/HSR_Voice_Test/evanescia-3"
```

The command **does not start a build, delete files, overwrite recovery
checkpoints, or send project media to GitHub**. It prints a small JSON result
that avoids transcript text, credentials, full traceback and source paths.
The exit code is 0 for a verified result and 2 for incomplete acceptance
evidence; a nonzero exit is not automatically a newly discovered crash.

The field report compares the latest stored project build job, its completion
timestamp, the pipeline report, the sealed MKV's provenance state and FFprobe
video/FLAC/ASS/SRT streams. It checks that the local Git checkout contains
the POS-pair fix commit. Read the build timestamp: an old completed job is
not evidence you just reran the actual voice archive. Run the command **only
after** the real current build finishes, not while the job is running.

The MKV provenance check reads and hashes the output file. On large mobile
archives, this may take substantial I/O and battery, but it does not rewrite
media. FFprobe checks stream types, not complete audio-frame decoding or
real-font playback. Device playback and final audio/subtitle acceptance
remain separate from this report.

## Translation

Core archive functions, local project management, update scanning, subtitles and FLAC remain available on Termux.

AI fallback translation works without a vendor Python SDK. Configure a provider once with hidden terminal input:

```bash
python -m app.credentials configure --provider vapi
```

For the official OpenAI API instead:

```bash
python -m app.credentials configure --provider openai
```

The key is stored only in the local Termux home state directory with best-effort owner-only permissions. It is not placed in the browser UI or project JSON. Later `bash run_termux.sh` launches reuse the saved provider automatically.

Before a real character batch, verify the configured Responses endpoint and Structured Outputs with one tiny request:

```bash
python -m app.credentials test

# Change the default model for future projects; no API key prompt.
python -m app.credentials model qwen3.7-plus
```

Translation keeps batch checkpoints, exact ID validation and bounded retry behavior. Checkpoints are bound to provider + Base URL + model.


### Accurate-enough font advances are a prerequisite, not libass proof

The Termux launchers also install native `python-pillow` when `PIL.ImageFont` is missing. Earlier installations could silently substitute per-character width ratios for actual imported TTF/OTF advances. Pillow measures the selected font rather than guesses, but its shaping/line heights are **not pixel-identical to FFmpeg/libass**. The normal Layout canvas therefore never displays SVG glyphs as fallback while the true frame is rendering, stale or failed. Auxiliary SVG is opt-in and cannot authorize a final line break; whole-corpus real-ink acceptance remains separate.
