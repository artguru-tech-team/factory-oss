import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const playwrightPath = process.env.PLAYWRIGHT || "/usr/local/lib/python3.12/dist-packages/playwright/driver/package/index.mjs";
const { chromium } = await import(playwrightPath);

const screenshotDir = process.env.SCREENSHOT_DIR || path.join(os.tmpdir(), "factory-scratchpad");
fs.mkdirSync(screenshotDir, { recursive: true });

const procs = [];
const cleanup = () => procs.forEach(p => { try { p.kill("SIGTERM"); } catch (_) {} });
process.on("exit", cleanup);
process.on("SIGINT", () => { cleanup(); process.exit(1); });

// Start the seeded server (setup_release_browser.py) and resolve with its first JSON line.
function startServer(...args) {
  const proc = spawn("python3", [path.join(__dirname, "setup_release_browser.py"), ...args], {
    cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, stdio: ["ignore", "pipe", "inherit"],
  });
  procs.push(proc);
  return new Promise((resolve, reject) => {
    let buf = "";
    proc.stdout.on("data", c => { buf += c; const i = buf.indexOf("\n"); if (i !== -1) resolve(JSON.parse(buf.slice(0, i))); });
    proc.on("error", reject);
    proc.on("exit", code => { if (code) reject(new Error(`setup exited ${code}`)); });
  });
}

const problems = [];
const check = (ok, msg) => { if (ok) console.log("[PASS] " + msg); else { problems.push(msg); console.error("[FAIL] " + msg); } };
const shot = (page, name, opts = {}) => page.screenshot({ path: path.join(screenshotDir, name), fullPage: true, ...opts });
const browser = await chromium.launch({ args: ["--no-sandbox"] });

async function open(url, viewport) {
  const page = await browser.newPage({ viewport });
  page.on("dialog", d => d.accept());
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  page.on("console", m => { if (m.type() === "error" && !m.text().includes("Content Security Policy") && !m.text().includes("Failed to load resource")) errors.push(m.text()); });
  await page.goto(url + "#/all/automation", { waitUntil: "domcontentloaded" });
  return { page, errors };
}

