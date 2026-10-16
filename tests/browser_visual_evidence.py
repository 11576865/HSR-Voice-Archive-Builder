"""Chromium visual/interaction evidence for shipped HSR subtitle studio.

The actual app/static/index.html is served from this checkout, with deterministic
API fixtures. Screenshots are real Chromium pixels, but NOT live project/libass
render evidence or phone-device screenshots.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import os
import subprocess
import tempfile
from pathlib import Path
import threading
from urllib.parse import unquote, urlsplit
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SUBTITLES = [
    {
        "id": 11, "source_text": "The stars are still beyond our reach.",
        "final_chs": "群星仍在我们触不可及之处。",
        "official_chs": "群星仍在我们触不可及之处。",
        "source_start": 5.0, "source_end": 7.0,
        "start": 5.0, "end": 7.0, "group": "Story / Main", "filename": "line-11.wav",
    },
    {
        "id": 12, "source_text": "And yet, we continue our journey.",
        "final_chs": "但旅途并未因此停下。",
        "official_chs": "但旅途并未因此停下。",
        "source_start": 7.4, "source_end": 10.0,
        "start": 7.4, "end": 10.0, "group": "Story / Main", "filename": "line-12.wav",
    },
    {
        "id": 13, "source_text": "Rules are made to be broken.",
        "final_chs": "规则，就是用来打破的！",
        "official_chs": "规则，就是用来打破的！",
        "source_start": 11.0, "source_end": 14.0,
        "start": 11.0, "end": 14.0, "group": "Story / Main", "filename": "line-13.wav",
    },
    {
        "id": 14, "source_text": "A new day begins.",
        "final_chs": "新的一天开始了。",
        "official_chs": "新的一天开始了。",
        "source_start": 87.0, "source_end": 90.0,
        "start": 87.0, "end": 90.0, "group": "Chapter Two", "filename": "line-14.wav",
    },
]


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        # Mirror both production servers' /static mount. The old fixture
        # special-cased just the stylesheet, accidentally returning HTTP 404
        # for the newly bundled diagnostic WebP image.
        path = unquote(urlsplit(self.path).path)
        if path.startswith("/static/"):
            static_root = (ROOT / "app" / "static").resolve()
            requested = (static_root / path[len("/static/"):]).resolve()
            if not requested.is_relative_to(static_root) or not requested.is_file():
                self.send_error(404, "Static resource does not exist")
                return
            media_type = mimetypes.guess_type(str(requested))[0] or "application/octet-stream"
            payload = requested.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/app/static/index.html"):
            # Match the production FastAPI template injection; serving raw
            # index.html would leave a bare JS sentinel and abort startup.
            template = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
            payload = template.replace("__HSR_API_TOKEN_JSON__", json.dumps(""))
            body = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def geometry(payload: dict) -> dict:
    english = str(payload.get("english_text") or "")
    chinese = str(payload.get("chinese_text") or "")
    def line(text: str, top: int, size: int) -> dict:
        return {
            "text": text, "x": 960, "font_size": size,
            "bbox": {"x_min": 270, "y_min": top, "x_max": 1650, "y_max": top + size * 1.4},
        }
    return {
        "ok": True,
        "safe_area": {
            "x_min": 70, "y_min": 65, "max_printable_width": 1780, "max_printable_height": 950,
        },
        "central_gap": {"y_top": 524, "y_bottom": 557, "min_central_gap": 20},
        "layout": {
            "failed": False, "scale_percent": 100,
            "primary_lines": [line(english, 440, 39)] if english else [],
            "chs_lines": [line(chinese, 578, 44)] if chinese else [],
        },
        "parallax": None,
    }


def real_browser_video_fixture() -> bytes:
    """Real browser-decodable WebM with audible audio, not a silent stub.

    Browser evidence checks interactive playback. Actual project PCM source
    and exact voice duration are independently tested by the FFmpeg pipeline.
    """
    with tempfile.TemporaryDirectory(prefix="hsr-browser-video-") as td:
        path = Path(td) / "motion.webm"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3.5",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3.5",
             "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
             "-pix_fmt", "yuv420p", "-c:a", "libopus", "-b:a", "96k",
             "-t", "3.5", str(path)],
            capture_output=True, text=True, timeout=50, check=True,
        )
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15, check=True,
        )
        kinds = {row.get("codec_type") for row in json.loads(probe.stdout)["streams"]}
        assert {"video", "audio"} <= kinds, "Browser fixture must carry audible audio"
        return path.read_bytes()


def make_router(rows: list[dict], writes: list[dict], video_bytes: bytes | None = None,
                *, project_root: str = "/qa/fixture"):
    def handler(route):
        request = route.request
        url = request.url
        path = url.split("?", 1)[0]
        body = request.post_data_json if request.method == "POST" and request.post_data else {}
        if path.endswith("/api/subtitle-layout/render-preview-video") and video_bytes:
            assert body.get("preview_format") == "webm", "Chromium must negotiate VP8"
            has_source = "source_video_base64" in body
            has_image = "preview_background_image_base64" in body
            has_reference = body.get("use_studio_reference_background") is True
            assert not (has_source and has_image), "Background input sources must be exclusive"
            if has_reference:
                assert not has_source and not has_image, "Bundled image must yield to selected user media"
            if has_source:
                assert base64.b64decode(body["source_video_base64"], validate=True) == video_bytes
                assert body.get("source_start_seconds") == 0.2
            if has_image:
                assert base64.b64decode(body["preview_background_image_base64"], validate=True).startswith(
                    b"\x89PNG\r\n\x1a\n")
            # Browser test proves decoded playback and UI provenance; separate
            # real-FFmpeg test proves the output pixels include actual ASS.
            assert body.get("project_voice_entry_id") is not None, (
                "Effects preview must select a project cue, not an arbitrary silent sample"
            )
            route.fulfill(status=200, content_type="video/webm", body=video_bytes,
                          headers={
                              "X-HSR-Preview-Audio": "project",
                              "X-HSR-Preview-Duration": "3.500",
                              "X-HSR-Preview-Renderer": "ffmpeg-libass-webm",
                              "X-HSR-Preview-Background":
                              "local-footage" if has_source else
                              "background-image" if has_image or has_reference else "black",
                          })
            return
        if path.endswith("/api/status"):
            payload = {
                "ok": True, "version": "visual-fixture", "project": {
                    "root": project_root, "name": "visual-fixture",
                    "config": {
                        "source_text_language": "en", "target_language": "zh-CN",
                        "subtitle_chs_font": "Arial", "subtitle_primary_font": "Arial",
                        "delivery_mode": "complete",
                    }, "report": {}, "outputs": {},
                },
                "runtime": {"ffmpeg": False, "warnings": []},
                "recent_projects": [], "jobs": [],
            }
        elif path.endswith("/api/quick/candidates"):
            payload = {"ok": True, "candidates": []}
        elif path.endswith("/api/subtitle-layout/settings"):
            # Mirror production's project-scoped save receipt. A generic
            # {"ok":true} clears currentProject in the actual JS autosaver.
            assert request.method == "POST", "Settings must not be written via GET"
            assert body.get("expected_project_root") == project_root, (
                "Settings must be bound to the active project"
            )
            payload = {"ok": True, "project": {
                "root": project_root, "name": "visual-fixture",
                "config": {
                    "source_text_language": "en", "target_language": "zh-CN",
                    "subtitle_chs_font": body.get("chs_font") or "Arial",
                    "subtitle_primary_font": body.get("primary_font") or "Arial",
                    "subtitle_chs_size": body.get("base_chs_size", 48),
                    "subtitle_primary_size": body.get("base_primary_size", 42),
                    "subtitle_primary_color": body.get("primary_color", "#DCEBFF"),
                    "subtitle_chs_color": body.get("chs_color", "#FFFFFF"),
                    "delivery_mode": "complete",
                }, "report": {}, "outputs": {},
            }}
        elif "/subtitles?" in url or path.endswith("/subtitles"):
            payload = {
                "ok": True, "subtitles": rows, "persistable": True,
                "export_status": {
                    "state": "current", "artifacts_current": True,
                    "ass_required": True, "artifact_error": "",
                },
            }
        elif path.endswith("/subtitles/artifacts/refresh"):
            payload = {
                "ok": True, "artifacts_current": True, "artifact_error": "",
                "refreshed": {"ass_error": ""},
                "export_status": {
                    "state": "current", "artifacts_current": True,
                    "ass_required": True, "artifact_error": "",
                },
            }
        elif path.endswith("/api/subtitle-layout/preview"):
            payload = geometry(body or {})
        elif path.endswith("/timing"):
            cue_id = path.split("/subtitles/")[1].split("/timing")[0]
            match = next((x for x in rows if str(x["id"]) == cue_id), None)
            assert match is not None, f"unknown cue {cue_id}"
            assert body.get("expected_project_root") == project_root, "project guard missing"
            if (body.get("expected_start"), body.get("expected_end")) != (match["start"], match["end"]):
                route.fulfill(status=409, content_type="application/json", body=json.dumps({
                    "ok": False, "error": "Stale cue timing"
                }))
                return
            new_start = match["source_start"] if body.get("reset") else body["start"]
            new_end = match["source_end"] if body.get("reset") else body["end"]
            assert new_end - new_start >= 0.1
            writes.append({"id": cue_id, "start": new_start, "end": new_end})
            match["start"], match["end"] = new_start, new_end
            payload = {"ok": True, "timing": {
                "id": cue_id, "start": new_start, "end": new_end,
                "timing_modified": new_start != match["source_start"] or new_end != match["source_end"],
            }, "refreshed": {"ass_error": ""}}
        elif path.endswith("/api/subtitle-layout/word-alignments"):
            payload = {"ok": True, "diagnostics": {}}
        elif "/api/" in path:
            payload = {"ok": True}
        else:
            route.continue_()
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))
    return handler


def ensure(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)


def rect(page, selector: str):
    data = page.locator(selector).bounding_box()
    assert data, f"{selector} missing or not visible"
    return data


def load_studio(page):
    page.wait_for_function("typeof showWorkspace === 'function' && typeof loadLayoutStressSample === 'function'")
    # /api/status, not a manual global assignment, must own the same project
    # identity. Otherwise a late refresh() clears the selected cue mid-test.
    page.wait_for_function("currentProject?.root === '/qa/fixture'", timeout=12000)
    page.evaluate("() => showWorkspace('layout',{scroll:false})")
    page.wait_for_function("layoutCorpusItems.length >= 3", timeout=12000)
    page.wait_for_function("document.getElementById('layoutCueHeading').textContent.includes('字幕')", timeout=12000)
    # A separate effects workbench owns timing, Karaoke and playable preview.
    # The style view intentionally hides those controls.
    ensure(page.locator('#studioStyleTab').is_visible(), "Focused Layout task missing")
    ensure(page.locator('#studioEffectsTab').is_visible(), "Focused Effects task missing")
    ensure(not page.locator("#layoutTimelineViewport").is_visible(), "Style shows effect timeline")
    subtitleRequests = []
    page.on("request", lambda request: subtitleRequests.append(request.url)
            if "/subtitles?selector=all" in request.url else None)
    page.locator('#studioEffectsTab').click()
    page.wait_for_timeout(150)
    ensure(not subtitleRequests, "Layout -> Effects caused a second subtitle corpus fetch")
    ensure(page.locator("#studioEffectsTab").get_attribute("aria-current") == "page",
           "Effects is not a primary destination")
    ensure(page.locator("#studioStyleTab").get_attribute("aria-current") is None,
           "Layout and Effects both selected")
    ensure(not page.locator("#layoutTimelineViewport").is_visible() and
           not page.locator("#studioV4Timing").evaluate("(node)=>node.open"),
           "Effects should start with the full image, not an expanded timeline")
    ensure(page.locator("#previewVideoBtn").is_visible(), "Playable effect preview action hidden")
    ensure(page.locator("#layoutCueFinalText").count() == 0,
           "Duplicate final text editor still exists in DOM")
    ensure(page.locator("#layoutCueAudio").count() == 0,
           "Duplicated original WAV control remains in Effects")
    ensure(page.locator("audio").count() == 1, "More than one original audio player")
    page.evaluate("() => window.scrollTo(0, 0)")


def screenshot(page, output: Path, name: str):
    page.screenshot(path=str(output / f"{name}.png"), full_page=True, animations="disabled")


def desktop_check(browser, base_url, output, report):
    page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
    rows = [dict(x) for x in SUBTITLES]
    writes = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    fixture_video=real_browser_video_fixture()
    page.route("**/api/**", make_router(rows, writes, fixture_video))
    page.goto(base_url, wait_until="domcontentloaded")
    load_studio(page)
    # Default work area must be two-column and keep the duplicate list closed.
    ensure(page.locator("#layoutAuditDrawerToggle").is_visible(),
           "Effects shared cue selector is missing")
    ensure(page.locator("#layoutCorpusRail").is_hidden(), "Second full subtitle explorer still dominates UI")
    ensure(page.locator("#layoutTrialTextDetails").get_attribute("open") is None,
           "Manual text authoring should not be a second visible editor")
    ensure(page.locator('#studioEffectsTab').count() == 1 and
           page.locator('#studioStyleTab').count() == 1,
           "Layout and Effects must have one primary navigation entry each")
    ensure(page.locator("#layoutCueFinalText").count() == 0,
           "Duplicated final text textarea is still mounted")
    visual=page.evaluate("""() => {
      const root=document.getElementById('subtitleLayoutPreviewCard');
      const stage=root.querySelector('.preview-stage');
      const cmd=root.querySelector('.studio-v3-command-bar');
      const nodes=['.studio-v4-workbench','.studio-v3-main',
                   '.studio-v3-visual','.studio-v3-editors',
                   '.studio-v3-inspector'].map(k=>root.querySelector(k));
      const box=x=>x.getBoundingClientRect();
      return {width:box(stage).width,height:box(stage).height,
        imageFirst:box(stage).bottom<=box(cmd).top+2,
        overflow:nodes.map(x=>getComputedStyle(x).overflowY)};
    }""")
    ensure(visual["height"]>260 and
           abs(visual["width"]/visual["height"]-16/9)<.045 and visual["imageFirst"],
           f"V4 screenshot must show complete 16:9 preview first: {visual}")
    ensure(all(v=="visible" for v in visual["overflow"]),
           f"Nested scrolling containers still present: {visual}")
    ensure(page.locator("#layoutTrialTextDetails").is_hidden(),
           "Second subtitle editor should not exist in visible workspace")
    ensure(page.locator("#previewBackgroundMode").is_hidden(),
           "Preview has only one fixed red-clouds background")
    # Word timestamp diagnostics live inside the one Effects Karaoke section.
    # Expand the user-visible owner before checking its nested controls.
    parent=page.locator('details.layout-inspector-section[data-studio-scope="effects"]').filter(
        has=page.locator('#hsrKaraokeReadiness'))
    ensure(parent.count()==1,"Word timing has lost its single Effects owner")
    parent.locator(":scope > summary").click()
    ensure(page.locator("#hsrKaraokeReadiness").is_visible(),
           "Karaoke word-alignment disclosure is unavailable")
    ensure(not page.locator("#hsrKaraokeReadiness").evaluate("(el)=>el.open"),
           "Karaoke diagnostics must not occupy the default Inspector")
    page.locator("#hsrKaraokeReadiness > summary").click()
    for action in ("#refreshWordAlignmentBtn","#importWordAlignmentBtn"):
        ensure(page.locator(action).is_visible(),"Missing visible Karaoke recovery: "+action)
    page.evaluate("() => loadWordAlignmentDiagnostics()")
    ensure("尚无可诊断" in page.locator("#hsrKaraokeReadinessDetail").inner_text(),
           "Karaoke readiness failed to explain missing project data")
    ensure(page.locator("#generateWordAlignmentBtn").is_disabled(),
           "WhisperX generation claimed availability in an unconfigured fixture")
    ensure(page.locator("#hsrEffectOperationStatus").get_attribute("data-phase")=="idle" and
           page.locator("#hsrEffectOperationStatus").is_hidden(),
           "Idle operation status should not take space away from the image")
    ensure(page.locator("#previewBackgroundMode").input_value()=="reference",
           "Studio did not default to user red-clouds reference")
    try:
        page.wait_for_function("""() => {
          const i=document.getElementById('previewBackgroundImage');
          return i && i.complete && i.naturalWidth===640 && i.naturalHeight===360;
        }""",timeout=10000)
    except Exception as exc:
        image_details = page.evaluate("""() => {
          const img=document.getElementById('previewBackgroundImage');
          return img && {
            src: img.currentSrc, complete: img.complete,
            naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight
          };
        }""")
        response = page.request.get(page.url.split("/app/static/index.html")[0] +
                                    "/static/assets/studio-red-clouds.webp")
        raise AssertionError(
            f"Reference WebP did not decode: {image_details}; HTTP {response.status}; "
            f"type={response.headers.get('content-type')}; bytes={len(response.body())}"
        ) from exc
    ensure(page.locator("#previewBackgroundImage").is_visible(),
           "Red-clouds image is missing from the actual stage")
    screenshot(page, output, "desktop_clean_effects_workspace")
    # The fixture must actually decode and display, and its provenance must
    # reflect the fixed red-clouds reference. "video" would mean plain color;
    # the production UI correctly calls this "video-image".
    page.locator("#previewVideoBtn").click()
    try:
        page.wait_for_function("""() => {
          const video=document.getElementById('libassVideoPreview');
          return video && video.readyState>=2 &&
            document.querySelector('.layout-preview-evidence').dataset.previewEvidence==='video-image';
        }""", timeout=20000)
    except Exception:
        details=page.evaluate("""() => {
          const v=document.getElementById('libassVideoPreview');
          return {readyState:v.readyState,networkState:v.networkState,error:v.error?.message,
            src:v.currentSrc,duration:v.duration,
            studio:document.getElementById('subtitleLayoutPreviewCard').dataset.studioMode,
            evidence:document.querySelector('.layout-preview-evidence').dataset.previewEvidence,
            status:document.getElementById('videoPreviewStatus').textContent,
            buttonDisabled:document.getElementById('previewVideoBtn').disabled,
            presentation:document.getElementById('libassVideoPreview').className};
        }""")
        print("CHROMIUM VIDEO DIAGNOSTIC",json.dumps(details,ensure_ascii=False),flush=True)
        raise
    ensure(page.locator("#libassVideoPreview").is_visible(), "Decoded video not visible")
    ensure(page.locator("#hsrEffectOperationStatus").get_attribute("data-phase")=="ready",
           "Video returned without decoder-confirmed visible success")
    ensure("可播放" in page.locator("#hsrEffectOperationTitle").inner_text(),
           "Stage shows no confirmed playback status")
    page.locator("#libassVideoPreview").evaluate("(video) => {video.muted=true;video.currentTime=0.7;}")
    page.wait_for_function("document.getElementById('libassVideoPreview').currentTime >= 0.65")
    screenshot(page, output, "desktop_effect_video_playback")
    # Optional media input lives in a deliberate disclosure. Keep the first
    # viewport for the active motion controls, not an entire file picker.
    ensure(not page.locator(".hsr-preview-media-disclosure").evaluate("(el) => el.open"),
           "Optional video upload settings should start collapsed")
    page.locator(".hsr-preview-media-disclosure > summary").click()
    ensure(page.locator("#effectBackgroundImageFile").count()==0,
           "Alternate studio image picker returned")
    ensure(page.locator("#effectPreviewBackgroundColor").count()==0,
           "Alternate studio color picker returned")
    page.locator("#prevEnableKinetic").uncheck()
    ensure(page.locator("#hsrEffectOperationStatus").get_attribute("data-phase")=="stale",
           "Effect changes retained false successful playback")
    page.locator("#previewVideoBtn").click()
    page.wait_for_function("""() => {
      const video=document.getElementById('libassVideoPreview');
      return video.readyState>=2 &&
        document.querySelector('.layout-preview-evidence')
          .dataset.previewEvidence==='video-image';
    }""",timeout=20000)
    screenshot(page, output, "desktop_red_cloud_effect_preview")
    # File picker must forward actual source bytes and update evidence identity.
    page.locator("#effectFootageFile").set_input_files({
        "name": "user-footage.webm", "mimeType": "video/webm", "buffer": fixture_video
    })
    page.locator("#effectFootageStart").fill("0.2")
    ensure(page.locator("#libassVideoPreview").is_hidden(), "Changing selected footage retained stale output")
    page.locator("#previewVideoBtn").click()
    page.wait_for_function("""() => {
      const evidence=document.querySelector('.layout-preview-evidence').dataset.previewEvidence;
      const video=document.getElementById('libassVideoPreview');
      return evidence==='video-footage' && video.readyState>=2;
    }""",timeout=20000)
    ensure(page.locator("#libassVideoPreview").is_visible(), "Uploaded-footage preview was not decoded")
    ensure("本地画面" in page.locator("#videoPreviewStatus").inner_text(), "Local footage provenance missing")
    screenshot(page, output, "desktop_local_footage_preview")
    page.locator('#studioStyleTab').click()
    ensure(page.locator("#libassVideoPreview").is_hidden(), "Layout retained stale video")
    ensure(page.locator('#studioStyleTab').get_attribute('aria-current') == 'page',
           "Primary nav did not select Layout")
    ensure(page.locator("#layoutAuditDrawerToggle").is_visible(), "Layout QA action hidden")
    page.locator("#layoutAuditDrawerToggle").click()
    ensure(page.locator("#layoutAuditDrawerToggle").get_attribute("aria-expanded") == "true",
           "QA drawer expansion state not exposed")
    ensure(page.locator("#layoutCorpusRail").is_visible(), "Batch audit disappeared")
    ensure(page.locator("#layoutCorpusRail").get_attribute("aria-hidden") == "false",
           "Batch audit accessibility state incorrect")
    screenshot(page, output, "desktop_optional_batch_qa")
    # The optional QA explorer is Layout-only, not a permanent third
    # column in the independent Effects task.
    rail = rect(page, ".preview-workbench .layout-corpus-rail")
    stage_with_qa = rect(page, ".preview-workbench .layout-preview-region")
    qa_inspector = rect(page, ".preview-workbench .preview-inspector")
    workbench_box=rect(page,"#subtitleLayoutPreviewCard .preview-workbench")
    ensure(rail["x"] >= workbench_box["x"]-3 and
           rail["x"]+rail["width"] <= workbench_box["x"]+workbench_box["width"]+3,
           "Optional QA drawer must stay within the bounded task surface")
    ensure(stage_with_qa["x"] + stage_with_qa["width"] <= qa_inspector["x"] + 8,
           "Desktop QA inspector overlaps the Layout stage")
    page.locator('#studioEffectsTab').click()
    ensure(page.locator("#studioEffectsTab").get_attribute("aria-current") == "page",
           "Effects not selected in the primary navigation")
    ensure(page.locator("#studioStyleTab").get_attribute("aria-current") is None,
           "Two top-level workspace tabs selected")
    ensure(page.evaluate("document.body.dataset.workspace") == "layout",
           "Effects forked the canvas layout surface")
    ensure(page.evaluate("document.body.dataset.workspaceTask") == "effects",
           "Effects task identity lost")
    page.locator("#layoutAuditDrawerToggle").click()
    ensure(page.locator("#layoutCorpusRail").is_visible(),
           "Effects must expose the shared cue browser")
    ensure(page.locator("#layoutCorpusSearch").is_visible(),
           "Effects cue selector has no searchable list")
    ensure(page.locator("#layoutAuditBtn").is_hidden(),
           "Layout-only bulk check leaked into Effects")
    page.locator("#layoutAuditDrawerToggle").click()
    ensure(page.locator("#layoutCorpusRail").is_hidden(),
           "Layout QA rail leaks into the Effects task")
    ensure(page.locator("#layoutCorpusRail").get_attribute("aria-hidden") == "true",
           "Hidden QA rail ARIA state stale after switching to Effects")
    ensure(page.locator("#layoutCorpusRail").get_attribute("inert") is not None,
           "Hidden QA rail remains keyboard interactive")
    ensure(page.locator("#layoutAuditDrawerToggle").get_attribute("aria-expanded") == "false",
           "QA opener did not reset when leaving Layout")
    # Programmatic task switches must not strand keyboard focus in a newly
    # inert subtree or on the layout-only (now hidden) drawer opener.
    page.locator('#studioStyleTab').click()
    ensure(page.locator("#layoutTimelinePanel").count()==0,
           "Removed selection-only timeline still exists")
    # The current subtitle is intentionally a collapsed, discoverable
    # read-only audio-clock panel, not an always-visible second editor.
    # Require its tab/summary to remain usable and its content to exist;
    # the test below opens it and checks actual audio-clock contents.
    ensure(page.locator("#studioV4Timing > summary").is_visible(),
           "Layout lost its current-subtitle disclosure control")
    ensure(page.locator("#layoutCueEditorPanel").count() == 1,
           "Layout lost its current subtitle/audio-clock content")
    ensure(not page.locator("#studioV4Timing").evaluate("(node)=>node.open"),
           "Returning to Layout unexpectedly expanded subtitle details")
    ensure(page.locator("#layoutCorpusRail").is_hidden(),
           "Old Layout QA drawer unexpectedly reopened on return")
    page.locator("#layoutAuditDrawerToggle").click()
    page.locator("#layoutCorpusSearch").focus()
    page.evaluate("() => showWorkspace('effects',{scroll:false})")
    ensure(page.evaluate("document.activeElement?.id") in ("studioEffectsTab","layoutAuditDrawerToggle"),
           "Moving from focused shared cue browser left keyboard focus hidden")
    page.locator('#studioStyleTab').click()
    page.locator("#layoutAuditDrawerToggle").focus()
    page.evaluate("() => showWorkspace('effects',{scroll:false})")
    ensure(page.evaluate("document.activeElement?.id") in ("studioEffectsTab","layoutAuditDrawerToggle"),
           "Shared cue browser focus was lost after switching task")
    ensure(page.locator("#layoutCorpusRail").is_hidden(),
           "Focused QA rail did not close when entering Effects")
    stage = rect(page, ".preview-workbench .layout-preview-region")
    inspector = rect(page, ".preview-workbench .preview-inspector")
    canvas = rect(page, ".preview-workbench .preview-stage")
    ensure(stage["x"] + stage["width"] <= inspector["x"] + 8,
           "Effects inspector overlaps the stage")
    ensure(inspector["width"] >= 320, "Effects inspector too narrow")
    ensure(abs(canvas["width"] / canvas["height"] - 16/9) < 0.1,
           "Stage aspect ratio incorrect")
    ensure(page.locator("#layoutTimelineViewport").count()==0,
           "Selection-only timeline should have been removed")
    page.locator("#layoutAuditDrawerToggle").click()
    ensure(page.locator("#layoutCorpusList").is_visible(),
           "Effects should reuse Layout's project subtitle browser")
    selected=page.evaluate("""async () => {
      const first=layoutCorpusItems[0];
      const ready=await selectLayoutCorpusByIndex(0);
      return {ready,expected:first?.id??null,actual:layoutCueActiveId,
        project:currentProject?.root??null,corpus:layoutCorpusItems.length,
        error:document.getElementById('layoutCorpusCheckStatus')?.textContent??''};
    }""")
    ensure(selected["ready"] and str(selected["actual"])==str(selected["expected"]),
           "Project cue selection did not finish: "+json.dumps(selected,ensure_ascii=False))
    # Autosave may finish while a cue is selected; neither a generic mock nor
    # a late project receipt may erase the selected project's identity.
    page.wait_for_timeout(850)
    ensure(page.evaluate("currentProject?.root") == "/qa/fixture",
           "Autosave unexpectedly cleared the active project")
    page.locator("#layoutAuditDrawerToggle").click()
    page.locator("#studioV4Timing > summary").click()
    ensure(page.locator("#layoutCueClockOrigin").is_visible(),
           "Read-only audio-clock source label is missing")
    ensure(page.locator("#layoutCueTimingSaveBtn").count()==0,
           "Unauthorized subtitle time editor remains in the workbench")
    ensure(not writes,"Selecting subtitle in shared browser must not modify audio or SRT")
    screenshot(page, output, "desktop_studio_audio_clock_readonly")
    # Fixture-injected failed receipt in the *real* Chromium-rendered app.
    # Exercise the existing independent retry control without resubmitting
    # either text or timing edits.
    page.evaluate("""() => layoutCueApplyExportHealth({
      state:'failed',artifacts_current:false,ass_required:true,
      artifact_error:'Fixture: an earlier ASS generation was interrupted'
    })""")
    ensure(page.locator("#layoutExportHealthNotice").is_visible(),
           "Export-failure strip must be visible after a persisted failure")
    ensure(page.locator("#layoutExportHealthRetryBtn").is_enabled(),
           "Independent export retry must be offered")
    screenshot(page, output, "desktop_studio_failed_export_notice")
    page.locator("#layoutExportHealthRetryBtn").click()
    page.wait_for_function("layoutCueExportHealthState === 'current'")
    ensure(not writes, "Export retry must not write subtitle timing")
    ensure(page.locator("#layoutExportHealthRetryBtn").is_hidden(),
           "Successful retry must clear old failure affordance")
    report["desktop"] = {
        "resolution": "1600x900", "panel_rectangles": {"rail": rail, "stage": stage, "inspector": inspector},
        "selected_cue_id": selected["actual"],
        "stage_aspect_ratio": round(canvas["width"] / canvas["height"], 3),
        "subtitle_timing_writes": len(writes), "page_errors": errors,
    }
    ensure(not errors, "Browser page errors: " + " | ".join(errors))
    page.close()


def mobile_check(browser, base_url, output, report, width, height, label):
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1,
                            is_mobile=True, has_touch=True)
    rows = [dict(x) for x in SUBTITLES]
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", make_router(rows, []))
    page.goto(base_url, wait_until="domcontentloaded")
    load_studio(page)
    ensure(page.locator("#layoutCorpusRail").is_hidden(),
           f"{label}: duplicate explorer appears by default")
    ensure(page.evaluate("currentProject?.root") == "/qa/fixture",
           f"{label}: project identity lost after fixture hydration")
    ensure(page.locator("#studioEffectsTab").get_attribute("aria-current") == "page",
           f"{label}: Effects not selected as top-level workspace")
    ensure(page.locator("#layoutAuditDrawerToggle").is_visible(),
           f"{label}: shared subtitle browser unavailable in Effects")
    # Mobile controls remain directly operable without sacrificing the 16:9
    # media stage; the selector and explicit preview are not 31px desktop taps.
    if width <= 600:
        taps = page.evaluate("""() => {
          const height=id=>document.getElementById(id).getBoundingClientRect().height;
          return {select:height('layoutAuditDrawerToggle'),
                  preview:height('previewVideoBtn'),
                  save:height('saveSubtitleLayoutBtn')};
        }""")
        ensure(min(taps.values()) >= 42,
               f"{label}: touch targets undersized: {taps}")
    screenshot(page, output, f"{label}_clean_studio")
    page.locator('#studioStyleTab').click()
    ensure(page.locator("#layoutAuditDrawerToggle").is_visible(),
           f"{label}: Layout batch QA action unavailable")
    page.locator("#layoutAuditDrawerToggle").click()
    ensure(page.locator("#layoutCorpusRail").is_visible(),
           f"{label}: optional Layout QA explorer is inaccessible")
    ensure(page.locator("#layoutCorpusRail").get_attribute("inert") is None,
           f"{label}: expanded QA remains inert")
    ensure(page.locator("#layoutAuditDrawerToggle").get_attribute("aria-expanded") == "true",
           f"{label}: QA control state out of sync")
    for selector in ("#layoutCorpusSearch", "#layoutCorpusList", "#layoutCorpusRefreshBtn"):
        ensure(page.locator(selector).is_visible(),
               f"{label}: opened mobile QA is missing {selector}")
    ensure(page.locator("#layoutCorpusMobileToggleBtn").count() == 0,
           f"{label}: nested second QA expander reappeared")
    screenshot(page, output, f"{label}_optional_layout_qa")
    # The global task switch hides the QA toolbar as well as its rail;
    # focused controls must transfer focus to the visible Effects tab.
    page.locator("#layoutAuditDrawerToggle").focus()
    page.evaluate("() => showWorkspace('effects',{scroll:false})")
    ensure(page.evaluate("document.activeElement?.id") in ("studioEffectsTab","layoutAuditDrawerToggle"),
           f"{label}: focus stranded outside visible controls")
    ensure(page.locator("#layoutCorpusRail").is_hidden(),
           f"{label}: layout QA rail leaks into Effects")
    ensure(page.locator("#layoutCorpusRail").get_attribute("aria-hidden") == "true",
           f"{label}: layout QA accessibility state not reset")
    ensure(page.locator("#layoutCorpusRail").get_attribute("inert") is not None,
           f"{label}: hidden QA controls remain focusable")
    ensure(page.locator("#layoutAuditDrawerToggle").get_attribute("aria-expanded") == "false",
           f"{label}: QA opener did not return to closed state")
    canvas=rect(page,".preview-stage")
    ensure(abs(canvas["width"]/canvas["height"]-16/9)<.045,
           f"{label}: preview squashed: {canvas}")
    ensure(page.locator("#layoutTrialTextDetails").is_hidden(),
           f"{label}: second editable subtitle field visible")
    ensure(page.locator("#previewBackgroundMode").is_hidden(),
           f"{label}: multiple preview backgrounds exposed")
    flat=page.evaluate("""() => {
      const root=document.getElementById('subtitleLayoutPreviewCard');
      return ['.studio-v4-workbench','.studio-v3-main','.studio-v3-visual',
              '.studio-v3-editors','.studio-v3-inspector'].map(
                 k=>getComputedStyle(root.querySelector(k)).overflowY);
    }""")
    ensure(all(v=="visible" for v in flat),
           f"{label}: nested scrollbox detected: {flat}")
    ensure(not page.locator("#studioV4Timing").evaluate("(n)=>n.open"),
           f"{label}: time editing should be collapsed by default")
    page.locator("#studioV4Timing > summary").click()
    cue=rect(page,"#layoutCueEditorPanel")
    inspector=rect(page,".studio-v3-inspector")
    ensure(page.locator("#layoutCueClockOrigin").is_visible(),
           f"{label}: audio-derived read-only timing notice missing")
    # Control headings/content must stay within the horizontal viewport, not
    # merely hide body overflow in CSS.
    sizes = page.evaluate("""() => ({
        html: document.documentElement.scrollWidth, body: document.body.scrollWidth,
        stage: document.querySelector('.preview-workbench').scrollWidth,
        client: document.documentElement.clientWidth
    })""")
    ensure(sizes["html"] <= width + 3, f"{label}: horizontal overflow {sizes}")
    screenshot(page, output, f"{label}_studio")
    report[label] = {"viewport": [width, height], "rectangles": {
        "stage": canvas, "cue": cue, "inspector": inspector,
    }, "scroll": sizes, "page_errors": errors}
    ensure(not errors, f"{label}: JS errors: " + " | ".join(errors))
    page.close()



def shared_corpus_request_check(browser, base_url, report):
    """Production UI reuses clean complete Review rows, not filtered/stale rows."""
    page = browser.new_page(viewport={"width": 1180, "height": 800})
    requests = []
    rows = [dict(row) for row in SUBTITLES]
    fixture_router = make_router(rows, [])
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route_corpus(route):
        if ("/subtitles?" in route.request.url and
                route.request.method == "GET"):
            requests.append(route.request.url)
        fixture_router(route)

    page.route("**/api/**", route_corpus)
    page.goto(base_url, wait_until="domcontentloaded")
    # Await real status hydration. Injecting currentProject here races the
    # production refresh() callback, invalidating an in-flight shared snapshot.
    page.wait_for_function("""() => typeof fetchSubtitles === 'function' &&
      currentProject?.root === '/qa/fixture'""", timeout=15000)
    # fillProject automatically fetches the Review list on hydration. Let
    # that app startup fetch finish, then measure only this explicit workflow.
    page.wait_for_function("loadedSubtitles.length === 4",timeout=15000)
    requests.clear()
    page.evaluate("""() => {
      document.getElementById('subSelector').value='all';
      document.getElementById('subSearchQuery').value='';
      document.getElementById('subTimeRange').value='';
      showWorkspace('review',{scroll:false});
    }""")
    page.evaluate("() => fetchSubtitles()")
    page.wait_for_function("loadedSubtitles.length === 4",timeout=15000)
    ensure(len(requests) == 1 and "selector=all" in requests[0],
           f"Clean unfiltered Review should issue exactly one full-list request: {requests}")
    page.evaluate("() => showWorkspace('layout',{scroll:false})")
    page.wait_for_function("""() =>
      layoutCorpusItems.length===4 && layoutCorpusSelectedId!==null
    """, timeout=15000)
    ensure(len(requests) == 1,
           f"Review -> Layout duplicated the full corpus request: {requests}")

    # A filtered Review request invalidates the shared full-list snapshot.
    # Layout may not treat the previous unfiltered copy as newly verified.
    page.evaluate("""() => {
      showWorkspace('review',{scroll:false});
      document.getElementById('subSearchQuery').value='stars';
    }""")
    page.evaluate("() => fetchSubtitles()")
    ensure(len(requests) == 2 and "q=stars" in requests[-1],
           "Filtered Review request was not sent to backend")
    with page.expect_response(lambda response: (
        "/subtitles?selector=all" in response.url and response.status == 200
    ), timeout=15000):
        page.evaluate("() => showWorkspace('layout',{scroll:false})")
    ensure(len(requests) == 3,
           f"Filtered Review must revalidate full Layout corpus; requests={requests}")
    ensure(not errors, "Shared corpus browser JS errors: " + " | ".join(errors))
    report["cross_workspace_corpus"] = {
        "initial_review_full_requests": 1,
        "after_clean_review_to_layout": 1,
        "after_filtered_review_and_layout": 3,
        "js_errors": errors,
    }
    page.close()




def focused_studio_viewport_check(browser, base_url, output, report):
    """Verify V4's full-size stage and a single document scrollbar."""
    measured=[]
    for width,height,mobile in [(1600,900,False),(768,1024,True),
                                (390,844,True),(844,390,True)]:
        page=browser.new_page(viewport={"width":width,"height":height},
                              device_scale_factor=1,is_mobile=mobile,has_touch=mobile)
        page.route("**/api/**",make_router([dict(row) for row in SUBTITLES],[]))
        errors=[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        page.goto(base_url,wait_until="domcontentloaded")
        load_studio(page)
        for destination in ("effects","layout"):
            page.locator("#studioEffectsTab" if destination=="effects" else "#studioStyleTab").click()
            view=page.evaluate("""() => {
              const root=document.getElementById('subtitleLayoutPreviewCard');
              const stage=root.querySelector('.preview-stage');
              const actions=root.querySelector('.studio-v3-command-bar');
              const els=['.studio-v4-workbench','.studio-v3-main',
                '.studio-v3-visual','.studio-v3-editors','.studio-v3-inspector']
                  .map(x=>root.querySelector(x));
              const box=x=>x.getBoundingClientRect();
              return {
                width:box(stage).width,height:box(stage).height,
                beforeActions:box(stage).bottom<=box(actions).top+2,
                overflow:els.map(x=>getComputedStyle(x).overflowY),
                secondText:!!document.getElementById('layoutTrialTextDetails').offsetParent,
                backdropPicker:!!document.getElementById('previewBackgroundFile')
              };
            }""")
            ensure(view["beforeActions"] and
                   view["width"]>width*.55 and
                   abs(view["width"]/view["height"]-16/9)<.05,
                   f"{width}: canvas too small or occluded: {view}")
            ensure(all(v=="visible" for v in view["overflow"]),
                   f"{width}: nested scroll region: {view}")
            ensure(not view["secondText"] and not view["backdropPicker"],
                   f"{width}: duplicate inputs returned: {view}")
            measured.append({"width":width,"height":height,"task":destination,"view":view})
        ensure(not errors,f"{width}: JS errors: {errors}")
        page.screenshot(path=str(output/f"flat_studio_{width}.png"),full_page=True)
        page.close()
    report["flat_studio"]=measured


def touch_safety_check(browser, base_url, report):
    page=browser.new_page(viewport={"width":390,"height":844},
                          device_scale_factor=1,is_mobile=True,has_touch=True)
    page.route("**/api/**",make_router([dict(row) for row in SUBTITLES],[]))
    page.goto(base_url,wait_until="domcontentloaded")
    load_studio(page)
    page.locator("#studioStyleTab").click()
    number=page.locator("#prevChsSizeTouchNumber")
    ensure(number.is_visible(),"Coarse pointer has no direct numeric parameter input")
    ensure(not page.locator("#prevChsSize").is_visible(),
           "Touch range still captures vertical scrolling")
    before=page.locator("#prevChsSize").input_value()
    page.evaluate("""() => {
      const inspector=document.querySelector('#subtitleLayoutPreviewCard .preview-inspector');
      inspector.scrollTop+=140;
    }""")
    ensure(page.locator("#prevChsSize").input_value()==before,
           "Scrolling Inspector unexpectedly changed parameter value")
    number.fill("52")
    number.dispatch_event("change")
    ensure(page.locator("#prevChsSize").input_value()=="52",
           "Direct touch numeric editor failed to update authoritative range value")
    # Rendering is debounced; the committed source range changes synchronously.
    page.wait_for_function("""() => document.getElementById('prevChsSizeVal')
      .textContent.trim().startsWith('52')""", timeout=12000)
    # Layout typography AND bilingual region must be visible together.
    # The previous name= accordion was mutually exclusive, concealing 60/40.
    style_sections=page.locator('details.layout-inspector-section[data-studio-scope="style"]')
    ensure(style_sections.count()==3,"Expected typography, bilingual position and readability")
    ensure(style_sections.nth(0).evaluate("(el)=>el.open") and
           style_sections.nth(1).evaluate("(el)=>el.open"),
           "Typography and 60/40 position must both be expanded on Layout entry")
    ensure(page.locator("#prevChsSizeTouchNumber").is_visible(),
           "Typography number input not visible")
    ensure(page.locator("#prevPrimaryShareTouchNumber").is_visible(),
           "60/40 bilingual region number input not visible")
    ensure(page.locator("#prevPrimaryShareTouchNumber").is_visible() and
           page.locator('[data-share-percent="60"]').is_visible(),
           "60/40 position controls must be conspicuous on a touch device")
    page.locator('[data-share-percent="40"]').click()
    ensure(page.locator("#prevPrimaryShare").input_value()=="40",
           "40/60 quick choice did not reach authoritative region field")
    page.wait_for_function("""() => document.getElementById('prevPrimaryShareVal')
       .textContent.trim()==='40% / 60%'""", timeout=10000)
    page.locator("#prevPrimaryShareTouchNumber").fill("50")
    page.locator("#prevPrimaryShareTouchNumber").dispatch_event("change")
    ensure(page.locator("#prevPrimaryShare").input_value()=="50",
           "Explicit numeric region edit is not connected")
    style_sections.nth(2).locator(":scope > summary").click()
    ensure(style_sections.nth(2).evaluate("(el)=>el.open") and
           style_sections.nth(0).evaluate("(el)=>el.open") and
           style_sections.nth(1).evaluate("(el)=>el.open"),
           "Opening readability must not close typography/position")
    number.fill("48")
    number.dispatch_event("change")
    ensure(page.locator("#prevChsSize").input_value()=="48",
           "Direct touch typography edit failed")
    share=page.locator("#prevPrimaryShareTouchNumber")
    share.fill("65")
    share.dispatch_event("change")
    ensure(page.locator("#prevPrimaryShare").input_value()=="65",
           "Touch region share is not wired to layout state")
    page.wait_for_function("""() => document.getElementById('prevPrimaryShareVal')
      .textContent.trim()==='65% / 35%'""",timeout=12000)
    stepper=page.locator("#prevChsSizeTouchNumber").locator("xpath=..")
    stepper.get_by_role("button",name="中文基础字号（数值调整） 增加").click()
    ensure(page.locator("#prevChsSize").input_value()=="49",
           "Intentional +1 button failed to update the authoritative range")
    ensure(not page.locator("#layoutTimelinePan").is_visible(),
           "Timeline range still captures touch scrolling in Effects")
    # The retired editable timeline controls are no longer the navigation
    # contract. Shared project-cue browser arrows remain accessible on touch.
    ensure(page.locator("#layoutPreviousSampleBtn").count()==1 and
           page.locator("#layoutNextSampleBtn").count()==1,
           "Shared subtitle browser lost previous/next navigation")
    page.locator("#layoutAuditDrawerToggle").click()
    ensure(page.locator("#layoutPreviousSampleBtn").is_visible() and
           page.locator("#layoutNextSampleBtn").is_visible(),
           "Touch cue navigation is inaccessible in the opened browser")
    page.locator(".layout-corpus-item").first.click()
    page.wait_for_function("""() => String(layoutCueActiveId)===
      String(layoutCorpusVisibleRows()[0]?.id)""",timeout=12000)
    page.locator("#layoutNextSampleBtn").click()
    page.wait_for_function("""() => String(layoutCueActiveId)===
      String(layoutCorpusVisibleRows()[1]?.id)""",timeout=12000)
    ensure(page.evaluate("currentProject?.root")=="/qa/fixture",
           "Cue navigation unexpectedly invalidated the open project")
    page.locator("#layoutAuditDrawerToggle").click()
    ensure(page.locator("#hsrTouchSliderToggle").count()==0,
           "Global slider locking control has returned")
    report["touch_numeric"]={"before":before,"after":page.locator("#prevChsSize").input_value()}
    page.close()


def gradient_background_check(browser, base_url, output, report):
    """Real Chromium UI + real FFmpeg preview PNG at desktop and phone width."""
    import sys
    sys.path.insert(0, str(ROOT))
    from app.archive_bundle_backgrounds import normalize_bundle_background, render_background_preview

    evidence = []
    for width, height in ((1600, 900), (390, 844)):
        page = browser.new_page(viewport={"width": width, "height": height})
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def preview_route(route):
            payload = route.request.post_data_json
            assert payload["expected_project_root"] == "/qa/fixture"
            spec = normalize_bundle_background(payload.get("background_spec"))
            assert spec["mode"] in {"linear", "radial", "soft"}
            requests.append(spec)
            png = render_background_preview(
                spec, color=payload.get("background_color") or "#000000"
            )
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({
                    "ok": True, "image_base64": base64.b64encode(png).decode("ascii"),
                    "source": "ffmpeg", "width": 640, "height": 360,
                }),
            )
        page.route("**/api/**", make_router([dict(x) for x in SUBTITLES], []))
        page.route("**/api/output/archive-bundle/background-preview", preview_route)
        # App startup calls refresh() asynchronously and can change the
        # archive's visibility. Wait for fixture network to settle before
        # installing the checked project state.
        page.goto(base_url, wait_until="networkidle")
        page.evaluate("""() => {
          currentProject={root:'/qa/fixture',name:'Gradient fixture'};
          syncArchiveBundleProjectState({
            root:'/qa/fixture',
            bundle_export:{
              state:'stale',message:'背景已变化，需要更新 MKV',
              background_spec:{mode:'linear',first:'#e25a6b',second:'#112941',
                third:'#313f73',direction:'horizontal',resolution:'1080p',quality:'high'}
            }
          }, true);
          // The fixture intentionally replaces a loaded project's saved
          // background: pass the explicit force flag rather than relying on
          // a fictitious root change to bypass draft-preservation semantics.
          showWorkspace('export',{scroll:false});
          // This deterministic browser fixture has no built project summary:
          // reveal the normal Export result container explicitly, rather
          // than simulating a successful build or pretending files exist.
          document.getElementById('archiveCard').classList.remove('hidden');
          document.getElementById('finishedOutputs').classList.remove('hidden');
          document.getElementById('archiveBundlePanel').open=true;
        }""")
        # Workspace auto-disclosure runs on requestAnimationFrame after
        # navigation and may close the Export details panel. Open it as the
        # user would after that navigation pass settles.
        page.wait_for_timeout(220)
        page.locator("#archiveBundlePanel").evaluate("(node) => { node.open = true; }")
        ensure(page.locator("#archiveCard").is_visible(),
               "MKV archive Export section is CSS-hidden despite being selected")
        if not page.locator("#bundleBackgroundMode").is_visible():
            print("GRADIENT UI VISIBILITY DIAGNOSTIC", json.dumps(page.evaluate("""() => {
              const el=document.getElementById('bundleBackgroundMode');
              const rows=[];
              for(let node=el;node;node=node.parentElement)rows.push({
                tag:node.tagName,id:node.id,classes:node.className,
                hidden:node.hidden,open:node.tagName==='DETAILS'?node.open:undefined,
                display:getComputedStyle(node).display,
                visibility:getComputedStyle(node).visibility
              });
              return {workspace:document.body.dataset.workspace,
                project:currentProject?.root||null,ancestors:rows};
            }"""), ensure_ascii=False))
        ensure(page.locator("#bundleBackgroundMode").is_visible(),
               "Background mode selector is not visible in Export workspace")
        ensure(page.locator("#bundleBackgroundMode").input_value() == "linear",
               "Saved gradient background mode not restored")
        ensure(page.locator("#bundleVideoResolution").input_value() == "1080p",
               "Saved 1080p resolution not restored")
        page.locator("#bundleBackgroundMode").select_option("soft")
        ensure(page.locator("#bundleGradientThird").is_visible(),
               "Soft mode must reveal third color control")
        ensure(not page.locator("#bundleGradientDirection").is_visible(),
               "Radial/soft should not show irrelevant linear direction")
        page.locator("#bundlePreviewBtn").click()
        page.wait_for_function("""() => {
          const img=document.getElementById('bundleBackgroundPreview');
          return img?.style.display==='block' && img.complete &&
            img.naturalWidth===640 && img.naturalHeight===360;
        }""", timeout=30000)
        ensure(requests and requests[-1]["mode"] == "soft",
               "Browser submitted wrong soft gradient to FFmpeg")
        # Actual production browser DOM contract: a stale MKV is not a
        # second current deliverable and doesn't erase FLAC/SRT/ASS artifacts.
        states = page.evaluate("""() => {
          const existing=name=>({path:'/qa/fixture/output/'+name,exists:true,size_bytes:64});
          const outputs={
            'HSR_Voice_Archive_Bundle.mkv':existing('HSR_Voice_Archive_Bundle.mkv'),
            'HSR_Voice_Archive_Black.mkv':existing('HSR_Voice_Archive_Black.mkv'),
            'continuous.flac':existing('continuous.flac'),
            'HSR_Voice_Archive.srt':existing('HSR_Voice_Archive.srt'),
            'HSR_Voice_Archive.ass':existing('HSR_Voice_Archive.ass')
          };
          const project={root:'/qa/fixture',bundle_export:{
            state:'stale',needs_rebuild:true,can_generate:false,
            message:'字幕已变化，旧文件保留',blockers:['请先更新 ASS/SRT'],
            prerequisites:{continuous_flac:true,srt_current:true,ass_current:false,subtitles_current:false}
          }};
          const inspect=()=>{
            const cards=[...document.querySelectorAll('#artifactBrowser .artifact-card')];
            return {labels:cards.map(x=>x.querySelector('.badge')?.textContent),
              names:cards.map(x=>x.querySelector('.artifact-card-title')?.textContent),
              count:document.getElementById('artifactCount')?.textContent,
              pendingAction:[...cards[0]?.querySelectorAll('.artifact-card-actions button')||[]].map(x=>x.textContent).join(' / ')};
          };
          renderArtifactBrowser(project,outputs,outputs);
          const stale=inspect();
          project.bundle_export={...project.bundle_export,state:'current',needs_rebuild:false,
            can_generate:true,blockers:[],prerequisites:{continuous_flac:true,srt_current:true,ass_current:true,subtitles_current:true}};
          renderArtifactBrowser(project,outputs,outputs);
          const current=inspect();
          project.bundle_export={...project.bundle_export,state:'stale',needs_rebuild:true,
            can_generate:false,blockers:['请先更新 ASS/SRT'],
            prerequisites:{continuous_flac:true,srt_current:true,ass_current:false,subtitles_current:false}};
          renderArtifactBrowser(project,outputs,outputs);
          return {stale,current};
        }""")
        ensure(states["stale"]["labels"][0] == "旧文件·已过期",
               "Old MKV must not show as a current bundle")
        ensure("背景／重新封装" in states["stale"]["pendingAction"],
               "Stale MKV must expose the one authoritative remux/settings action")
        ensure(states["current"]["labels"][0] == "当前有效",
               "Current bundle should show validated source status")
        ensure(states["stale"]["names"][0] == "HSR_Voice_Archive_Bundle.mkv",
               "The complete MKV is the sole default finished media object")
        ensure(all(name not in states["stale"]["names"] for name in
                   ("continuous.flac", "HSR_Voice_Archive.srt", "HSR_Voice_Archive.ass")),
               "Redundant audio/subtitle working sources must not appear as default deliverables")
        ensure("独立轨道按需导出" in states["stale"]["count"],
               "UI should distinguish default product from explicit track exports")
        ensure(not errors, "Gradient/background browser JS error: " + repr(errors))
        screenshot(page, output, "gradient_background_" + str(width))
        evidence.append({"viewport": str(width)+"x"+str(height),
                         "mode": requests[-1]["mode"],
                         "preview": "FFmpeg-generated PNG decoded by Chromium",
                         "selected_resolution": page.locator("#bundleVideoResolution").input_value()})
        page.close()
    report["gradient_background"] = evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/subtitle-browser"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/app/static/index.html"
    report = {
        "category": "Chromium rendering of checked-out production HTML/CSS/JS with API fixtures",
        "backend": "test doubles for corpus and geometry (NOT real manifest, FFmpeg/libass, physical-device or final-media validation)",
        "screenshots": [],
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                desktop_check(browser, url, args.output, report)
                focused_studio_viewport_check(browser, url, args.output, report)
                touch_safety_check(browser, url, report)
                shared_corpus_request_check(browser, url, report)
                gradient_background_check(browser, url, args.output, report)
                mobile_check(browser, url, args.output, report, 390, 844, "mobile_390")
                mobile_check(browser, url, args.output, report, 768, 1024, "tablet_768")
            finally:
                browser.close()
    finally:
        server.shutdown()
        report["screenshots"] = sorted(f.name for f in args.output.glob("*.png"))
        (args.output / "browser_evidence.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print("Chromium fixture visual + interaction evidence: OK")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
