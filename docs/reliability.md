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

## Quick Scan source and remote-index drift

A package or index can change after preflight but before project creation. Reusing the earlier counts in that situation creates a time-of-check/time-of-use (TOCTOU) mismatch. Character-name filtering also fails when filenames use an English identifier while the remote workbook's character column is localized.

**Project response**

- Archive inputs receive a content SHA-256; directory inputs receive a deterministic tree SHA-256 over relative paths, sizes, and per-file hashes.
- Chinese sources and selected local CSV indexes are fingerprinted as well, then checked again before the generated project index is written.
- Remote fallback queries exact WAV basenames instead of relying on a localized character-name match.
- A remote result must cover every WAV, contain non-empty English text, have no duplicate filename, and have one dominant character label covering at least 75% of matches. The threshold permits story-specific aliases without accepting a genuinely mixed-character package.
- The selected remote rows preserve workbook order and receive their own fingerprint. A changed remote result forces a new scan.
- Remote slices are cached for 24 hours; a failed refresh may use a cache no older than seven days.

## Translation failures and rate limits

Network/API translation can fail after some batches have already completed, for example because of transient transport failures, server errors, rate limits, or the local process being interrupted. Re-running the whole character would waste time and paid requests.

**Project response**

- The OpenAI SDK client is configured with finite retries and a request timeout.
- Translation IDs must round-trip exactly and returned Chinese text must be non-empty.
- After every successful batch, a local `.translation_checkpoint.json` is atomically updated.
- A checkpoint row is reused only when provider, Base URL and model match and the SHA-256 of the source English text still matches.
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

The current OpenAI Python SDK depends on `jiter`, a Rust-backed package. On Termux/Python 3.13 there are public reports of pip falling back to source builds, invoking `maturin`, and failing on the Android Rust target.

The API itself does not require that SDK. The Responses API is an HTTPS endpoint and Structured Outputs are supported directly through REST.

**Project response**

- v0.6 removes the OpenAI Python SDK from the normal dependency set.
- Desktop and Termux now share one dependency-free Responses REST client implemented with Python's standard library.
- The client defaults to `https://api.openai.com/v1/responses` but v0.7 can target a configured OpenAI-compatible HTTPS Base URL such as V-API. Loopback HTTP is allowed for local providers.
- Structured Outputs use `text.format.type = json_schema`.
- Temporary 408/409/429/5xx and network errors are retried with bounded exponential backoff and `Retry-After` support.
- 401/403 and other non-transient HTTP failures are not blindly retried.
- Translation batches remain checkpointed; successful earlier batches are reused after a later failure.
- Returned translation IDs must exactly match the requested IDs and Chinese text must be non-empty before it is accepted.
- API keys are not stored in project JSON or browser UI. v0.7 can store a provider key in the local user state directory with best-effort owner-only permissions, or read it from environment variables.

Third-party API relays add a separate trust and availability boundary. A provider advertising OpenAI-compatible endpoints does not by itself prove that every model route has the same upstream provenance or feature completeness as the official vendor endpoint.

**v0.7 response**

- Provider and Base URL are explicit runtime identity.
- Switching provider/Base URL invalidates translation checkpoint reuse for that route.
- A tiny structured-output smoke-test command is available before paid batch translation.
- Only non-sensitive game dialogue should be sent through untrusted relays unless the operator has separately assessed their privacy and contractual terms.

References:

- https://gpt.ge/
- https://gpt.ge/en/models/gpt-5.6-luna
- https://developers.openai.com/api/reference/resources/responses/methods/create
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://developers.openai.com/api/docs/models/gpt-5.6-luna
- https://github.com/openai/openai-python/blob/main/pyproject.toml
- https://github.com/openai/openai-python/issues/2102

## FastAPI / Pydantic v2 on Termux

The previous Termux launcher still installed FastAPI. Modern FastAPI depends on Pydantic v2, which depends on the Rust-backed `pydantic-core`. On Termux/Python 3.13, pip commonly has no official Android wheel and falls back to a maturin source build. Current Termux bug reports show the same failure seen here: `aarch64-unknown-linux-android` is rejected by rustup during the build bootstrap.

There are workarounds in the ecosystem: installing Termux Rust/binutils and compiling locally, or consuming third-party Android wheels. Both add substantial install time/complexity, and the latter also adds a separate binary supply-chain trust decision.

**Project response**

- Termux no longer installs FastAPI, Uvicorn, python-multipart, Pydantic or pydantic-core.
- Android uses a project-owned HTTP server built only on Python's standard library.
- The existing browser UI and API surface are preserved.
- Desktop builds keep FastAPI/Uvicorn.
- This removes the Rust compiler from the normal Android startup path instead of asking users to compile Pydantic on-device.
- Third-party prebuilt pydantic-core wheels are not used by default.

References:

- https://github.com/termux/termux-packages/issues/29818
- https://github.com/pydantic/pydantic-core/issues/1474
- https://github.com/fastapi/fastapi/discussions/6544
- https://github.com/Eutalix/android-pydantic-core
