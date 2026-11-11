// Playwright browser test for Roadmap view.
// Covers:
// - Drag into full Now -> swap modal -> Swap -> order and toast -> Undo restores
// - 390 px width has no horizontal scroll
// - Zero console errors
// - Screenshots at 1440 and 390 px saved next to test output (docs/screenshots/rm-*.png)

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const playwrightPath = process.env.PLAYWRIGHT;
if (!playwrightPath) {
  console.error("Set PLAYWRIGHT to the path of playwright/index.mjs");
  process.exit(2);
}
let resolvedPlaywright = playwrightPath;
if (!resolvedPlaywright.endsWith(".mjs") && !resolvedPlaywright.endsWith(".js")) {
  resolvedPlaywright = path.join(resolvedPlaywright, "index.mjs");
}

const { chromium } = await import(resolvedPlaywright);

const outDir = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "roadmap-shots-"));  // never overwrite the committed screenshots
if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });

const setupScript = path.join(__dirname, "setup_roadmap_browser.py");
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

try {
  serverInfo = await infoPromise;
} catch (err) {
  console.error("Failed to start test environment:", err);
  cleanup();
  process.exit(1);
}
const { url } = serverInfo;
console.log(`Test environment ready at ${url}`);

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const problems = [];

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

// 1. Desktop test at 1440x900
async function testDesktop() {
  const { page, errors } = await newPage({ width: 1440, height: 900 });
  await page.goto(url + "#/bk/roadmap", { waitUntil: "networkidle" });
  await page.waitForSelector(".board", { timeout: 10000 });
  await page.waitForTimeout(300);

  // Check 4 lanes exist
  const lanesCount = await page.locator(".lane").count();
  if (lanesCount !== 4) errors.push(`expected 4 lanes, saw ${lanesCount}`);

  // Check Now has 7 cards
  const nowCards = await page.locator('[data-lane="now"] .rc').count();
  if (nowCards !== 7) errors.push(`expected 7 Now cards, saw ${nowCards}`);

  // Drag card FEAT-10 (in Later) to Now lane
  const laterCard = page.locator('.rc[data-id="FEAT-10"]');
  const nowLane = page.locator('.lane[data-lane="now"]');

  // Trigger drag and drop using Playwright dragTo
  await laterCard.dragTo(nowLane);
  await page.waitForTimeout(500);

  // Check Swap modal is open
  const modalVisible = await page.locator('.modal[role="dialog"]').isVisible();
  if (!modalVisible) errors.push("Now is full modal did not open on drag into full Now");

  // Pick FEAT-2 to swap out
  await page.click('input[name="out"][value="FEAT-2"]');
  const swapBtn = page.locator("#doSwap");
  const isEnabled = await swapBtn.isEnabled();
  if (!isEnabled) errors.push("Swap button should be enabled after radio selection");

  // Click Swap
  await swapBtn.click();
  await page.waitForTimeout(600);

  // Check FEAT-10 is now in Now
  const feat10InNow = await page.locator('[data-lane="now"] .rc[data-id="FEAT-10"]').count();
  if (feat10InNow !== 1) errors.push("FEAT-10 is not in Now after Swap");

  // Check FEAT-2 is in Next
  const feat2InNext = await page.locator('[data-lane="next"] .rc[data-id="FEAT-2"]').count();
  if (feat2InNext !== 1) errors.push("FEAT-2 is not in Next after Swap");

  // Check toast with Undo is visible
  const toastText = await page.locator("#toast").textContent();
  if (!toastText.includes("Swapped")) errors.push(`toast missing 'Swapped', got: ${toastText}`);
  const undoBtn = page.locator('#toast [data-act="undo"]');
  const hasUndo = await undoBtn.isVisible();
  if (!hasUndo) errors.push("Toast missing Undo button");

  // Save 1440 screenshot
  const shot1440 = path.join(outDir, "rm-1440.png");
  await page.screenshot({ path: shot1440, fullPage: true });
  console.log(`Saved 1440 screenshot: ${shot1440}`);

  // Click Undo
  await undoBtn.click();
  await page.waitForTimeout(800);

  // Check restored: FEAT-10 back in Later, FEAT-2 back in Now
  const feat10Restored = await page.locator('[data-lane="later"] .rc[data-id="FEAT-10"]').count();
  if (feat10Restored !== 1) errors.push("FEAT-10 not restored to Later after Undo");
  const feat2Restored = await page.locator('[data-lane="now"] .rc[data-id="FEAT-2"]').count();
  if (feat2Restored !== 1) errors.push("FEAT-2 not restored to Now after Undo");

  if (errors.length) problems.push(...errors.map(e => `[1440] ${e}`));
  else console.log("[PASS] Desktop 1440x900 drag, swap modal, swap, toast, undo, zero console errors");
  await page.close();
}

