"""Playwright -> real FastAPI -> actual FFmpeg/libass -> decoded browser proof.

Only unrelated project/corpus APIs are fixture backed. The render-preview-video
request MUST pass through the actual localhost server (not a mocked response).
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Allow direct execution as "python tests/browser_live_preview_e2e.py".
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from playwright.sync_api import sync_playwright

from app.server import app
from tests.browser_visual_evidence import (
    SUBTITLES, ensure, make_router, screenshot,
)


def make_source(ffmpeg: str, target: Path) -> bytes:
    subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=523:sample_rate=48000:duration=5",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
        "-b:v", "500k", "-c:a", "libopus", "-b:a", "64k",
        "-shortest", str(target),
    ], check=True, capture_output=True, timeout=45)
    media = target.read_bytes()
    ensure(len(media) > 1024 and len(media) <= 24 * 1024 * 1024,
           "Fixture must be a real bounded local movie")
    return media


def probe(ffprobe: str, target: Path) -> dict:
    result = subprocess.run([
        ffprobe, "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,start_time",
        "-of", "json", str(target),
    ], check=False, capture_output=True, text=True, timeout=15)
    if result.returncode:
        prefix = target.read_bytes()[:60]
        raise AssertionError(
            f"ffprobe cannot decode API response ({target.stat().st_size} bytes, "
            f"first bytes={prefix!r}): {result.stderr[:1400]}"
        )
    return json.loads(result.stdout)


def wait_for_live_state(page, evaluate_fn: str, label: str,
                        timeout_seconds: float = 30.0) -> None:
    """Use page.evaluate callable, not wait_for_function's CSP-blocked eval."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.evaluate(evaluate_fn):
            return
        page.wait_for_timeout(100)
    raise AssertionError("Timed out waiting for " + label)


def load_studio_under_production_csp(page) -> None:
    wait_for_live_state(
        page, "() => typeof showWorkspace === 'function' && typeof loadLayoutStressSample === 'function'",
        "workbench boot",
    )
    page.evaluate("""() => {
        currentProject = {name:'visual-fixture',root:'/qa/fixture',config:{
            source_text_language:'en',target_language:'zh-CN',
            subtitle_chs_font:'Arial',subtitle_primary_font:'Arial'
        }};
        showWorkspace('layout',{scroll:false});
    }""")
    # The real browser may have queued the initial requestAnimationFrame before
    # fixture project setup; explicitly await the production corpus loader to
    # obtain a deterministic sample before testing genuine video bytes.
    page.evaluate("() => loadLayoutStressSample({force:true})")
    try:
        page.locator('[data-timeline-cue]').nth(2).wait_for(state="attached", timeout=20000)
    except Exception as exc:
        state = page.evaluate("""() => ({
          project: currentProject?.root,
          studio: document.body.dataset.workspaceTask,
          rows: layoutCorpusItems?.length,
          selected: layoutCorpusSelectedId,
          status: document.getElementById('layoutCorpusCheckStatus')?.textContent
        })""")
        raise AssertionError(f"Production corpus did not initialize: {state}") from exc
    page.locator("#studioEffectsTab").click()
    ensure(page.locator("#previewVideoBtn").is_visible(), "Effect preview hidden")


