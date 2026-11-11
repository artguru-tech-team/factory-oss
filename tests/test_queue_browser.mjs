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
if (!fs.existsSync(screenshotDir)) fs.mkdirSync(screenshotDir, { recursive: true });

const setupScript = path.join(__dirname, "setup_queue_browser.py");
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
const { url } = serverInfo;
console.log(`Test environment ready at ${url}`);

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const problems = [];

async function newPage(viewport, initTime = "2026-11-10T08:00:00+01:00") {
  const page = await browser.newPage({ viewport });
  if (initTime) {
    await page.addInitScript(t => { window.__NOW__ = t; }, initTime);
  }
  const errors = [];
  page.on("console", m => {
    if (m.type() === "error" && !m.text().includes("violates the following Content Security Policy")) {
      errors.push(`console error: ${m.text()}`);
    }
  });
  page.on("pageerror", e => errors.push(`page error: ${e.message}`));
  return { page, errors };
}

try {
  // ----------------------------------------------------
  // Test Suite 1: Desktop 1280px
  // ----------------------------------------------------
  console.log("Testing 1280px viewport...");
  const { page: page1280 } = await newPage({ width: 1280, height: 800 }, "2026-11-10T08:00:00+01:00");

  // Check: skeleton under a delayed /api/queue
  console.log("Testing skeleton under delayed /api/queue...");
  let delayQueue = true;
  await page1280.route("**/api/queue*", async route => {
    if (delayQueue) {
      await new Promise(r => setTimeout(r, 600));
    }
    await route.continue();
  });

  await page1280.goto(url + "#/all/automation", { waitUntil: "domcontentloaded" });
  await page1280.click('[data-autotab=\"develop-queue\"]');

  // Verify skeleton while loading
  const skCount = await page1280.$$eval(".sk", els => els.length).catch(() => 0);
  if (skCount > 0) {
    console.log(`[PASS] Skeleton rendered while loading (${skCount} skeleton bars)`);
  } else {
    // Wait briefly for skeleton if needed
    try {
      await page1280.waitForSelector(".sk", { timeout: 1000 });
      console.log("[PASS] Skeleton rendered while loading");
    } catch (_) {
      problems.push("Skeleton did not show while /api/queue was delayed");
    }
  }

  // Disable delay for subsequent requests
  delayQueue = false;
  await page1280.waitForSelector(".pipe", { timeout: 8000 });
  await page1280.unroute("**/api/queue*");

  // Check: normal screenshot 1280
  const shotNormal1280 = path.join(screenshotDir, "dqr1-normal-1280.png");
  await page1280.screenshot({ path: shotNormal1280, fullPage: true });
  console.log(`[PASS] Normal 1280 screenshot saved: ${shotNormal1280}`);

  // Check: short ids and flow labels come from the release config
  const rowIds = await page1280.$$eval(".row .id", els => els.map(e => e.textContent));
  if (!rowIds.some(t => /^(app|web|sto) #\d+/.test(t))) problems.push(`Queue row ids do not use configured short ids: ${rowIds.join(", ")}`);
  const flows = await page1280.$$eval(".tp span", els => els.map(e => e.textContent));
  if (!flows.includes("develop → main · 07:30")) problems.push(`Queue does not show the configured flow label: ${flows.join(", ")}`);
  if (!flows.includes("PR → main · 11:00")) problems.push("Queue does not show the factory flow label");

  // Check: .tk.ask and conditional tile (.tk.cond)
  const askTiles = await page1280.$$(".tk.ask");
  if (askTiles.length === 0) problems.push("Expected at least one .tk.ask tile (feature needing OK)");
  else console.log(`[PASS] .tk.ask tile found (${askTiles.length} tile(s))`);

  const condTiles = await page1280.$$(".tk.cond");
  if (condTiles.length === 0) problems.push("Expected at least one .tk.cond tile (review PR)");
  else console.log(`[PASS] .tk.cond tile found (${condTiles.length} tile(s))`);

  // Check: dependency batch tile
  const depsTile = await page1280.$('.tk[data-k="bots"]');
  if (!depsTile) {
    problems.push("Expected deps batch tile .tk[data-k='bots'] on Wednesday");
  } else {
    const text = await depsTile.textContent();
    if (!text.includes("deps") || !text.includes("bumps:")) {
      problems.push(`Deps tile text unexpected: ${text}`);
    } else {
      console.log(`[PASS] Deps tile renders correctly: "${text.trim()}"`);
    }

    // Clicking deps tile expands the Dependency batch group
    await depsTile.click();
    await page1280.waitForTimeout(200);
    const botsExpanded = await page1280.$eval('.gh[data-g="bots"]', el => el.getAttribute("aria-expanded"));
    if (botsExpanded !== "true") {
      problems.push(`Expected .gh[data-g='bots'] aria-expanded to be 'true' after clicking deps tile, got '${botsExpanded}'`);
    } else {
      console.log("[PASS] Clicking deps tile expanded Dependency batch group");
    }
  }

  // Check: today dot only Mon-Thu
  const todayCols = await page1280.$$eval(".th .today", els => els.length);
  if (todayCols !== 1) {
    problems.push(`Expected exactly 1 column highlighted with .today on Tuesday, found ${todayCols}`);
  } else {
    console.log("[PASS] Today dot highlighted on Tuesday (Mon-Thu)");
  }

  // Check weekend: no today dot on weekend
  console.log("Testing weekend mode (Sunday)...");
  await page1280.evaluate(() => {
    window.__NOW__ = "2026-11-15T10:00:00+01:00"; // Sunday
    render();
  });
  await page1280.waitForTimeout(200);
  const weekendTodayCols = await page1280.$$eval(".th .today", els => els.length);
  if (weekendTodayCols !== 0) {
    problems.push(`Expected 0 columns highlighted with .today on weekend, found ${weekendTodayCols}`);
  } else {
    console.log("[PASS] No today dot highlighted on weekend (Fri-Sun)");
  }
  // the snapshot was read on Tue 10 Nov, so the train keeps that week's dates on a later day
  const weekendHeader = await page1280.$$eval(".th div", els => els.slice(1).map(e => e.textContent.trim()));
  if (JSON.stringify(weekendHeader) !== JSON.stringify(["Mon 9", "Tue 10", "Wed 11", "Thu 12"])) {
    problems.push(`Weekend header should keep the snapshot's week, got ${JSON.stringify(weekendHeader)}`);
  } else {
    console.log("[PASS] Train keeps the snapshot's week (Mon 9 - Thu 12) when viewed on Sunday");
  }
  const shotWeekend1280 = path.join(screenshotDir, "dqr1-weekend-1280.png");
  await page1280.screenshot({ path: shotWeekend1280, fullPage: true });
  console.log(`[PASS] Weekend 1280 screenshot saved: ${shotWeekend1280}`);

  // Restore weekday
  await page1280.evaluate(() => {
    window.__NOW__ = "2026-11-10T08:00:00+01:00";
    render();
  });
  await page1280.waitForTimeout(200);

  // Check: Hold moves the row into "Held or closed" after the POST
  console.log("Testing Hold action...");
  const pr1392Row = await page1280.$('.row[data-k*="1392"]');
  if (!pr1392Row) {
    problems.push("Could not find PR row for #1392 to test Hold");
  } else {
    await pr1392Row.click();
    await page1280.waitForSelector("#panel .acts", { timeout: 3000 });
    const holdBtn = await page1280.$('.acts button[data-a="hold"]');
    if (!holdBtn) {
      problems.push("Could not find Hold button in panel");
    } else {
      await holdBtn.click();
      await page1280.waitForSelector("#toast", { timeout: 4000 });
      const toastText = await page1280.$eval("#toast", el => el.textContent.trim());
      if (toastText !== "Held. The train skips it.") {
        problems.push(`Expected toast 'Held. The train skips it.', got '${toastText}'`);
      } else {
        console.log(`[PASS] Hold action completed with toast: "${toastText}"`);
      }
      await page1280.waitForTimeout(500);

      // Verify row is now inside held group
      const inHeldGroup = await page1280.evaluate(() => {
        const heldSection = document.querySelector('section.grp .gh[data-g="held"]')?.closest("section.grp");
        if (!heldSection) return false;
        return Boolean(heldSection.querySelector('.row[data-k*="1392"]'));
      });
      if (!inHeldGroup) {
        problems.push("Row #1392 did not move into 'Held or closed' group after hold action");
      } else {
        console.log("[PASS] Row #1392 moved into 'Held or closed by you' group");
      }
    }
  }
  const shotHeld1280 = path.join(screenshotDir, "dqr1-held-1280.png");
  await page1280.screenshot({ path: shotHeld1280, fullPage: true });
  console.log(`[PASS] Held 1280 screenshot saved: ${shotHeld1280}`);

  // Check: Unhold primary on a held PR
  console.log("Testing Unhold primary on held PR #999...");
  // Expand held group if collapsed
  await page1280.evaluate(() => {
    const heldBtn = document.querySelector('.gh[data-g="held"]');
    if (heldBtn && heldBtn.getAttribute("aria-expanded") === "false") {
      heldBtn.click();
    }
  });
  await page1280.waitForTimeout(200);
  const heldRow999 = await page1280.$('.row[data-k*="999"]');
  if (!heldRow999) {
    problems.push("Could not find row #999 in held group");
  } else {
    await heldRow999.click();
    await page1280.waitForSelector("#panel .acts", { timeout: 3000 });
    const priAction = await page1280.$eval("#panel .acts .btn.pri", el => ({
      text: el.textContent.trim(),
      action: el.getAttribute("data-a")
    }));
    if (priAction.action !== "unhold" || priAction.text !== "Unhold") {
      problems.push(`Expected primary action to be 'Unhold', got: ${JSON.stringify(priAction)}`);
    } else {
      console.log(`[PASS] Unhold is the primary action on held PR #999 (${priAction.text})`);
    }
    // Close panel
    await page1280.click("#panel #x");
    await page1280.waitForTimeout(200);
  }

  // Check: Error state on 500 with Retry working
  console.log("Testing error state on 500 with Retry working...");
  await page1280.route("**/api/queue*", route => {
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: "Database unreachable" })
    });
  });

  await page1280.evaluate(() => loadQueueFromServer());
  await page1280.waitForSelector("#dq-error", { timeout: 5000 });
  const errorMsg = await page1280.$eval("#dq-error", el => el.textContent.trim());
  if (!errorMsg.includes("Database unreachable")) {
    problems.push(`Error state does not contain error message 'Database unreachable': ${errorMsg}`);
  } else {
    console.log("[PASS] Error state shows error message on 500");
  }

  const shotError1280 = path.join(screenshotDir, "dqr1-error-1280.png");
  await page1280.screenshot({ path: shotError1280, fullPage: true });
  console.log(`[PASS] Error 1280 screenshot saved: ${shotError1280}`);

  // Now unroute 500 and click Retry
  await page1280.unroute("**/api/queue*");
  const retryBtn = await page1280.$("#dq-retry");
  if (!retryBtn) {
    problems.push("Retry button #dq-retry not found in error state");
  } else {
    await retryBtn.click();
    await page1280.waitForSelector(".pipe", { timeout: 6000 });
    console.log("[PASS] Retry button refetched and recovered queue view");
  }

  // Check: Empty state ("no snapshot yet")
  console.log("Testing empty state...");
  await page1280.route("**/api/queue*", route => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ at: null, products: {} })
    });
  });
  await page1280.evaluate(() => loadQueueFromServer());
  await page1280.waitForTimeout(300);
  const emptyStateText = await page1280.$eval(".state", el => el.textContent.trim());
  if (emptyStateText.includes("Every PR is merged")) {
    problems.push(`Empty state claims 'Every PR is merged': ${emptyStateText}`);
  }
  if (!emptyStateText.includes("No queue read yet") || !emptyStateText.includes("factory release queue")) {
    problems.push(`Empty state missing expected wording 'No queue read yet... factory release queue': ${emptyStateText}`);
  } else {
    console.log("[PASS] Empty state matches expected 'No queue read yet. It is read by the morning plan, or run factory release queue.'");
  }
  await page1280.unroute("**/api/queue*");

  await page1280.close();

  // ----------------------------------------------------
  // Test Suite 2: Mobile 390px Viewport
  // ----------------------------------------------------
  console.log("\nTesting 390px viewport...");
  const { page: page390 } = await newPage({ width: 390, height: 844 }, "2026-11-10T08:00:00+01:00");
  await page390.goto(url + "#/all/automation", { waitUntil: "networkidle" });
  await page390.click('[data-autotab=\"develop-queue\"]');
  await page390.waitForSelector(".pipe", { timeout: 8000 });
  await page390.waitForTimeout(300);

  // Check: No horizontal scroll at 390px
  const hscroll = await page390.evaluate(() => {
    const el = document.scrollingElement;
    return el.scrollWidth > el.clientWidth + 1;
  });
  if (hscroll) {
    const scrollInfo = await page390.evaluate(() => ({
      scrollWidth: document.scrollingElement.scrollWidth,
      clientWidth: document.scrollingElement.clientWidth
    }));
    problems.push(`Horizontal scroll detected at 390px: ${JSON.stringify(scrollInfo)}`);
  } else {
    console.log("[PASS] No horizontal scroll at 390px viewport");
  }

  // Screenshot normal 390
  const shotNormal390 = path.join(screenshotDir, "dqr1-normal-390.png");
  await page390.screenshot({ path: shotNormal390, fullPage: true });
  console.log(`[PASS] Normal 390 screenshot saved: ${shotNormal390}`);

  // Open panel and verify width at 390px
  const rowAt390 = await page390.$(".row");
  if (rowAt390) {
    await rowAt390.click();
    await page390.waitForSelector("#panel h2", { timeout: 3000 });
    const panelWidthCheck = await page390.evaluate(() => {
      const panel = document.getElementById("panel");
      const rect = panel.getBoundingClientRect();
      const docWidth = document.documentElement.clientWidth;
      const overflowingElements = [];
      panel.querySelectorAll("*").forEach(el => {
        if (el.clientWidth > rect.width + 1) {
          overflowingElements.push({ tag: el.tagName, className: el.className, w: el.clientWidth, pw: rect.width });
        }
      });
      return {
        panelWidth: rect.width,
        docWidth: docWidth,
        overflowingCount: overflowingElements.length,
        overflowingElements: overflowingElements.slice(0, 5)
      };
    });

    if (panelWidthCheck.panelWidth > panelWidthCheck.docWidth + 1) {
      problems.push(`Panel is wider than document at 390px (${panelWidthCheck.panelWidth}px > ${panelWidthCheck.docWidth}px)`);
    } else if (panelWidthCheck.overflowingCount > 0) {
      problems.push(`Elements inside panel are wider than panel: ${JSON.stringify(panelWidthCheck.overflowingElements)}`);
    } else {
      console.log(`[PASS] Panel width at 390px is within bounds (${panelWidthCheck.panelWidth}px <= ${panelWidthCheck.docWidth}px), no overflowing elements`);
    }
  }

  // Screenshot weekend 390
  await page390.evaluate(() => {
    window.__NOW__ = "2026-11-15T10:00:00+01:00"; // Sunday
    render();
  });
  await page390.waitForTimeout(200);
  const shotWeekend390 = path.join(screenshotDir, "dqr1-weekend-390.png");
  await page390.screenshot({ path: shotWeekend390, fullPage: true });
  console.log(`[PASS] Weekend 390 screenshot saved: ${shotWeekend390}`);

  // Screenshot held after action 390
  await page390.evaluate(() => {
    window.__NOW__ = "2026-11-10T08:00:00+01:00";
    render();
  });
  await page390.waitForTimeout(200);
  const shotHeld390 = path.join(screenshotDir, "dqr1-held-390.png");
  await page390.screenshot({ path: shotHeld390, fullPage: true });
  console.log(`[PASS] Held 390 screenshot saved: ${shotHeld390}`);

  // Screenshot error state 390
  await page390.route("**/api/queue*", route => {
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: "Failed to connect to upstream server" })
    });
  });
  await page390.evaluate(() => loadQueueFromServer());
  await page390.waitForSelector("#dq-error", { timeout: 4000 });
  const shotError390 = path.join(screenshotDir, "dqr1-error-390.png");
  await page390.screenshot({ path: shotError390, fullPage: true });
  console.log(`[PASS] Error 390 screenshot saved: ${shotError390}`);
  await page390.unroute("**/api/queue*");

  await page390.close();

} finally {
  await browser.close();
  cleanup();
}

console.log("\n===== Develop Queue browser summary =====");
if (problems.length > 0) {
  console.error("FAILURES:\n" + problems.map(p => " - " + p).join("\n"));
  process.exit(1);
} else {
  console.log("All Develop queue browser checks PASSED.");
}
