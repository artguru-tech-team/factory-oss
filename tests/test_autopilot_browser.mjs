// Playwright check for the Autopilot (Automation) view.
// At 390 and 1440 px: no console/page errors, no horizontal scroll, the projects table renders,
// the toggle POSTs, the Add-project drawer refuses an empty goal, and Pause all shows the banner.
// Screenshots land in docs/screenshots/autopilot-*.png.

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const playwrightPath = process.env.PLAYWRIGHT;
if (!playwrightPath) {
  console.error("Set PLAYWRIGHT to the path of playwright/index.mjs");
  process.exit(2);
}
const { chromium } = await import(playwrightPath);

const outDir = path.resolve(repoRoot, "docs", "screenshots");
if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });

const setupScript = path.join(__dirname, "setup_autopilot_browser.py");
const serverProc = spawn("python3", [setupScript], {
  cwd: repoRoot,
  env: { ...process.env, PYTHONPATH: repoRoot },
  stdio: ["ignore", "pipe", "inherit"],
});

let serverInfo = null;
const infoPromise = new Promise((resolve, reject) => {
  let buf = "";
  serverProc.stdout.on("data", chunk => {
    buf += chunk.toString();
    const idx = buf.indexOf("\n");
    if (idx !== -1) {
      try { resolve(JSON.parse(buf.slice(0, idx))); } catch (err) { reject(err); }
    }
  });
  serverProc.on("error", reject);
  serverProc.on("exit", code => { if (code !== 0 && code !== null) reject(new Error(`setup exited ${code}`)); });
});
const cleanup = () => { try { serverProc.kill("SIGTERM"); } catch (_) {} };
process.on("exit", cleanup);
process.on("SIGINT", () => { cleanup(); process.exit(1); });
process.on("SIGTERM", () => { cleanup(); process.exit(1); });

try { serverInfo = await infoPromise; } catch (err) {
  console.error("Failed to start test environment:", err);
  cleanup();
  process.exit(1);
}
const { url, factoryHome } = serverInfo;
console.log(`Test environment ready at ${url} (FACTORY_HOME=${factoryHome})`);

const browser = await chromium.launch();
const problems = [];
const viewResults = [];

async function runView(name, viewport, interact) {
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on("console", m => {
    if (m.type() === "error" && !m.text().includes("violates the following Content Security Policy")) {
      errors.push(`console error: ${m.text()}`);
    }
  });
  page.on("pageerror", e => errors.push(`page error: ${e.message}`));

  await page.goto(url + "#/bk/automation", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-ap-add]", { timeout: 10000 });

  const hscroll = await page.evaluate(() => {
    const el = document.scrollingElement;
    return el.scrollWidth > el.clientWidth + 1;
  });
  if (hscroll) errors.push("horizontal scroll detected");

  const rows = await page.locator("tbody tr").count();
  if (rows < 2) errors.push(`expected >=2 autopilot rows, saw ${rows}`);

  const shot = `${outDir}/autopilot-${name}.png`;
  await page.screenshot({ path: shot, fullPage: true });

  if (interact) {
    // Toggle the first project off then on; each click must POST without error.
    const tg = page.locator("[data-ap-toggle]").first();
    const before = await tg.isChecked();
    await tg.click();
    await page.waitForTimeout(400);
    const afterOff = await page.locator("[data-ap-toggle]").first().isChecked();
    if (afterOff !== !before) errors.push(`toggle did not flip (before=${before}, after=${afterOff})`);
    await page.locator("[data-ap-toggle]").first().click();
    await page.waitForTimeout(400);

    // Add drawer: an empty goal must be refused with the inline error.
    await page.click("[data-ap-add]");
    await page.waitForSelector("#apDrawer[open]", { timeout: 5000 });
    await page.click("[data-ap-add-save]");
    await page.waitForTimeout(200);
    const errShown = await page.evaluate(() => {
      const e = document.getElementById("apAddGoalErr");
      return e && e.style.display !== "none" && e.textContent.includes("required");
    });
    if (!errShown) errors.push("add drawer did not refuse an empty goal");
    await page.screenshot({ path: `${outDir}/autopilot-${name}-add-validation.png` });

    // A goal draft survives a re-render; the draft pauses refresh only while the drawer is open.
    await page.fill("#apAddGoal", "draft goal text");
    await page.evaluate(() => render());
    const draftSurvived = await page.evaluate(() => {
      const dw = document.getElementById("apDrawer");
      const g = document.getElementById("apAddGoal");
      return Boolean(dw && dw.open && g && g.value === "draft goal text" && hasMsgDraft());
    });
    if (!draftSurvived) errors.push("add drawer draft did not survive a re-render");

    await page.click("[data-ap-drawer-close]");
    const refreshResumed = await page.evaluate(() => !hasMsgDraft());
    if (!refreshResumed) errors.push("auto-refresh did not resume after closing the drawer");

    // Pause all: the banner with a single Resume must appear, "All automations paused at HH:MM".
    await page.click("[data-ap-pause]");
    await page.waitForTimeout(400);
    const paused = await page.evaluate(() => {
      const b = document.querySelector(".banner.stop");
      const oneResume = document.querySelectorAll("[data-ap-resume]").length === 1;
      return Boolean(b) && /All automations paused at \d\d:\d\d/.test(b.textContent) && oneResume;
    });
    if (!paused) errors.push("pause banner wording/Resume did not match");
    await page.screenshot({ path: `${outDir}/autopilot-${name}-paused.png` });
  }

  viewResults.push({ view: name, viewport: `${viewport.width}x${viewport.height}`, errors, screenshot: shot });
  if (errors.length) problems.push(...errors.map(e => `[${name}] ${e}`));
  else console.log(`[PASS] ${name} (${viewport.width}x${viewport.height})`);
  await page.close();
}

try {
  await runView("1440", { width: 1440, height: 900 }, true);
  await runView("390", { width: 390, height: 844 }, false);

  console.log("\n===== Autopilot browser summary =====");
  for (const r of viewResults) {
    console.log(`- ${r.view} (${r.viewport}): ${r.errors.length ? "FAIL" : "PASS"} · ${r.screenshot}`);
  }
  if (problems.length) {
    console.error(`\nFailed with ${problems.length} problem(s):\n` + problems.join("\n"));
    process.exit(1);
  }
  console.log("\nAutopilot browser checks PASSED.");
} finally {
  await browser.close();
  cleanup();
}
