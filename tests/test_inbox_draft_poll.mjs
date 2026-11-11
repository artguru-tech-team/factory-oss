// Self-contained Playwright test in Chromium:
// Tests inbox poll with live draft across 3 routes:
// 1. home (#/bk)
// 2. drawer (#/p/ds) with draft opened inside #drawer
// 3. all (#/all)
//
// In each view:
// - A and B are open.
// - B is opened, draft text is typed in textarea, and a radio option is checked.
// - A is resolved via `factory resolve <itemA>`.
// - Polling triggers within 35 s.
// - A vanishes everywhere (all copies in #main and #drawer).
// - All counts and badges are consistent (inbox ct, side nav, project cards, drawer strip, all group headers).
// - B's draft note and checked radio option remain completely intact.
// - No console errors. Tested at 390 and 1440 px.

import { spawn, execFileSync } from "node:child_process";
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

const cliArgs = process.argv.slice(2);
let outDir = cliArgs[0] || process.env.OUT_DIR || "/tmp/factory-poll-shots";
if (!fs.existsSync(outDir)) {
  fs.mkdirSync(outDir, { recursive: true });
}

// 1. Launch self-contained setup script (builds temp home, temp T3 DB, starts server)
console.log("Starting self-contained test environment...");
const setupScript = path.join(__dirname, "setup_inbox_draft_poll.py");
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
      try {
        resolve(JSON.parse(buf.slice(0, idx)));
      } catch (err) {
        reject(err);
      }
    }
  });
  serverProc.on("error", reject);
  serverProc.on("exit", code => {
    if (code !== 0 && code !== null) reject(new Error(`Server exited with code ${code}`));
  });
});

const cleanup = () => {
  try {
    serverProc.kill("SIGTERM");
  } catch (_) {}
};
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

const { url, factoryHome, itemA, itemB, bin } = serverInfo;
console.log(`Test environment ready at ${url} (FACTORY_HOME=${factoryHome})`);

function resetInbox() {
  execFileSync("python3", [setupScript, "--reset", factoryHome, itemA, itemB], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: repoRoot },
    stdio: "pipe",
  });
}

function resolveItemA() {
  execFileSync(bin, ["resolve", itemA], {
    env: { ...process.env, FACTORY_HOME: factoryHome },
    encoding: "utf8",
    stdio: "pipe",
  });
}

const browser = await chromium.launch();
const problems = [];
const viewResults = [];

const viewsToTest = [
  { name: "home", route: "#/bk", viewport: { width: 1440, height: 900 }, openInDrawer: false },
  { name: "drawer", route: "#/p/ds", viewport: { width: 1440, height: 900 }, openInDrawer: true },
  { name: "all", route: "#/all", viewport: { width: 1440, height: 900 }, openInDrawer: false },
  { name: "home-mobile", route: "#/bk", viewport: { width: 390, height: 844 }, openInDrawer: false },
  { name: "drawer-mobile", route: "#/p/ds", viewport: { width: 390, height: 844 }, openInDrawer: true },
];