// 2. Mobile test at 390x844
async function testMobile() {
  const { page, errors } = await newPage({ width: 390, height: 844 });
  await page.goto(url + "#/bk/roadmap", { waitUntil: "networkidle" });
  await page.waitForSelector(".board", { timeout: 10000 });
  await page.waitForTimeout(300);

  // Check horizontal page scroll
  const hscroll = await page.evaluate(() => {
    const el = document.scrollingElement;
    return el.scrollWidth > el.clientWidth + 1;
  });
  if (hscroll) errors.push("horizontal page scroll detected on 390 px");

  // Check mobile lane tabs are visible
  const lanetabsVisible = await page.locator(".lanetabs").isVisible();
  if (!lanetabsVisible) errors.push("mobile lane tabs not visible at 390 px");

  // Save 390 screenshot
  const shot390 = path.join(outDir, "rm-390.png");
  await page.screenshot({ path: shot390, fullPage: true });
  console.log(`Saved 390 screenshot: ${shot390}`);

  if (errors.length) problems.push(...errors.map(e => `[390] ${e}`));
  else console.log("[PASS] Mobile 390 px no horizontal scroll, lane tabs, zero console errors");
  await page.close();
}

// 3. One click = one handler run; a Notion 502 then "Try again" really resends the move.
async function testSingleClickAndRetry() {
  const { page, errors } = await newPage({ width: 1440, height: 900 });
  await page.goto(url + "#/bk/roadmap", { waitUntil: "networkidle" });
  await page.waitForSelector(".board", { timeout: 10000 });
  await page.evaluate(() => { window.__menuRuns = 0; const o = roadMenu; roadMenu = (...a) => { window.__menuRuns++; return o(...a); }; });
  await page.click('.rc[data-id="FEAT-8"] [data-menu]');
  const runs = await page.evaluate(() => window.__menuRuns);
  if (runs !== 1) errors.push(`one click on ⋯ ran the menu handler ${runs} times`);
  await page.keyboard.press("Escape");

  let posts = 0;
  await page.route("**/api/roadmap*", async route => {
    if (route.request().method() !== "POST") return route.continue();
    posts++;
    if (posts === 1) return route.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ error: "Notion HTTP 502" }) });
    return route.continue();
  });
  await page.click('.rc[data-id="FEAT-8"] [data-menu]');
  await page.click('.menu [data-to="later"]');
  await page.waitForSelector('#toast [data-act="retry"]', { timeout: 3000 });
  if (await page.locator('[data-lane="later"] .rc[data-id="FEAT-8"]').count()) errors.push("card moved although Notion answered 502");
  await page.click('#toast [data-act="retry"]');
  await page.waitForTimeout(600);
  if (posts !== 2) errors.push(`Try again sent ${posts - 1} request(s), expected 1`);
  if (await page.locator('[data-lane="later"] .rc[data-id="FEAT-8"]').count() !== 1) errors.push("card not in Later after Try again");

  const real = errors.filter(e => !e.includes("status of 502"));  // the 502 above is simulated on purpose
  if (real.length) problems.push(...real.map(e => `[retry] ${e}`));
  else console.log("[PASS] one click runs one handler; 502 then Try again resends and moves the card");
  await page.close();
}