try {
  const { url } = await startServer();
  const base = url.split("/?")[0], key = url.split("k=")[1];
  const api = () => fetch(`${base}/api/release?k=${key}`).then(r => r.json());

  // ---- desktop: loading, default, hold, read error
  const { page, errors } = await open(url, { width: 1280, height: 900 });
  // hold the first GET until the skeleton has been seen, so the check cannot race the reply
  let release; const held = new Promise(x => { release = x; });
  await page.route("**/api/release*", async r => { await held; await r.continue(); });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".skel-box", { timeout: 30000 });
  check(true, "skeleton shows while /api/release is slow");
  release();
  await page.waitForSelector("[data-rl-card]", { timeout: 30000 });
  await page.unrouteAll({ behavior: "wait" });

  const tabs = await page.$$eval("[data-autotab]", els => els.map(e => [e.dataset.autotab, e.getAttribute("aria-selected")]));
  check(tabs[0][0] === "releases" && tabs[0][1] === "true", `Releases is the first and default tab (${JSON.stringify(tabs[0])})`);
  check(await page.$$eval("[data-rl-card]", e => e.length) === 3, "one card per product");
  await shot(page, "01-default.png");

  const web = page.locator('[data-rl-card="web"]'), app = page.locator('[data-rl-card="app"]'), store = page.locator('[data-rl-card="store"]');
  check((await web.textContent()).includes("Solo red") && (await web.textContent()).includes("Ships #1289"), "web card shows its verdict and what ships");
  check((await web.textContent()).includes("Held #1292") && (await web.textContent()).includes("shipped"), "web card shows held PR reason and last journal events");
  check((await web.textContent()).includes("9c01bf2") && !(await web.textContent()).includes("9c01bf2a"), "short sha");
  check((await web.locator(".rl-mode").textContent()) === "live" && (await store.locator(".rl-mode").textContent()) === "shadow", "mode badges");

  check((await app.textContent()).includes("Rollback hold") && (await app.textContent()).includes("until it can be lifted"), "hold card shows the time left");
  check(await app.locator("[data-rl-unhold]").isDisabled(), "Unhold is disabled before 24h");
  await app.screenshot({ path: path.join(screenshotDir, "02-hold-unhold-disabled.png") });
  check(await app.locator("[data-rl-unhold]").getAttribute("disabled") !== null, "Unhold carries the disabled attribute, so the browser never sends the click");

  const st = await store.textContent();
  check(st.includes("GitHub read error") && st.includes("502 Bad Gateway"), "read error is shown on its card");
  check((await web.textContent()).includes("Solo red"), "the other cards still render normally");

  // ---- Unhold refused by the server (button forced on): the error text reaches the user
  await page.route("**/api/release*", async r => {
    if (r.request().method() !== "GET") return r.continue();
    const b = await (await r.fetch()).json();
    b.products.app.hold.remaining_hours = 0;
    await r.fulfill({ json: b });
  });
  await page.evaluate(() => loadReleaseFromServer());
  await page.waitForFunction(() => !document.querySelector("[data-rl-unhold]").disabled);
  await app.locator("[data-rl-unhold]").click();
  await page.waitForSelector(".toast");
  check((await page.textContent(".toast")).includes("under 24h old"), "a refused unhold shows the server's reason");
  await page.unrouteAll({ behavior: "wait" });
  await page.evaluate(() => loadReleaseFromServer());
  await page.waitForFunction(() => document.querySelector("[data-rl-unhold]").disabled);

  // ---- pause / resume one product
  await web.locator("[data-rl-pause]").click();
  await page.waitForSelector('[data-rl-card="web"] [data-rl-resume]');
  check((await web.textContent()).includes("Paused"), "Pause marks the card paused");
  check((await api()).products.web.paused === true, "the pause is saved on the server");
  check(!(await app.textContent()).includes("Paused"), "other products are not paused");
  await shot(page, "03-web-paused.png");
  await web.locator("[data-rl-resume]").click();
  await page.waitForSelector('[data-rl-card="web"] [data-rl-pause]');
  check((await api()).products.web.paused === false, "Resume clears the pause");

  // ---- pause all / resume all
  await page.click('[data-rl-pause="*"]');
  await page.waitForSelector(".rl-banner");
  check((await page.$$eval("[data-rl-card] .rl-head button", b => b.every(x => x.disabled))), "while all are paused the per-product buttons are disabled");
  check((await page.textContent('[data-rl-resume="*"]')).trim() === "Resume all" && (await api()).paused === true, "Pause all saved, button becomes Resume all");
  await shot(page, "04-pause-all.png");
  await page.click('[data-rl-resume="*"]');
  await page.waitForSelector(".rl-banner", { state: "detached" });
  check((await api()).paused === false, "Resume all clears it");

  // ---- error state with Retry
  await page.route("**/api/release*", r => r.fulfill({ status: 500, json: { error: "boom" } }));
  await page.evaluate(() => { DATA.release = null; loadReleaseFromServer(); });
  await page.waitForSelector("#rl-error");
  check((await page.textContent("#rl-error")).includes("boom"), "error state shows the message");
  await shot(page, "05-error.png");
  await page.unrouteAll({ behavior: "wait" });
  await page.click("[data-rl-retry]");
  await page.waitForSelector("[data-rl-card]");
  check(true, "Retry reloads the cards");
  check(errors.length === 0, `no console or page errors (${errors.join(" | ")})`);
  await page.close();

  // ---- first load fails: the error stays on screen and the page does not refetch in a loop
  const { page: f, errors: ferr } = await open(url, { width: 1280, height: 700 });
  let gets = 0;
  await f.route("**/api/release*", r => { gets++; return r.fulfill({ status: 500, json: { error: "down" } }); });
  await f.reload({ waitUntil: "domcontentloaded" });
  await f.waitForSelector("#rl-error");
  await f.waitForTimeout(1500);
  check(gets === 1 && await f.$("#rl-error") !== null, `first-load failure shows the error once, no refetch loop (${gets} GETs)`);
  await f.close();

  // ---- cross-product journal on the page
  check((await api()).journal.length > 0, "the API returns the global journal");
  const lp = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await lp.goto(url + "#/all/automation", { waitUntil: "domcontentloaded" });
  await lp.waitForSelector("[data-rl-card]");
  check((await lp.textContent("body")).includes("Release journal: last"), "the cross-product journal panel renders");
  await lp.close();

  // ---- mobile
  const { page: m } = await open(url, { width: 390, height: 844 });
  await m.waitForSelector("[data-rl-card]");
  check(!(await m.evaluate(() => document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth + 1)), "no horizontal scroll at 390px");
  await shot(m, "06-mobile-390.png");
  await m.close();

  // ---- empty: no products configured
  const empty = await startServer("empty");
  const { page: e } = await open(empty.url, { width: 1280, height: 700 });
  await e.waitForSelector(".rl-state");
  check((await e.textContent(".rl-state")).includes("No products configured"), "empty state");
  check(await e.$$eval("[data-rl-card]", x => x.length) === 0, "no cards when nothing is configured");
  await shot(e, "07-empty.png");
  await e.close();
} finally {
  await browser.close();
  cleanup();
}

if (problems.length) { console.error("FAILURES:\n - " + problems.join("\n - ")); process.exit(1); }
console.log("All Releases browser checks PASSED.");
