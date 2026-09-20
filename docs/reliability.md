# Failure-driven reliability notes

This project intentionally treats public issue reports, security advisories, and upstream documentation as design input rather than waiting for the same failure to appear locally.

## Hosted web UI -> localhost

A public HTTPS page that actively `fetch()`es a localhost or LAN HTTP service is not a stable cross-browser control channel. Chromium's Local Network Access / Private Network Access work has changed permission and CORS behavior over time, and real projects have reported hosted frontends getting stuck while trying to connect to local runtimes.

**Project response**

- GitHub Pages is a static launcher/documentation site.
- It does not make successful cross-origin localhost fetches a prerequisite for operation.
- The "Enter local console" action performs a top-level navigation to `http://127.0.0.1:8765/`.
- LAN control uses the local backend URL printed by the launcher and a temporary token.

References:

- https://developer.chrome.com/blog/local-network-access
- https://github.com/WICG/local-network-access/issues/60
- https://github.com/abhigyanpatwari/GitNexus/issues/686
- https://github.com/googlecolab/colabtools/issues/5506
- https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site

## Large continuous WAV intermediates

Classic RIFF/WAVE uses 32-bit chunk sizes. Once a temporary WAV grows past roughly 4 GiB, the regular RIFF size fields can no longer represent it correctly. FFmpeg supports RF64 for large WAV files, but a temporary uncompressed file also doubles I/O and disk requirements.

**Project response**

v0.4 no longer builds a temporary continuous WAV. Raw PCM is streamed directly from the ordered source WAVs into FFmpeg's FLAC encoder. The streamed PCM is hashed as it is written; the final FLAC is decoded once and hashed again before the file is atomically promoted from `.partial.flac` to `continuous.flac`.

Reference:

- https://ffmpeg.org/ffmpeg-formats.html#WAV

## Archive extraction

Archive extraction has two recurring failure classes: path traversal / symlink escapes and resource exhaustion through decompression bombs. Python's `zipfile` documentation explicitly warns against extracting untrusted archives without inspection. py7zr versions through 1.1.2 were affected by 2026 advisories including arbitrary file writes and decompression-bomb denial of service; 1.1.3 added hardening and `max_extract_size`.

**Project response**

- Require `py7zr>=1.1.3`.
- Reject suspicious absolute / traversal member paths before ZIP or 7z extraction.
- Reject archive symlinks.
- Cap archive member count.
- Cap declared uncompressed extraction size (32 GiB by default, configurable with `HSR_MAX_EXTRACT_BYTES`).
- Pass `max_extract_size` to py7zr.

References:

- https://docs.python.org/3/library/zipfile.html
- https://github.com/miurahr/py7zr/security/advisories/GHSA-q6rc-2cgv-63h7
- https://py7zr.readthedocs.io/en/stable/Changelog.html

## Long-running local jobs

Heavy background work can outlive an HTTP request. It can also be interrupted when the local Python process exits. FastAPI's documentation distinguishes simple in-process background work from more durable multi-process job systems such as Celery.

**Project response**

This application remains intentionally single-host and single-process; adding Redis/Celery would conflict with the lightweight local-first goal. Instead:

- long builds and update checks run outside the request;
- job metadata is journaled to the user's local state directory;
- a job that was queued/running when the process stopped is shown as `interrupted` after restart;
- an interrupted build is not reported as successful;
- final FLAC and primary metadata files use atomic replacement where practical.

The journal does **not** imply resumable FFmpeg encoding. Interrupted jobs are detected, not magically resumed.

Reference:

- https://fastapi.tiangolo.com/tutorial/background-tasks/

## Multi-worker servers

Project state and job execution are local process state. Running this app behind multiple Uvicorn workers would make active-project/job state ambiguous without an external coordinator.

**Project response**

The supported launchers use one Uvicorn process. Multi-worker deployment is outside the supported architecture. The tool is a local application with a browser UI, not a public multi-tenant web service.


## Localhost APIs are not automatically private

Binding a service to `127.0.0.1` prevents direct network access from another machine, but a malicious website open in the user's browser can still attempt requests to localhost. Blind CSRF against state-changing local APIs and DNS rebinding are recurring failure modes in local web-control applications. Host-header validation is also a standard mitigation for rebinding/Host attacks.

**Project response**

- A random per-process control token is generated even in local-only mode.
- Every `/api/*` request and the legacy `/build` endpoint requires the token in `X-HSR-Token`.
- The dashboard receives the token from the local server; it is not stored in GitHub Pages or project files.
- LAN mode additionally gates the dashboard entry itself with the generated token.
- Unexpected `Host` values are rejected.
- Non-loopback binds require explicit `--lan`.
- The dashboard uses `Cache-Control: no-store`, a restrictive CSP, `Referrer-Policy: no-referrer`, and `X-Content-Type-Options: nosniff`.

References:

- https://fastapi.tiangolo.com/tutorial/cors/
- https://fastapi.tiangolo.com/advanced/middleware/#trustedhostmiddleware
- https://github.com/openclaw/openclaw/issues/12032
- https://github.com/open-mercato/open-mercato/issues/253

