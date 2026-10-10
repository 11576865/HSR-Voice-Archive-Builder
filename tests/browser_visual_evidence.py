"""Chromium visual/interaction evidence for shipped HSR subtitle studio.

The actual app/static/index.html is served from this checkout, with deterministic
API fixtures. Screenshots are real Chromium pixels, but NOT live project/libass
render evidence or phone-device screenshots.
"""
from __future__ import annotations

import argparse
import json
import math
import os
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


def make_router(rows: list[dict], writes: list[dict]):
    def handler(route):
        request = route.request
        url = request.url
        path = url.split("?", 1)[0]
        body = request.post_data_json if request.method == "POST" and request.post_data else {}
        if path.endswith("/api/status"):
            payload = {
                "ok": True, "version": "visual-fixture", "project": None,
                "runtime": {"ffmpeg": False, "warnings": []},
                "recent_projects": [], "jobs": [],
            }
        elif path.endswith("/api/quick/candidates"):
            payload = {"ok": True, "candidates": []}
        elif "/subtitles?" in url or path.endswith("/subtitles"):
            payload = {"ok": True, "subtitles": rows, "persistable": True}
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
    page.evaluate("() => window.scrollTo(0, 0)")


def screenshot(page, output: Path, name: str):
    page.screenshot(path=str(output / f"{name}.png"), full_page=True, animations="disabled")


def desktop_check(browser, base_url, output, report):
    page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
    rows = [dict(x) for x in SUBTITLES]
    writes = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", make_router(rows, writes))
    page.goto(base_url, wait_until="domcontentloaded")
    load_studio(page)
    rail = rect(page, ".preview-workbench .layout-corpus-rail")
    stage = rect(page, ".preview-workbench .layout-preview-region")
    inspector = rect(page, ".preview-workbench .preview-inspector")
    timeline = rect(page, "#layoutTimelineViewport")
    canvas = rect(page, ".preview-workbench .preview-stage")
    ensure(rail["x"] + rail["width"] <= stage["x"] + 8, "Desktop left corpus overlaps stage")
    ensure(stage["x"] + stage["width"] <= inspector["x"] + 8, "Desktop inspector overlaps stage")
    ensure(rail["width"] >= 205 and inspector["width"] >= 320, "Desktop panel width below useful minimum")
    ensure(abs(canvas["width"] / canvas["height"] - 16/9) < 0.1, "Preview stage is not approximately 16:9")
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
    vp = rect(page, "#layoutTimelineViewport")
    canvas = rect(page, ".preview-workbench .preview-stage")
    cue = rect(page, "#layoutCueEditorPanel")
    rail = rect(page, ".layout-corpus-rail")
    inspector = rect(page, ".preview-inspector")
    if width < 760:
        ensure(canvas["y"] < cue["y"] < rail["y"] < inspector["y"],
               f"{label}: invalid mobile task order")
    else:
        ensure(rail["x"] + rail["width"] <= canvas["x"] + 8,
               f"{label}: tablet corpus/stage overlap")
        ensure(inspector["y"] >= min(rail["y"] + rail["height"], cue["y"] + cue["height"]) - 8,
               f"{label}: tablet inspector overlays timeline/corpus")
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
        "stage": canvas, "cue": cue, "rail": rail, "inspector": inspector,
    }, "scroll": sizes, "page_errors": errors}
    ensure(not errors, f"{label}: JS errors: " + " | ".join(errors))
    page.close()


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
        "backend": "test doubles (NOT real manifest, FFmpeg/libass, physical-device or final-media validation)",
        "screenshots": [],
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                desktop_check(browser, url, args.output, report)
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