try {
  for (const tc of viewsToTest) {
    console.log(`\n========================================`);
    console.log(`Testing view: ${tc.name} (${tc.route}) at ${tc.viewport.width}x${tc.viewport.height}`);
    console.log(`========================================`);

    resetInbox();

    const page = await browser.newPage({ viewport: tc.viewport });
    const viewErrors = [];

    page.on("console", m => {
      if (m.type() === "error" && !m.text().includes("violates the following Content Security Policy")) {
        viewErrors.push(`console error: ${m.text()}`);
      }
    });
    page.on("pageerror", e => viewErrors.push(`page error: ${e.message}`));

    await page.goto(url + tc.route, { waitUntil: "networkidle" });
    if (tc.openInDrawer) {
      await page.waitForSelector("#drawer[open] .pp-title", { timeout: 10000 });
    }

    // Wait for items A and B
    await page.waitForSelector(`.ib[data-id="${itemA}"]`, { timeout: 10000 });
    await page.waitForSelector(`.ib[data-id="${itemB}"]`, { timeout: 10000 });

    const aCopiesInitial = await page.locator(`.ib[data-id="${itemA}"]`).count();
    console.log(`Item ${itemA} initial copies: ${aCopiesInitial}`);
    if (tc.openInDrawer && aCopiesInitial < 2) {
      viewErrors.push(`Expected 2 copies of item A on drawer view (#main and #drawer), found ${aCopiesInitial}`);
    }

    // Open item B and type draft note and check radio option
    const leverBBtn = tc.openInDrawer
      ? page.locator(`#drawer [data-lever="${itemB}"]`)
      : page.locator(`[data-lever="${itemB}"]`).first();

    await leverBBtn.click();

    const formB = tc.openInDrawer
      ? page.locator(`#drawer [data-form="${itemB}"]`)
      : page.locator(`[data-form="${itemB}"]`).first();

    await formB.waitFor({ state: "visible", timeout: 5000 });

    const textareaB = formB.locator("textarea");
    const draftText = `Owner draft note on ${tc.name}: keep this intact while A is resolved`;
    await textareaB.fill(draftText);

    // Pick radio option '2'
    const radio2 = formB.locator("input[type=radio][value='2']");
    if (await radio2.count() > 0) {
      await radio2.check();
    }

    // Read counts before resolving A
    const countsBefore = await page.evaluate(() => {
      const ibCt = document.querySelector(".inbox .panel-h .ct")?.textContent?.trim() || "";
      const sideHome = document.querySelector('#side .nav[data-sec="home"] .cnt')?.textContent?.trim() || "";
      const cardBadges = [...document.querySelectorAll(".card .cnt")].map(el => el.textContent.trim());
      const stripBadges = [...document.querySelectorAll("#dwStrip a .cnt")].map(el => el.textContent.trim());
      const grpHeader = document.querySelector(".grp-h span.fog")?.textContent?.trim() || "";
      const lrowBadge = document.querySelector(".lrow .sub .cnt")?.textContent?.trim() || "";
      const statusText = document.querySelector("p.status")?.textContent?.trim() || "";
      return { ibCt, sideHome, cardBadges, stripBadges, grpHeader, lrowBadge, statusText };
    });
    console.log("Counts before resolve:", JSON.stringify(countsBefore));

    await page.screenshot({ path: `${outDir}/${tc.name}-before-resolve.png` });

    console.log(`Resolving item ${itemA} via CLI...`);
    resolveItemA();

    console.log(`Waiting up to 35s for item ${itemA} to vanish everywhere...`);
    const t0 = Date.now();
    let vanished = false;

    while (Date.now() - t0 < 35000) {
      const count = await page.locator(`.ib[data-id="${itemA}"]`).count();
      if (count === 0) {
        vanished = true;
        break;
      }
      await page.waitForTimeout(400);
    }
    const elapsedSec = ((Date.now() - t0) / 1000).toFixed(1);

    if (!vanished) {
      viewErrors.push(`Item ${itemA} did not vanish within 35s (still present after ${elapsedSec}s)`);
    } else {
      console.log(`Item ${itemA} vanished everywhere in ${elapsedSec}s (<= 35s)`);
    }

    // Assert A is gone everywhere (all copies)
    const remainingA = await page.locator(`.ib[data-id="${itemA}"]`).count();
    if (remainingA !== 0) {
      viewErrors.push(`Found ${remainingA} remaining copies of item A`);
    }

    // Read counts after resolving A
    const countsAfter = await page.evaluate(() => {
      const ibCt = document.querySelector(".inbox .panel-h .ct")?.textContent?.trim() || "";
      const sideHome = document.querySelector('#side .nav[data-sec="home"] .cnt')?.textContent?.trim() || "";
      const cardBadges = [...document.querySelectorAll(".card .cnt")].map(el => el.textContent.trim());
      const stripBadges = [...document.querySelectorAll("#dwStrip a .cnt")].map(el => el.textContent.trim());
      const grpHeader = document.querySelector(".grp-h span.fog")?.textContent?.trim() || "";
      const lrowBadge = document.querySelector(".lrow .sub .cnt")?.textContent?.trim() || "";
      const statusText = document.querySelector("p.status")?.textContent?.trim() || "";
      return { ibCt, sideHome, cardBadges, stripBadges, grpHeader, lrowBadge, statusText };
    });
    console.log("Counts after resolve:", JSON.stringify(countsAfter));

    // Assert count consistency:
    // Inbox count should be 1
    if (countsAfter.ibCt && countsAfter.ibCt !== "1") {
      viewErrors.push(`Inbox count expected "1", got "${countsAfter.ibCt}"`);
    }
    // Side nav count if present should be 1
    if (countsAfter.sideHome && countsAfter.sideHome !== "1") {
      viewErrors.push(`Side nav home count expected "1", got "${countsAfter.sideHome}"`);
    }
    // Status text should say "1 waiting on you"
    if (countsAfter.statusText && !countsAfter.statusText.includes("1 waiting on you")) {
      viewErrors.push(`Status text expected "1 waiting on you", got "${countsAfter.statusText}"`);
    }
    // If card badge was present (was 2), should now be 1
    if (countsBefore.cardBadges.includes("2") && !countsAfter.cardBadges.includes("1")) {
      viewErrors.push(`Project card badge expected "1", got ${JSON.stringify(countsAfter.cardBadges)}`);
    }
    // If drawer strip badge was present (was 2), should now be 1
    if (countsBefore.stripBadges.includes("2") && !countsAfter.stripBadges.includes("1")) {
      viewErrors.push(`Drawer strip badge expected "1", got ${JSON.stringify(countsAfter.stripBadges)}`);
    }
    // If grpHeader was present on #/all (e.g. "2 waiting"), should now be "1 waiting"
    if (countsBefore.grpHeader.includes("2 waiting") && !countsAfter.grpHeader.includes("1 waiting")) {
      viewErrors.push(`Group header expected "1 waiting", got "${countsAfter.grpHeader}"`);
    }
    // If lrowBadge was present on #/all (e.g. "2"), should now be "1"
    if (countsBefore.lrowBadge === "2" && countsAfter.lrowBadge !== "1") {
      viewErrors.push(`Project row badge expected "1", got "${countsAfter.lrowBadge}"`);
    }

    // Assert item B's draft is intact
    const curDraftVal = await textareaB.inputValue();
    if (curDraftVal !== draftText) {
      viewErrors.push(`Item B draft text changed! Expected "${draftText}", got "${curDraftVal}"`);
    }
    const isRadioChecked = await radio2.isChecked();
    if (!isRadioChecked) {
      viewErrors.push(`Item B radio choice '2' is no longer checked`);
    }

    const shotPath = `${outDir}/${tc.name}-after-resolve.png`;
    await page.screenshot({ path: shotPath });

    viewResults.push({
      view: tc.name,
      viewport: `${tc.viewport.width}x${tc.viewport.height}`,
      timeToVanish: `${elapsedSec}s`,
      countsBefore,
      countsAfter,
      draftIntact: curDraftVal === draftText && isRadioChecked,
      errors: viewErrors,
      screenshot: shotPath,
    });

    if (viewErrors.length) {
      problems.push(...viewErrors.map(e => `[${tc.name}] ${e}`));
    } else {
      console.log(`[PASS] ${tc.name}: A vanished in ${elapsedSec}s, all counts consistent, B draft intact.`);
    }

    await page.close();
  }

  console.log(`\n========================================`);
  console.log(`TEST SUMMARY`);
  console.log(`========================================`);
  for (const r of viewResults) {
    console.log(`- ${r.view} (${r.viewport}): vanished in ${r.timeToVanish}, draft intact=${r.draftIntact}, status: ${r.errors.length ? "FAIL" : "PASS"}`);
  }

  if (problems.length) {
    console.error(`\nFailed with ${problems.length} problem(s):\n` + problems.join("\n"));
    process.exit(1);
  }
  console.log("\nAll inbox draft poll tests PASSED successfully!");
} finally {
  await browser.close();
  cleanup();
}