// 4. Injection & toast security (C4):
// - card title <img src=x id=pwn> shows as text in card & swap toast; no #pwn element in DOM
// - non-roadmap caller of toast shows <b>x</b> as text without leaking HTML
async function testInjectionAndToast() {
  const { page, errors } = await newPage({ width: 1440, height: 900 });
  await page.goto(url + "#/bk/roadmap", { waitUntil: "networkidle" });
  await page.waitForSelector(".board", { timeout: 10000 });

  // Add a card with XSS payload in title in Later lane
  const inp = page.locator('[data-lane="later"] .addrow input');
  await inp.fill("<img src=x id=pwn>");
  await inp.press("Enter");
  await page.waitForTimeout(600);

  // Check that no #pwn element was created in DOM
  const pwnInDom = await page.locator("#pwn").count();
  if (pwnInDom !== 0) errors.push("XSS injection: #pwn element found in DOM after adding card");

  // Verify the card text contains the literal string <img src=x id=pwn>
  const xssCard = page.locator('[data-lane="later"] .rc').filter({ hasText: "<img src=x id=pwn>" });
  if ((await xssCard.count()) === 0) errors.push("Card with <img src=x id=pwn> text not found in Later");

  // Drag the card to Now (which is full with 7 cards) to trigger Swap modal
  const nowLane = page.locator('.lane[data-lane="now"]');
  await xssCard.first().dragTo(nowLane);
  await page.waitForSelector('.modal[role="dialog"]', { timeout: 5000 });

  // Pick FEAT-1 to swap out
  await page.click('input[name="out"][value="FEAT-1"]');
  await page.click("#doSwap");
  await page.waitForTimeout(600);

  // Check swap toast: must show <img src=x id=pwn> as text and NOT create #pwn
  const toastText = await page.locator("#toast").textContent();
  if (!toastText.includes("<img src=x id=pwn>")) {
    errors.push("swap toast missing literal <img src=x id=pwn>, got: " + toastText);
  }
  const pwnInToast = await page.locator("#pwn").count();
  if (pwnInToast !== 0) errors.push("XSS injection: #pwn element created by swap toast");

  // Test an existing non-roadmap caller of toast that prints server text
  // e.g., toast("Not saved: <b>x</b>") using toast() with default html=false
  await page.evaluate(() => {
    toast("Not saved: <b>x</b>");
  });
  await page.waitForTimeout(200);
  const bInToast = await page.locator("#toast b").count();
  if (bInToast !== 0) errors.push("non-roadmap toast rendered <b> as HTML element instead of plain text");
  const nrToastText = await page.locator("#toast").textContent();
  if (!nrToastText.includes("<b>x</b>")) {
    errors.push("non-roadmap toast missing literal <b>x</b>, got: " + nrToastText);
  }

  if (errors.length) problems.push(...errors.map(e => "[injection] " + e));
  else console.log("[PASS] C4: Card title & swap toast escape <img src=x id=pwn>; non-roadmap toast shows <b>x</b> as text");
  await page.close();
}

// 5. CSS must not leak into other views (D5):
// - on Home view (# or empty), stale-data .banner must NOT show display: flex
// - .status max-width must be 78ch
async function testHomeCssLeak() {
  const { page, errors } = await newPage({ width: 1440, height: 900 });
  await page.goto(url + "#", { waitUntil: "networkidle" });
  await page.waitForSelector(".status", { timeout: 10000 });

  // Add stale-data banner on home view to test its styling
  const bannerDisplay = await page.evaluate(() => {
    const el = document.createElement("div");
    el.className = "banner warn";
    el.textContent = "Data is 20 min old.";
    document.querySelector(".status").before(el);
    return getComputedStyle(el).display;
  });
  if (bannerDisplay === "flex") {
    errors.push("Home view .banner has display: flex (leaked from roadmap view)");
  }

  // Check .status rule and computed style
  const statusInfo = await page.evaluate(() => {
    const el = document.querySelector(".status");
    if (!el) return null;
    const ruleMw = Array.from(document.styleSheets)
      .flatMap(s => { try { return Array.from(s.cssRules); } catch(_) { return []; } })
      .filter(r => r.selectorText === ".status")
      .map(r => r.style.maxWidth)
      .pop();
    return { csMw: getComputedStyle(el).maxWidth, ruleMw };
  });

  if (!statusInfo) errors.push(".status element not found on Home view");
  else if (statusInfo.ruleMw !== "78ch") {
    errors.push(".status rule max-width is " + statusInfo.ruleMw + ", expected 78ch");
  }

  if (errors.length) problems.push(...errors.map(e => "[css-leak] " + e));
  else console.log("[PASS] D5: Home view .banner is not flex, and .status max-width is 78ch");
  await page.close();
}