## WAVE_FORMAT_EXTENSIBLE and Python-version mismatch

Python 3.11's standard `wave` module only supports `WAVE_FORMAT_PCM`; it explicitly does not include `WAVE_FORMAT_EXTENSIBLE` even when the subformat is PCM. Python 3.12 added support for PCM `WAVE_FORMAT_EXTENSIBLE`. This can produce the classic `unknown format: 65534` failure on one supported Python version while the same WAV works on another.

**Project response**

v0.5 uses a small project-owned RIFF/RF64 PCM reader instead of relying on the standard-library `wave` parser for input. It accepts ordinary PCM and PCM `WAVE_FORMAT_EXTENSIBLE`, validates alignment and format metadata, and streams only the declared PCM data chunk into the FLAC pipeline. Regression tests construct a real extensible PCM WAV and run it through FFmpeg on Python 3.11 and 3.12 CI.

References:

- https://docs.python.org/3.11/library/wave.html
- https://docs.python.org/3.12/library/wave.html

## Remote XLSX files are compressed XML containers

A remote XLSX is both a download and a ZIP/XML input. Content-Length may be absent, and a small compressed workbook can expand substantially. openpyxl's documentation warns that XML entity-expansion attacks require additional protection such as `defusedxml`.

**Project response**

- Remote index URLs must remain HTTPS even after redirects.
- The downloader enforces an actual streamed-byte limit rather than trusting `Content-Length`.
- The XLSX ZIP container is checked for member count, total declared uncompressed size, and required workbook structure before openpyxl sees it.
- `defusedxml` is an explicit dependency.
- openpyxl continues to run in read-only/data-only mode.

References:

- https://docs.python.org/3/library/urllib.request.html
- https://openpyxl.readthedocs.io/en/stable/

## Translation failures and rate limits

Network/API translation can fail after some batches have already completed, for example because of transient transport failures, server errors, rate limits, or the local process being interrupted. Re-running the whole character would waste time and paid requests.

**Project response**

- The OpenAI SDK client is configured with finite retries and a request timeout.
- Translation IDs must round-trip exactly and returned Chinese text must be non-empty.
- After every successful batch, a local `.translation_checkpoint.json` is atomically updated.
- A checkpoint row is reused only when the model matches and the SHA-256 of the source English text still matches.
- A later failed batch therefore does not discard earlier successful batches.
- The checkpoint is ignored by Git and remains local.

References:

- https://platform.openai.com/docs/guides/error-codes
- https://platform.openai.com/docs/guides/rate-limits

## Android / Termux process killing

Long CPU-heavy work on Android is not equivalent to a desktop background service. The Termux project warns that Android 12+ can terminate phantom processes or processes using excessive CPU, and 2026 reports show long-running Termux work can still be killed on some Android 15 devices even with common keep-alive measures.

**Project response**

- Android/Termux is treated as a supported but interruptible host.
- The runtime preflight surfaces a Termux warning in the dashboard.
- Job state is journaled; a killed process does not later appear as a successful build.
- Metadata and translation batches are checkpointed/atomically written.
- FLAC output is only promoted after complete PCM verification; an interrupted `.partial.flac` is never treated as final.
- FLAC encoding itself is not resumable and restarts after interruption. The project does not claim otherwise.
- Large extraction/work directories are placed beside the selected output location instead of relying on a potentially smaller system temp partition.

References:

- https://github.com/termux/termux-app/blob/master/README.md
- https://github.com/termux/termux-app/issues/5150

## Launcher and network-interface failures

A fixed port may already be occupied, and a machine with VPNs, virtual adapters, Ethernet plus Wi-Fi, or no default internet route may not have a single obvious LAN address.

**Project response**

- The launcher checks the requested port before starting Uvicorn and reports a clear conflict.
- LAN mode gathers multiple IPv4 candidates instead of assuming a single UDP-route answer.
- `--display-host <IP>` lets the user override the printed phone/tablet URL on multi-interface or offline LAN hosts.
- Non-loopback binding is refused unless LAN mode is explicitly enabled.


## OpenAI Python SDK on Termux / Android

The current OpenAI Python SDK depends on `jiter`, a Rust-backed package. On Termux/Python 3.13 there are public reports of pip falling back to source builds, invoking `maturin`, and failing because the Android Rust target is not supported by the bootstrap path. The official SDK still lists `jiter` as a required dependency.

**Project response**

- The Termux base requirements do not install the OpenAI Python SDK.
- Core archive, update-check, subtitle and FLAC functions therefore start without pulling the Android-incompatible Rust dependency chain.
- Desktop installs keep the official SDK.
- If GPT fallback translation is enabled on a Termux runtime without the SDK, the UI/build fails early with a clear capability message instead of failing during package installation.
- This is a compatibility fallback, not a claim that the OpenAI API itself is unsupported on Android.

References:

- https://github.com/openai/openai-python/blob/main/pyproject.toml
- https://github.com/NousResearch/hermes-agent/issues/26891
- https://github.com/openai/openai-python/issues/2102
