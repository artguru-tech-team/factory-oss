// Playwright check for the Board view (Automation → Board sub-tab).
// At 390 and 1440 px: no console errors, no page horizontal scroll, the six columns render,
// the card drawer shows the "Issue ↗" link and a Description box, the on-card Promote moves the
// card to Triage without opening the drawer, and a reload keeps it in Triage (via GET /api/board).
// Then the error state (with cached cards) shows the banner above the cards and Retry fetches
// fresh data; the empty state still shows the auto-promote toggle; "Only stuck" gets its pressed
// style. Screenshots land in docs/screenshots/board-*.png.

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

const setupScript = path.join(__dirname, "setup_board_browser.py");
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
const { url, bin, env } = serverInfo;
console.log(`Test environment ready at ${url}`);

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const problems = [];

function spawnBuild(extraEnv) {
  return new Promise((resolve, reject) => {
    const p = spawn("python3", [bin, "build"], {
      cwd: repoRoot, env: { ...env, ...extraEnv },
      stdio: ["ignore", "ignore", "inherit"],
    });
    p.on("exit", code => (code === 0 ? resolve() : reject(new Error(`rebuild exited ${code}`))));
  });
}

async function newPage(viewport) {
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on("console", m => {
    if (m.type() === "error" && !m.text().includes("violates the following Content Security Policy")) {
      errors.push(`console error: ${m.text()}`);
    }
  });
  page.on("pageerror", e => errors.push(`page error: ${e.message}`));
  return { page, errors };
}

async function cardColumn(page, id) {
  return page.evaluate((cid) => {
    const card = document.querySelector(`[data-bcard="${cid}"]`);
    if (!card) return null;
    const panel = card.closest(".panel");
    const h = panel && panel.querySelector(".panel-h h2");
    return h ? h.textContent.trim() : null;
  }, id);
}

async function waitForCardColumn(page, id, col, timeout = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const c = await cardColumn(page, id);
    if (c && c.startsWith(col)) return true;
    await new Promise(r => setTimeout(r, 250));
  }
  return false;
}

async function checkLoadedView(name, viewport, { interact }) {
  const { page, errors } = await newPage(viewport);
  await page.goto(url + "#/bk/automation", { waitUntil: "networkidle" });
  await page.waitForSelector(".board-grid", { timeout: 10000 });
  await page.waitForTimeout(300);

  const hscroll = await page.evaluate(() => {
    const el = document.scrollingElement;
    return el.scrollWidth > el.clientWidth + 1;
  });
  if (hscroll) errors.push("horizontal page scroll detected");

  const cards = await page.locator(".bcard").count();
  if (cards < 6) errors.push(`expected >=6 board cards, saw ${cards}`);

  await page.screenshot({ path: `${outDir}/board-${name}.png`, fullPage: true });

  if (interact) {
    // drawer: Issue ↗ link + Description box
    await page.click('[data-bcard="o/r#142"]');
    await page.waitForSelector("#boardDrawer[open]", { timeout: 5000 });
    const issueLink = await page.locator("#boardIssueLink").isVisible();
    const linkHref = await page.locator("#boardIssueLink").getAttribute("href");
    if (!issueLink) errors.push("drawer Issue ↗ link not visible");
    if (!linkHref || !linkHref.includes("/issues/142")) errors.push(`drawer Issue ↗ link wrong href: ${linkHref}`);
    const desc = await page.evaluate(() => document.querySelector("#boardDwSlide").textContent);
    if (!desc.includes("Description")) errors.push("drawer missing Description box");
    if (!desc.includes("postal code")) errors.push("drawer Description box missing issue body");
    await page.screenshot({ path: `${outDir}/board-${name}-drawer.png` });
    await page.click("#boardDwClose");

    // on-card Promote: moves to Triage without opening the drawer
    await page.locator('[data-bcard="o/r#142"] [data-bpromote]').click();
    await page.waitForFunction(() => ((document.getElementById("toast") || {}).textContent || "").includes("Promoted to triage"), { polling: 100, timeout: 10000 });
    const drawerOpen = await page.evaluate(() => !!document.querySelector("#boardDrawer[open]"));
    if (drawerOpen) errors.push("on-card Promote opened the drawer (it must not)");

    // reload: GET /api/board must keep the card in Triage
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForSelector(".board-grid", { timeout: 10000 });
    const triaged = await waitForCardColumn(page, "o/r#142", "Triage");
    if (!triaged) errors.push("after reload the promoted card is not in Triage (GET /api/board)");
  }

  if (errors.length) problems.push(...errors.map(e => `[${name}] ${e}`));
  else console.log(`[PASS] ${name} (${viewport.width}x${viewport.height})`);
  await page.close();
}