// 6. Page state (E6):
// - Reopen the view after a server-side factory roadmap move (call API), card is in new lane
// - Drawer: change title and lane in one Save, and both apply
async function testReopenRoadmapAndDrawerSave() {
  const { page, errors } = await newPage({ width: 1440, height: 900 });
  await page.goto(url + "#/bk/roadmap", { waitUntil: "networkidle" });
  await page.waitForSelector(".board", { timeout: 10000 });

  // Verify FEAT-9 is in Next initially
  if ((await page.locator('[data-lane="next"] .rc[data-id="FEAT-9"]').count()) !== 1) {
    errors.push("FEAT-9 not in Next initially");
  }

  // Move FEAT-9 to Later on the server via API
  await page.evaluate(async () => {
    const res = await fetch("api/roadmap" + Q, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin" },
      body: JSON.stringify({ action: "move", id: "FEAT-9", lane: "later" }),
    });
    if (!res.ok) throw new Error("API move failed");
  });

  // Switch to Home view then back to Roadmap view
  await page.goto(url + "#/bk", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  await page.goto(url + "#/bk/roadmap", { waitUntil: "networkidle" });
  await page.waitForSelector(".board", { timeout: 10000 });
  await page.waitForTimeout(400);

  // Check FEAT-9 is now in Later after in-place sync
  const feat9InLater = await page.locator('[data-lane="later"] .rc[data-id="FEAT-9"]').count();
  if (feat9InLater !== 1) {
    errors.push("FEAT-9 not in Later after reopening roadmap view following server-side move");
  }

  // Test Drawer Save: change title and lane in one Save
  // Open drawer for FEAT-11 (currently in Later)
  const feat11 = page.locator('.rc[data-id="FEAT-11"]');
  await feat11.click();
  await page.waitForSelector(".rm-drawer", { timeout: 5000 });

  // Update title and lane
  const titleInput = page.locator('.rm-drawer #f-t');
  await titleInput.fill("Updated Feature 11 Title");
  const laneSelect = page.locator('.rm-drawer #f-l');
  await laneSelect.selectOption("next");

  // Click Save
  await page.click('.rm-drawer #f-save');
  await page.waitForSelector(".rm-drawer", { state: "detached", timeout: 5000 });
  await page.waitForTimeout(500);

  // Verify FEAT-11 is in Next with updated title
  const feat11InNext = await page.locator('[data-lane="next"] .rc[data-id="FEAT-11"]').count();
  if (feat11InNext !== 1) {
    errors.push("FEAT-11 not moved to Next by drawer Save");
  }
  const feat11Title = await page.locator('.rc[data-id="FEAT-11"] .nm').textContent();
  if (!feat11Title.includes("Updated Feature 11 Title")) {
    errors.push("FEAT-11 title not updated by drawer Save, got: " + feat11Title);
  }

  // Check server persistence
  const serverItems = await page.evaluate(async () => {
    const res = await fetch("api/roadmap" + Q + (Q ? "&" : "?") + "product=bk");
    const data = await res.json();
    return data.items || [];
  });
  const saved11 = serverItems.find(i => i.id === "FEAT-11");
  if (!saved11 || saved11.lane !== "next" || saved11.title !== "Updated Feature 11 Title") {
    errors.push("Server does not have updated FEAT-11: " + JSON.stringify(saved11));
  }

  if (errors.length) problems.push(...errors.map(e => "[reopen-drawer] " + e));
  else console.log("[PASS] E6: Reopening roadmap view syncs server changes; Drawer saves title and lane in one Save");
  await page.close();
}

try {
  await testDesktop();
  await testMobile();
  await testSingleClickAndRetry();
  await testInjectionAndToast();
  await testHomeCssLeak();
  await testReopenRoadmapAndDrawerSave();

  console.log("\n===== Roadmap browser test summary =====");
  if (problems.length) {
    console.error(`\nFailed with ${problems.length} problem(s):\n` + problems.join("\n"));
    process.exit(1);
  }
  console.log("\nRoadmap browser checks ALL PASSED.");
} finally {
  await browser.close();
  cleanup();
}
