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
