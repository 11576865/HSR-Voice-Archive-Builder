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
import os
import subprocess
import tempfile
from pathlib import Path
import threading
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
        if self.path.startswith("/static/hsr-ui-convergence.css"):
            css=(ROOT / "app/static/hsr-ui-convergence.css").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(css)))
            self.end_headers()
            self.wfile.write(css)
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
    """Real decodable WebM bytes, kept separate from backend libass proof."""
    with tempfile.TemporaryDirectory(prefix="hsr-browser-video-") as td:
        path = Path(td) / "motion.webm"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=1.5",
              "-an", "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
             "-pix_fmt", "yuv420p", str(path)],
            capture_output=True, text=True, timeout=40, check=True,
        )
        return path.read_bytes()


def make_router(rows: list[dict], writes: list[dict], video_bytes: bytes | None = None):
    def handler(route):
        request = route.request
        url = request.url
        path = url.split("?", 1)[0]
        body = request.post_data_json if request.method == "POST" and request.post_data else {}
        if path.endswith("/api/subtitle-layout/render-preview-video") and video_bytes:
            assert body.get("preview_format") == "webm", "Chromium must negotiate VP8"
            has_source = "source_video_base64" in body
            has_image = "preview_background_image_base64" in body
            assert not (has_source and has_image), "Background input sources must be exclusive"
            if has_source:
                assert base64.b64decode(body["source_video_base64"], validate=True) == video_bytes
                assert body.get("source_start_seconds") == 0.2
            if has_image:
                assert base64.b64decode(body["preview_background_image_base64"], validate=True).startswith(
                    b"\x89PNG\r\n\x1a\n")
            # Browser test proves decoded playback and UI provenance; separate
            # real-FFmpeg test proves the output pixels include actual ASS.
            route.fulfill(status=200, content_type="video/webm", body=video_bytes,
                          headers={"X-HSR-Preview-Background":
                                   "local-footage" if has_source else
                                   "background-image" if has_image else "black"})
            return
        if path.endswith("/api/status"):
            payload = {
                "ok": True, "version": "visual-fixture", "project": None,
                "runtime": {"ffmpeg": False, "warnings": []},
                "recent_projects": [], "jobs": [],
            }
        elif path.endswith("/api/quick/candidates"):
            payload = {"ok": True, "candidates": []}
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
            assert body.get("expected_project_root") == "/qa/fixture", "project guard missing"
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
    page.evaluate("""() => {
        currentProject = {name:'visual-fixture',root:'/qa/fixture',config:{
            source_text_language:'en',target_language:'zh-CN',subtitle_chs_font:'Arial',
            subtitle_primary_font:'Arial'
        }};
        showWorkspace('layout',{scroll:false});
    }""")
    page.wait_for_function("document.querySelectorAll('[data-timeline-cue]').length >= 3", timeout=12000)
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
    ensure(page.locator("#layoutTimelineViewport").is_visible(), "Effects timeline hidden")
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
    ensure(page.locator("#layoutAuditDrawerToggle").is_hidden(),
           "Layout-only batch QA control appears in Effects")
    ensure(page.locator("#layoutCorpusRail").is_hidden(), "Second full subtitle explorer still dominates UI")
    ensure(page.locator("#layoutTrialTextDetails").get_attribute("open") is None,
           "Manual text authoring should not be a second visible editor")
    ensure(page.locator('#studioEffectsTab').count() == 1 and
           page.locator('#studioStyleTab').count() == 1,
           "Layout and Effects must have one primary navigation entry each")
    ensure(page.locator("#layoutCueFinalText").count() == 0,
           "Duplicated final text textarea is still mounted")
    shell = page.locator("#subtitleLayoutPreviewCard .preview-workbench").bounding_box()
    ensure(shell is not None and shell["height"] < 850,
           f"Desktop workbench must be viewport-bounded, not an endless document: {shell}")
    scroll_regions=page.evaluate("""() => {
      const root=document.querySelector('#subtitleLayoutPreviewCard .preview-workbench');
      const preview=root.querySelector('.layout-preview-region');
      const inspector=root.querySelector('.preview-inspector');
      return [preview,inspector].map(x=>({scroll:getComputedStyle(x).overflowY,
        height:x.clientHeight,outer:root.clientHeight}));
    }""")
    ensure(all(x["scroll"] == "auto" and x["height"] <= x["outer"]+3 for x in scroll_regions),
           f"Desktop canvas and Inspector must be independent scroll panes: {scroll_regions}")
    ensure(page.locator("#hsrKaraokeReadiness").is_visible(),
           "Karaoke generation/import remains hidden in an advanced disclosure")
    for action in ("#refreshWordAlignmentBtn","#importWordAlignmentBtn"):
        ensure(page.locator(action).is_visible(),"Missing visible Karaoke recovery: "+action)
    page.evaluate("() => loadWordAlignmentDiagnostics()")
    ensure("尚无可诊断" in page.locator("#hsrKaraokeReadinessDetail").inner_text(),
           "Karaoke readiness failed to explain missing project data")
    ensure(page.locator("#generateWordAlignmentBtn").is_disabled(),
           "WhisperX generation claimed availability in an unconfigured fixture")
    ensure(page.locator("#hsrEffectOperationStatus").is_visible(),
           "Effects operation status is not visible beside the stage")
    screenshot(page, output, "desktop_clean_effects_workspace")
    # This is a real Chromium decoded-video interaction, but the API response
    # is a deterministic MP4 fixture. Backend libass video is checked separately.
    page.locator("#previewVideoBtn").click()
    try:
        page.wait_for_function("""() => {
          const video=document.getElementById('libassVideoPreview');
          return video && video.readyState>=2 &&
            document.querySelector('.layout-preview-evidence').dataset.previewEvidence==='video';
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
    # An image must reach the actual video endpoint, rather than being used
    # merely as decorative background in the browser's geometry preview.
    png=b"\x89PNG\r\n\x1a\n"+b"browser-fixture"
    page.locator("#effectBackgroundImageFile").set_input_files({
        "name": "custom-image.png", "mimeType": "image/png", "buffer": png
    })
    ensure(page.locator("#hsrEffectOperationStatus").get_attribute("data-phase")=="stale",
           "Changing media retained false success state")
    page.locator("#previewVideoBtn").click()
    page.wait_for_function("""() => {
      const video=document.getElementById('libassVideoPreview');
      return video.readyState>=2 && document.getElementById('videoPreviewStatus')
        .textContent.includes('本地背景图片');
    }""",timeout=20000)
    screenshot(page, output, "desktop_image_background_effect_preview")
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
    ensure(page.locator("#layoutTimelineViewport").is_visible(),
           "Top-level Effects lost the timeline")
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
    ensure(page.locator("#layoutCorpusRail").is_hidden(),
           "Old Layout QA drawer unexpectedly reopened on return")
    page.locator("#layoutAuditDrawerToggle").click()
    page.locator("#layoutCorpusSearch").focus()
    page.evaluate("() => showWorkspace('effects',{scroll:false})")
    ensure(page.evaluate("document.activeElement?.id") == "studioEffectsTab",
           "Moving from focused QA rail to Effects lost visible keyboard focus")
    page.locator('#studioStyleTab').click()
    page.locator("#layoutAuditDrawerToggle").focus()
    page.evaluate("() => showWorkspace('effects',{scroll:false})")
    ensure(page.evaluate("document.activeElement?.id") == "studioEffectsTab",
           "Layout QA opener retained hidden focus after switching to Effects")
    ensure(page.locator("#layoutCorpusRail").is_hidden(),
           "Focused QA rail did not close when entering Effects")
    stage = rect(page, ".preview-workbench .layout-preview-region")
    inspector = rect(page, ".preview-workbench .preview-inspector")
    timeline = rect(page, "#layoutTimelineViewport")
    canvas = rect(page, ".preview-workbench .preview-stage")
    ensure(stage["x"] + stage["width"] <= inspector["x"] + 8,
           "Effects inspector overlaps two-column stage")
    ensure(inspector["width"] >= 320, "Effects inspector width below useful minimum")
    ensure(abs(canvas["width"] / canvas["height"] - 16/9) < 0.1,
           "Preview stage is not approximately 16:9")
    ensure(timeline["width"] > 400, "Desktop timeline has insufficient width")
    ensure(page.locator('[data-timeline-cue="11"]').count() == 1, "Cue 11 not visible")
    ensure(page.locator('[data-timeline-cue="12"]').count() == 1, "Cue 12 not visible")
    screenshot(page, output, "desktop_studio_before_edit")

    # Use real browser pointer events to move selected cue END near next cue start.
    page.evaluate("() => layoutTimelineSelect(11)")
    page.wait_for_function("String(layoutCueActiveId)==='11' && document.querySelector('[data-timeline-edge=\"end\"]')!==null")
    handle = page.locator('[data-timeline-edge="end"]')
    handle.scroll_into_view_if_needed()
    hbox = handle.bounding_box()
    ensure(hbox["y"] >= 0 and hbox["y"] + hbox["height"] <= 900,
           f"Drag handle outside browser viewport after scroll: {hbox}")
    vp = rect(page, "#layoutTimelineViewport")
    start, duration = page.evaluate("[layoutTimelineWindowStart,layoutTimelineWindowSeconds]")
    target_x = vp["x"] + (7.39 - start) / duration * vp["width"]
    target_y = hbox["y"] + hbox["height"]/2
    page.mouse.move(hbox["x"] + hbox["width"]/2, target_y)
    page.mouse.down()
    armed = page.evaluate("layoutTimelineDrag")
    ensure(armed is not None and str(armed.get("id")) == "11",
           f"Real pointerdown did not arm the selected cue edge: {armed}")
    page.mouse.move(target_x, target_y, steps=8)
    page.mouse.up()
    actual = page.evaluate("""() => ({
      selected: layoutCueActiveId, value: document.getElementById('layoutCueTimingEnd').value,
      state: layoutTimingState(layoutCueRow(layoutCueActiveId)),
      drag: layoutTimelineDrag, left: layoutTimelineWindowStart,
      zoom: layoutTimelineWindowSeconds
    })""")
    print("CHROMIUM DRAG DIAGNOSTIC", json.dumps(actual, ensure_ascii=False), flush=True)
    page.wait_for_function("Number(document.getElementById('layoutCueTimingEnd').value) > 7.1", timeout=3000)
    end_draft = float(page.locator("#layoutCueTimingEnd").input_value())
    ensure(abs(end_draft - 7.4) < 0.051, f"Pointer snapping failed: {end_draft}")
    ensure(not writes, "Pointer drag persisted timing without explicit save")
    ensure(page.locator("#layoutCueTimingSaveBtn").is_enabled(), "Save not offered for changed draft")
    screenshot(page, output, "desktop_studio_unsaved_drag")
    page.locator("#layoutCueTimingSaveBtn").click()
    page.wait_for_function("document.getElementById('layoutCueTimingSaveBtn').disabled === true")
    ensure(len(writes) == 1 and abs(writes[0]["end"] - 7.4) < 0.05, "Saved timing mismatch")
    ensure(abs(rows[0]["source_end"] - 7.0) < 0.001, "Source audio time mutated by subtitle save")
    page.locator("#layoutTimelineZoom").select_option("60")
    ensure(page.locator("#layoutTimelineRuler .layout-timeline-tick").count() >= 2, "Zoom ruler empty")
    screenshot(page, output, "desktop_studio_saved_zoom")
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
    ensure(len(writes) == 1, "Export retry erroneously repeated the cue-timing write")
    ensure(page.locator("#layoutExportHealthRetryBtn").is_hidden(),
           "Successful retry must clear old failure affordance")
    report["desktop"] = {
        "resolution": "1600x900", "panel_rectangles": {"rail": rail, "stage": stage, "inspector": inspector},
        "timeline_width": timeline["width"], "stage_aspect_ratio": round(canvas["width"] / canvas["height"], 3),
        "dragged_end": end_draft, "write_count": len(writes), "page_errors": errors,
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
    ensure(page.locator("#studioEffectsTab").get_attribute("aria-current") == "page",
           f"{label}: Effects not selected as top-level workspace")
    ensure(page.locator("#layoutAuditDrawerToggle").is_hidden(),
           f"{label}: layout-only batch QA leaks into Effects")
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
    ensure(page.evaluate("document.activeElement?.id") == "studioEffectsTab",
           f"{label}: focus stranded on now-hidden QA opener")
    ensure(page.locator("#layoutCorpusRail").is_hidden(),
           f"{label}: layout QA rail leaks into Effects")
    ensure(page.locator("#layoutCorpusRail").get_attribute("aria-hidden") == "true",
           f"{label}: layout QA accessibility state not reset")
    ensure(page.locator("#layoutCorpusRail").get_attribute("inert") is not None,
           f"{label}: hidden QA controls remain focusable")
    ensure(page.locator("#layoutAuditDrawerToggle").get_attribute("aria-expanded") == "false",
           f"{label}: QA opener did not return to closed state")
    vp = rect(page, "#layoutTimelineViewport")
    canvas = rect(page, ".preview-workbench .preview-stage")
    cue = rect(page, "#layoutCueEditorPanel")
    inspector = rect(page, ".preview-inspector")
    ensure(canvas["y"] < cue["y"],
           f"{label}: preview must come before selected subtitle timing")
    if width < 760:
        preview_pane = rect(page, ".preview-workbench .layout-preview-region")
        ensure(preview_pane["y"] + preview_pane["height"] <= inspector["y"] + 8,
               f"{label}: bounded preview pane physically overlaps inspector")
        ensure(page.evaluate("""() => getComputedStyle(
          document.querySelector('.preview-workbench .layout-preview-region')).overflowY""") == "auto",
               f"{label}: cue/timeline must scroll inside clipped preview pane")
    else:
        preview_pane = rect(page, ".preview-workbench .layout-preview-region")
        ensure(preview_pane["y"] + preview_pane["height"] <= inspector["y"] + 8,
               f"{label}: tablet preview scroll pane overlaps inspector")
        ensure(inspector["width"] >= width * 0.72,
               f"{label}: tablet inspector too narrow for controls")
    ensure(vp["width"] > width*(0.6 if width < 760 else 0.38), f"{label}: timeline too narrow")
    ensure(page.locator("#layoutCueTimingStart").is_enabled(), f"{label}: timing editor inaccessible")
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
    page.wait_for_function("typeof fetchSubtitles === 'function'", timeout=15000)
    page.evaluate("""() => {
      currentProject={name:'fixture-project',root:'/qa/fixture',
                      config:{source_text_language:'en',target_language:'zh-CN'}};
      document.getElementById('subSelector').value='all';
      document.getElementById('subSearchQuery').value='';
      document.getElementById('subTimeRange').value='';
      showWorkspace('review',{scroll:false});
    }""")
    page.evaluate("() => fetchSubtitles()")
    ensure(len(requests) == 1 and "selector=all" in requests[0],
           "Clean unfiltered Review should issue exactly one full-list request")
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
    """Both tasks are bounded in the ordinary page: no focus/fullscreen mode."""
    measured=[]
    for width,height,mobile in [(1600,900,False),(768,1024,True),(390,844,True)]:
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
              const task=root.querySelector('.preview-workbench');
              const stage=task.querySelector('.layout-preview-region');
              const tools=task.querySelector('.preview-inspector');
              return {
                rootPosition:getComputedStyle(root).position,
                taskHeight:task.clientHeight,taskScroll:task.scrollHeight,
                stageHeight:stage.clientHeight,toolsHeight:tools.clientHeight,
                stageOverflow:getComputedStyle(stage).overflowY,
                toolsOverflow:getComputedStyle(tools).overflowY,
                innerHeight:window.innerHeight,
                focusControls:!!document.getElementById('hsrStudioFocusToggle'),
                lockControls:!!document.getElementById('hsrTouchSliderToggle'),
              };
            }""")
            ensure(view["rootPosition"]!="fixed" and not view["focusControls"] and
                   not view["lockControls"],f"{width}: hidden modal/focus lock remains: {view}")
            ensure(260 <= view["taskHeight"] <= max(view["innerHeight"],340),
                   f"{width}: workbench is unbounded: {view}")
            ensure(view["stageOverflow"]=="auto" and view["toolsOverflow"]=="auto",
                   f"{width}: content is not independently scrollable: {view}")
            ensure(view["stageHeight"]<=view["taskHeight"]+3 and
                   view["toolsHeight"]<=view["taskHeight"]+3,
                   f"{width}: canvas/inspector escaped bounded frame: {view}")
            measured.append({"width":width,"height":height,"task":destination,"view":view})
        ensure(not errors,f"{width}: JS errors in bounded workbench: {errors}")
        page.screenshot(path=str(output/f"bounded_studio_{width}.png"),full_page=False)
        page.close()
    report["bounded_studio"]=measured


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
    # Mutually exclusive native Inspector sections keep the toolbar short.
    # Presets belong to a different section, so open it intentionally.
    style_sections=page.locator('details[name="hsr-style-parameter-groups"]')
    ensure(style_sections.count()==4,"Style must expose four exclusive parameter groups")
    page.locator('details[name="hsr-style-parameter-groups"]').nth(2).locator(":scope > summary").click()
    ensure(page.locator('details[name="hsr-style-parameter-groups"][open]').count()==1,
           "Multiple long style sections remained expanded after category switch")
    page.locator("#prevPresetDefault").click()
    ensure(number.input_value()=="48",
           "Preset restored range but left the touch numeric editor stale")
    page.locator('details[name="hsr-style-parameter-groups"]').first.locator(":scope > summary").click()
    ensure(page.locator('details[name="hsr-style-parameter-groups"][open]').count()==1,
           "Returning to typography created multiple long expanded sections")
    ensure(page.locator("#prevChsSizeTouchNumber").is_visible(),
           "Numeric editor disappeared after accordion navigation")
    stepper=page.locator("#prevChsSizeTouchNumber").locator("xpath=..")
    stepper.get_by_role("button",name="中文基础字号（数值调整） 增加").click()
    ensure(page.locator("#prevChsSize").input_value()=="49",
           "Intentional +1 button failed to update the authoritative range")
    ensure(not page.locator("#layoutTimelinePan").is_visible(),
           "Timeline range still captures touch scrolling in Effects")
    ensure(page.locator("#layoutTimelineEarlierBtn").count()==1 and
           page.locator("#layoutTimelineLaterBtn").count()==1,
           "Timeline navigation lost its intentional arrow controls")
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
          });
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
