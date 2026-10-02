import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

function arg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : fallback;
}
const baseUrl = arg("--base-url", "http://127.0.0.1:8765");
const captureId = arg("--capture");
const output = arg("--output");
const metadataOutput = arg("--metadata-output");
if (!captureId || !output) process.exit(2);

const contract = JSON.parse(await fs.readFile(new URL("./ui-visual-capture.json", import.meta.url), "utf8"));
const capture = contract.captures.find((x) => x.id === captureId);
if (!capture) throw new Error("Unknown capture id: " + captureId);

const viewport = {
  width: capture.viewport?.width ?? 1365,
  height: capture.viewport?.height ?? 900,
  deviceScaleFactor: capture.viewport?.device_scale_factor ?? 1,
};
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    deviceScaleFactor: viewport.deviceScaleFactor,
    locale: capture.locale ?? "zh-CN",
    colorScheme: capture.color_scheme ?? "dark",
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  const targetUrl = new URL(capture.route ?? "/", baseUrl).toString();
  await page.goto(targetUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForFunction(() => typeof window.showWorkspace === "function", null, { timeout: 30000 });
  await page.evaluate(({ workspace, theme, selector }) => {
    if (typeof window.applyTheme === "function") window.applyTheme(theme, { persist: false });
    window.showWorkspace(workspace, { scroll: false });
    const target = selector ? document.querySelector(selector) : null;
    if (target) {
      target.classList.remove("hidden", "ui-collapsed");
      target.removeAttribute("hidden");
      target.setAttribute("data-uigs-capture-visible", "true");
    }
  }, { workspace: capture.fixture?.workspace ?? "project", theme: capture.color_scheme ?? "dark", selector: capture.selector ?? null });
  const ready = capture.ready ?? {};
  if (ready.selector) await page.locator(ready.selector).first().waitFor({ state: ready.state ?? "visible", timeout: 30000 });
  if (ready.settle_ms) await page.waitForTimeout(ready.settle_ms);
  await fs.mkdir(path.dirname(output), { recursive: true });
  if (capture.capture_region === "element") {
    await page.locator(capture.selector).first().screenshot({ path: output, animations: "disabled" });
  } else if (capture.capture_region === "full-page") {
    await page.screenshot({ path: output, fullPage: true, animations: "disabled" });
  } else {
    await page.screenshot({ path: output, fullPage: false, animations: "disabled" });
  }
  if (metadataOutput) {
    await fs.mkdir(path.dirname(metadataOutput), { recursive: true });
    await fs.writeFile(metadataOutput, JSON.stringify({
      schema_version: 1,
      capture_id: capture.id,
      surface_ids: capture.surface_ids,
      adapter: capture.adapter,
      evidence_level: capture.evidence_level,
      browser: { name: "chromium", version: browser.version() },
      target_url: targetUrl,
      viewport,
      color_scheme: capture.color_scheme ?? "dark",
      locale: capture.locale ?? "zh-CN",
      selector: capture.selector ?? null,
      fixture: capture.fixture ?? null,
      page_errors: pageErrors,
    }, null, 2) + "\n");
  }
} finally {
  await browser.close();
}