async function checkStuckStyle() {
  const { page, errors } = await newPage({ width: 390, height: 844 });
  await page.goto(url + "#/bk/automation", { waitUntil: "networkidle" });
  await page.waitForSelector(".board-grid", { timeout: 10000 });
  await page.click("[data-bstuck]");
  await page.waitForTimeout(300);
  const style = await page.evaluate(() => {
    const b = document.querySelector("[data-bstuck]");
    const cs = getComputedStyle(b);
    return { bg: cs.backgroundColor, color: cs.color, pressed: b.getAttribute("aria-pressed") };
  });
  if (style.pressed !== "true") errors.push(`Only stuck aria-pressed=${style.pressed}, want true`);
  if (style.bg !== "rgb(255, 255, 255)") errors.push(`Only stuck paper background not applied: ${style.bg}`);
  if (style.color !== "rgb(8, 9, 10)") errors.push(`Only stuck void text not applied: ${style.color}`);
  await page.screenshot({ path: `${outDir}/board-390.png`, fullPage: true });
  if (errors.length) problems.push(...errors.map(e => `[stuck] ${e}`));
  else console.log("[PASS] only-stuck pressed style (390)");
  await page.close();
}

async function checkErrorState() {
  const { page, errors } = await newPage({ width: 1440, height: 900 });
  await spawnBuild({ FACTORY_BOARD_FAIL: "1" });
  await page.goto(url + "#/bk/automation", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);

  // innerText (not textContent) so the inline <script> source does not count as page text
  const shown = await page.evaluate(() => document.body.innerText.includes("GitHub unreachable"));
  if (!shown) errors.push("error state did not show 'GitHub unreachable'");
  const lastGood = await page.evaluate(() => /Last good data \d\d:\d\d/.test(document.body.innerText));
  if (!lastGood) errors.push("error state did not show a 'Last good data HH:MM' time");
  const cached = await page.locator(".bcard").count();
  if (cached < 1) errors.push("error state should still show the cached cards, saw 0");
  await page.screenshot({ path: `${outDir}/board-error-1440.png`, fullPage: true });

  // Retry runs collect now (the server's gh is not failing) and re-renders fresh cards
  await page.click("[data-board-retry]");
  try {
    await page.waitForFunction(() => !document.body.innerText.includes("GitHub unreachable"), { polling: 100, timeout: 10000 });
  } catch (_) {}
  const stillError = await page.evaluate(() => document.body.innerText.includes("GitHub unreachable"));
  if (stillError) errors.push("Retry did not clear the error state");
  const fresh = await page.locator(".bcard").count();
  if (fresh < 6) errors.push(`Retry did not fetch fresh cards, saw ${fresh}`);

  if (errors.length) problems.push(...errors.map(e => `[error] ${e}`));
  else console.log("[PASS] error state + Retry fetch");
  await page.close();
}

async function checkEmptyState() {
  const { page, errors } = await newPage({ width: 1440, height: 900 });
  await spawnBuild({ FACTORY_BOARD_EMPTY: "1" });
  await page.goto(url + "#/bk/automation", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);

  const empty = await page.evaluate(() => document.body.innerText.includes("No factory issues"));
  if (!empty) errors.push("empty state did not show 'No factory issues'");
  const toggle = await page.locator("[data-bautopromote]").count();
  if (toggle < 1) errors.push("empty state did not show the auto-promote toggle");
  await page.screenshot({ path: `${outDir}/board-empty-1440.png`, fullPage: true });

  if (errors.length) problems.push(...errors.map(e => `[empty] ${e}`));
  else console.log("[PASS] empty state shows auto-promote toggle");
  await page.close();
}

try {
  await checkLoadedView("1440", { width: 1440, height: 900 }, { interact: true });
  await checkStuckStyle();
  await checkErrorState();
  await checkEmptyState();

  console.log("\n===== Board browser summary =====");
  if (problems.length) {
    console.error(`\nFailed with ${problems.length} problem(s):\n` + problems.join("\n"));
    process.exit(1);
  }
  console.log("\nBoard browser checks PASSED.");
} finally {
  await browser.close();
  cleanup();
}
