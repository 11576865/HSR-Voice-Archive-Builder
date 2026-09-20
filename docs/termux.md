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
- no OpenAI Python SDK in the Android base install; translation calls the official Responses HTTPS API directly with Python's standard library.

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

GPT fallback translation also works without the OpenAI Python SDK. The project sends HTTPS requests directly to the official Responses API with Python's standard library. Configure the API key in the Termux environment before starting the local engine:

```bash
export OPENAI_API_KEY="..."
bash run_termux.sh
```

The key is not stored in the browser UI or project JSON. Translation keeps the existing batch checkpoint, ID validation and retry behavior.
