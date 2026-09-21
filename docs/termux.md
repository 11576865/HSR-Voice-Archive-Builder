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

Keep Termux running and open `http://127.0.0.1:8765/` in the browser.

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
python -m app.credentials test --model qwen-mt-plus
```

Translation keeps batch checkpoints, exact ID validation and bounded retry behavior. Checkpoints are bound to provider + Base URL + model.