def run(output: Path) -> None:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("Real browser E2E requires FFmpeg + FFprobe")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hsr-live-preview-") as td:
        root = Path(td)
        fixture = make_source(ffmpeg, root / "source.webm")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                access_log=False, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(150):
                if server.started:
                    break
                if not thread.is_alive():
                    raise AssertionError("ASGI server exited before startup")
                time.sleep(0.1)
            ensure(server.started, "ASGI server failed to start")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True,
                                                      args=["--no-sandbox"])
                try:
                    page = browser.new_page(viewport={"width": 1600, "height": 900},
                                            device_scale_factor=1)
                    errors = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    mock = make_router([dict(row) for row in SUBTITLES], [])
                    seen_real = []
                    captured_media = []
                    def route_api(route):
                        if route.request.url.split("?", 1)[0].endswith(
                            "/api/subtitle-layout/render-preview-video"
                        ):
                            seen_real.append(route.request.post_data_json)
                            # Playwright's route.continue_() does not reliably
                            # expose the response body through Response.body().
                            # Fetch from the actual FastAPI origin, record its
                            # real bytes, then deliver the same response to
                            # Chromium. Never fabricate video response bytes.
                            upstream = route.fetch(timeout=100000)
                            payload = upstream.body()
                            captured_media.append({
                                "body": payload, "status": upstream.status,
                                "headers": upstream.headers,
                            })
                            route.fulfill(response=upstream, body=payload)
                        else:
                            mock(route)
                    page.route("**/api/**", route_api)
                    page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                    load_studio_under_production_csp(page)
                    page.locator("#effectFootageFile").set_input_files({
                        "name": "recorded-source.webm", "mimeType": "video/webm",
                        "buffer": fixture,
                    })
                    page.locator("#effectFootageStart").fill("0.3")
                    page.locator("#effectFootageAudio").check()
                    with page.expect_response(
                        lambda response: response.url.endswith(
                            "/api/subtitle-layout/render-preview-video"),
                        timeout=100000,
                    ) as api_request:
                        page.locator("#previewVideoBtn").click()
                    response = api_request.value
                    if response.status != 200:
                        raise AssertionError(
                            "Live preview rejected: " + str(response.status) + " " +
                            response.text()[:1200]
                        )
                    ensure(response.headers.get("x-hsr-preview-background") == "local-footage",
                           "Actual API did not confirm local source input")
                    ensure(seen_real and seen_real[0].get("include_source_audio") is True,
                           "Browser failed to submit requested source audio")
                    ensure("source_video_base64" in seen_real[0],
                           "Browser did not upload actual source bytes")
                    output_movie = root / "live-output.webm"
                    ensure(captured_media, "No actual FastAPI response was captured")
                    raw_response = captured_media[0]["body"]
                    ensure(captured_media[0]["status"] == 200,
                           "Actual FastAPI response was not successful")
                    ensure(response.headers.get("content-type", "").startswith("video/webm"),
                           "Real preview returned unexpected Content-Type: " +
                           str(response.headers.get("content-type")))
                    ensure(raw_response.startswith(b"\x1a\x45\xdf\xa3"),
                           "API returned non-WebM bytes: " + repr(raw_response[:100]))
                    output_movie.write_bytes(raw_response)
                    metadata = probe(ffprobe, output_movie)
                    streams = metadata.get("streams", [])
                    video = next((item for item in streams
                                  if item["codec_type"] == "video"), None)
                    audio = next((item for item in streams
                                  if item["codec_type"] == "audio"), None)
                    ensure(video is not None and video.get("codec_name") == "vp8",
                           "Live render did not produce VP8 video")
                    ensure(audio is not None and audio.get("codec_name") == "opus",
                           "Live render did not contain source audio as Opus")
                    ensure((video["width"], video["height"]) == (960, 540),
                           "Unexpected composited video dimensions")
                    start_delta = abs(float(video.get("start_time") or 0.0) -
                                      float(audio.get("start_time") or 0.0))
                    ensure(start_delta < 0.18,
                           "Source audio/video start timestamps drift: " + str(start_delta))
                    raw_audio = subprocess.run([
                        ffmpeg, "-v", "error", "-i", str(output_movie),
                        "-map", "0:a:0", "-t", "1",
                        "-f", "s16le", "-ac", "1", "-ar", "8000", "-",
                    ], check=True, capture_output=True, timeout=15).stdout
                    ensure(len(raw_audio) > 1000 and any(raw_audio),
                           "The output audio track did not decode to audible samples")
                    wait_for_live_state(page, """() => {
                        const player=document.getElementById('libassVideoPreview');
                        const proof=document.querySelector('.layout-preview-evidence');
                        return player && player.readyState>=2 &&
                          proof?.dataset.previewEvidence==='video-footage';
                    }""", "decoded ASS-on-source video", 45.0)
                    player = page.locator("#libassVideoPreview")
                    ensure(player.is_visible(), "Live composited clip not visible")
                    player.evaluate("(video)=>{video.pause(); video.currentTime=0.7;}")
                    wait_for_live_state(page, """() =>
                        document.getElementById('libassVideoPreview').currentTime >= 0.65
                    """, "video seeking", 15.0)
                    screenshot(page, output, "live_ffmpeg_libass_source_audio")
                    ensure(not errors, "Browser runtime errors: " + " / ".join(errors))
                    report = {
                        "type": "live_asgi_ffmpeg_libass_browser_e2e",
                        "api_mocked": False,
                        "preview_route_capture": "Playwright route.fetch upstream to real FastAPI",
                        "other_project_apis_mocked": True,
                        "input_bytes": len(fixture),
                        "output_bytes": output_movie.stat().st_size,
                        "input_start_seconds": 0.3,
                        "video_codec": video["codec_name"],
                        "audio_codec": audio["codec_name"],
                        "decoded_by_chromium": True,
                        "selected_cue_playback": True,
                        "device_evidence": False,
                    }
                    (output / "live_preview_evidence.json").write_text(
                        json.dumps(report, indent=2, ensure_ascii=False),
                        encoding="utf-8")
                    print("LIVE HSR PREVIEW E2E SUCCESS", json.dumps(report))
                    page.close()
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/subtitle-live-preview"))
    opts = parser.parse_args()
    run(opts.output)


if __name__ == "__main__":
    main()
