// Playwright browser test for the Product manager view.
// Covers: Today (tiles, morning memo with a failing launch first), Lifecycle (stage dots, stuck rows first, Unstick then
// Undo; a failed merged-not-in-prod read shows its banner), Roadmap priority (Approve then Undo), Signals (an
// ignored signal is gone after a reload), After launch (chips, a recorded day-1 check, a verdict then Undo, the launch kit line, Draft the missing then Undo), the support-error banner, the quiet day, and on Projects: sidebar badge, tiles, filter by a tile, Close then
// Undo (server and lists follow), Roadmaps (a proposed draft and an empty product, Approve one row, Drop then Undo another), Open prototype, Merged not in prod, the closed toggle on the Projects list, empty and
// error states, no horizontal scroll at 390 px, no console errors.
// Screenshots go to SHOTS_DIR (default: a new temp dir).
//   PLAYWRIGHT=/path/to/playwright/index.mjs SHOTS_DIR=/tmp/pm-sweep-shots node tests/test_pm_browser.mjs

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
let resolvedPlaywright = process.env.PLAYWRIGHT;
if (!resolvedPlaywright) {
  console.error("Set PLAYWRIGHT to the path of playwright/index.mjs");
  process.exit(2);
}
if (!resolvedPlaywright.endsWith(".mjs") && !resolvedPlaywright.endsWith(".js")) {
  resolvedPlaywright = path.join(resolvedPlaywright, "index.mjs");
}
const { chromium } = await import(resolvedPlaywright);

const outDir = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "pm-shots-"));
fs.mkdirSync(outDir, { recursive: true });

const serverProc = spawn("python3", [path.join(__dirname, "setup_pm_browser.py")], {
  cwd: repoRoot,
  env: { ...process.env, PYTHONPATH: repoRoot },
  stdio: ["ignore", "pipe", "inherit"],
});
const info = await new Promise((resolve, reject) => {
  let buf = "";
  serverProc.stdout.on("data", chunk => {
    buf += chunk.toString();
    const i = buf.indexOf("\n");
    if (i !== -1) { try { resolve(JSON.parse(buf.slice(0, i))); } catch (e) { reject(e); } }
  });
  serverProc.on("exit", code => { if (code) reject(new Error(`setup exited ${code}`)); });
});
const cleanup = () => { try { serverProc.kill("SIGTERM"); } catch (_) {} };
process.on("exit", cleanup);
process.on("SIGINT", () => { cleanup(); process.exit(1); });

const base = new URL(info.url);
const pmUrl = `${base.origin}/${base.search}#/acme/pm`;
const homeUrl = `${base.origin}/${base.search}#/acme`;
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const problems = [];
const expect = (ok, msg) => { if (!ok) problems.push(msg); };

async function open(viewport) {
  const page = await browser.newPage({ viewport });
  page.on("console", m => {
    if (m.type() === "error" && !m.text().includes("Content Security Policy") && !m.text().includes("status of 500")) {
      problems.push(`console error: ${m.text()}`);
    }
  });
  page.on("pageerror", e => problems.push(`page error: ${e.message}`));
  return page;
}
const tile = (page, g) => page.locator(`[data-pm-filter="${g}"] .n`).innerText();
const badge = page => page.locator('#side [data-sec="pm"] .cnt').innerText();
const shot = (page, name) => page.screenshot({ path: path.join(outDir, name), fullPage: true });
const apiPm = page => page.evaluate(async url => (await fetch(url)).json(), `${base.origin}/api/pm${base.search}`);
const FROW = ".pm-row:has(.pm-acts)";  // a sweep finding (Merged, not in prod rows carry no buttons)
const tab = async (page, k) => { await page.click(`[data-pm-tab="${k}"]`); await page.waitForSelector(`[data-pm-tab="${k}"][aria-pressed="true"]`); };
// fake one GET /api/pm answer from the real one
const patchPm = (page, change) => page.route("**/api/pm*", async r => {
  if (r.request().method() !== "GET") return r.continue();
  const res = await r.fetch();
  r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(change(await res.json())) });
});

try {
  const page = await open({ width: 1440, height: 900 });
  await page.goto(pmUrl);

  // Today is the default tab: tiles, then the morning memo's 3 decisions in order
  await page.waitForSelector(".pm-memo");
  const stats = await page.locator(".pm-stat .n").allInnerTexts();
  expect(stats.join(",") === "7,4,2,4,1", `Today tiles ${stats} != 7,4,2,4,1 (followed, stuck, launched, signals, inbox)`);
  const decs = await page.locator(".pm-memo li .t").allInnerTexts();
  expect(decs.length === 3, `memo has ${decs.length} decisions, not 3`);
  expect(/^shop release \d+ \w{3}: errors up since launch$/.test(decs[0]), `decision 1 (the failing launch): ${decs[0]}`);
  expect(decs[1] === "Reorder Now for Acme: move Bulk export for large... to #1", `decision 2: ${decs[1]}`);
  expect(decs[2] === "Close 3 finished projects", `decision 3: ${decs[2]}`);
  expect((await page.locator(".pm-tab .c").allInnerTexts()).join(",") === "3,11,3,4,2,2,9",
    `tab counts ${await page.locator(".pm-tab .c").allInnerTexts()} != 3,11,3,4,2,2,9 (decisions, features, moves, signals, launches, products with no roadmap in view, sweep rows)`);
  const launchDec = page.locator(".pm-memo li").first();
  expect((await launchDec.innerText()).includes("sentry: 41 > 11 (baseline 3)"), "launch decision does not name the failing check");
  expect((await launchDec.locator("button").allInnerTexts()).join(",") === "Keep,Iterate,Roll back,See launch", "launch decision buttons");
  expect(await launchDec.locator("button.primary").count() === 0, "no yellow Keep on a launch whose errors went up");
  await shot(page, "pm-today.png");
  await shot(page, "today-with-launch-decision.png");
  expect(((await page.locator(".pm-stat .n").nth(1).getAttribute("style")) || "").includes("var(--red)"), "stuck tile is not red");
  await shot(page, "today-stuck-tile.png");

  // Lifecycle: one row per feature, stuck rows first, a dot per stage; Unstick files one inbox item, Undo removes the note
  await tab(page, "life");
  const lrows = page.locator(".pm-life tr:has(td)");
  expect((await lrows.count()) === 11, `lifecycle rows ${await lrows.count()} != 11`);
  expect((await page.locator(".pm-err").innerText()).includes("unreleased shop: gh: HTTP 502"), "Lifecycle does not show the failed merged read");
  expect((await page.locator(".pm-life th").allInnerTexts()).slice(1, 12).join(",") === "Signal,Roadmap,Prototype,Validated,Build,Review,Develop,Prod,Announced,Day 7,Day 30",
    `stage columns ${await page.locator(".pm-life th").allInnerTexts()}`);
  const lfirst = await lrows.first().innerText();
  expect(lfirst.includes("ACME-1") && lfirst.includes("stuck 20d") && lfirst.includes("Roadmap for 20 days (limit 14)"), `first lifecycle row: ${lfirst}`);
  expect((await page.locator(".pm-life [data-pm-unstick]").count()) === 4, `Unstick buttons ${await page.locator(".pm-life [data-pm-unstick]").count()} != 4`);
  expect((await page.locator(".pm-life .pm-st.stuck").count()) === 4, "stuck dots != 4");
  const feeLife = page.locator(".pm-life tr", { hasText: "New fee engine" });
  expect((await feeLife.locator(".pm-st.ok").count()) === 3 && (await feeLife.locator(".pm-st").nth(8).getAttribute("class")).includes("now"), "fee engine is not at Announced (Build, Develop, Prod done)");
  expect((await page.locator(".pm-life tr", { hasText: "Dispute evidence" }).locator(".pm-st").nth(5).getAttribute("class")).includes("now"), "disputes not at Review");
  await shot(page, "lifecycle.png");
  const kyc = page.locator(".pm-life tr", { hasText: "Lock KYC" });
  const inboxBefore = (await apiPm(page)).stats.inbox;
  await kyc.locator("button", { hasText: "Unstick" }).click();
  await kyc.locator("text=nudged").waitFor();
  expect(!(await kyc.locator("[data-pm-unstick]").count()), "a nudged row still shows Unstick");
  let lapi = await apiPm(page);
  expect(lapi.lifecycle.find(r => r.id === "kyc-lock").nudged, "nudge not stored on the server");
  expect(lapi.stats.inbox === inboxBefore + 1, `inbox items ${lapi.stats.inbox} != ${inboxBefore + 1} after Unstick`);
  await shot(page, "lifecycle-nudged.png");
  await kyc.locator("button", { hasText: "Undo" }).click();
  await kyc.locator("button", { hasText: "Unstick" }).waitFor();
  lapi = await apiPm(page);
  expect(!lapi.lifecycle.find(r => r.id === "kyc-lock").nudged, "Undo did not remove the nudge on the server");
  expect(lapi.stats.inbox === inboxBefore + 1, "Undo removed the inbox item (it stays)");
  await tab(page, "brief");

  // After launch: newest first, live chips, shop's recorded day-1 check, a verdict then Undo
  await launchDec.locator("button", { hasText: "See launch" }).click();
  await page.waitForSelector('[data-pm-tab="after"][aria-pressed="true"]');
  const cards = page.locator(".pm-launch");
  expect((await cards.count()) === 2, `launch cards ${await cards.count()} != 2`);
  const acmeCard = cards.nth(0), shopCard = cards.nth(1);
  const acmeText = await acmeCard.innerText();
  expect(acmeText.includes("#1490 Bulk export as CSV") && acmeText.includes("day 2") && /next: Day-7 check \d+ \w{3}/.test(acmeText), `acme card: ${acmeText}`);
  expect((await acmeCard.locator(".pm-chips:not(.pm-kit) .pm-chk").allInnerTexts()).map(t => t[0]).join("") === "✓✓!", `acme chips ${await acmeCard.locator(".pm-chips:not(.pm-kit) .pm-chk").allInnerTexts()}`);
  expect(acmeText.includes("9 tickets mention it"), "acme tickets chip does not count the mapped support tickets");
  await shot(page, "after-launch.png");
  const shopText = await shopCard.innerText();
  const failing = shopCard.locator(".pm-chips:not(.pm-kit) .pm-chk", { has: page.locator(".pm-n") });
  expect((await failing.count()) === 1 && /Errors since launch\s*· sentry: 41 > 11 \(baseline 3\)/.test(await failing.innerText()), `shop errors chip: ${shopText}`);
  expect(shopText.includes("Day 1 check,") && shopText.includes("no comms configured"), `shop recorded check or comms chip: ${shopText}`);
  await shopCard.scrollIntoViewIfNeeded();
  await shot(page, "after-launch-failing.png");
  await acmeCard.locator("button", { hasText: "Roll back" }).click();
  await acmeCard.locator("text=✓ Roll back").waitFor();
  expect((await acmeCard.innerText()).includes("roll back with the release manager"), "roll back verdict shows no hint");
  let launch = (await apiPm(page)).launches.find(c => c.product === "acme");
  expect(launch.verdict && launch.verdict.by === "rollback", "verdict not stored on the server");
  expect((await page.locator('[data-pm-tab="after"] .c').innerText()) === "1", "After launch count did not drop to 1");
  await shot(page, "after-launch-verdict.png");
  await acmeCard.locator("button", { hasText: "Undo" }).click();
  await acmeCard.locator("button", { hasText: "Keep" }).waitFor();
  launch = (await apiPm(page)).launches.find(c => c.product === "acme");
  expect(!launch.verdict, "Undo did not remove the verdict on the server");

  // Launch kit: only the changelog can be missing; #1491 has no email yet, so email, video and caption show ! with why;
  // shop has no comms. Draft the missing then Undo.
  const acmeKit = acmeCard.locator(".pm-kit"), shopKit = shopCard.locator(".pm-kit");
  const kitText = (await acmeKit.innerText()).replace(/\s+/g, " ");
  // before the changelog: "waits for the changelog", or "shipped before comms started" early in the week (comms start on Monday)
  expect(/^Launch kit: ✕ ?Changelog ! ?Email · (waits for the changelog|shipped before comms started) ! ?Brag video · \1 ! ?Caption · \1 Draft the missing 1$/.test(kitText), `acme kit line: ${kitText}`);
  expect((await shopKit.innerText()).includes("no comms configured") && !(await shopKit.locator("button").count()), "shop kit: no comms, no button");
  await acmeCard.scrollIntoViewIfNeeded();
  await shot(page, "kit.png");
  await acmeKit.locator("button", { hasText: "Draft the missing 1" }).click();
  await acmeKit.locator("text=changelog stored").waitFor();
  launch = (await apiPm(page)).launches.find(c => c.product === "acme");
  expect(launch.kit.queued && launch.kit.queued.items.join(",") === "Changelog", `kit not queued on the server: ${JSON.stringify(launch.kit)}`);
  expect(launch.kit.items[0].state === "y", "Draft the missing did not store the changelog");
  expect(!(await acmeKit.locator("button", { hasText: "Draft the missing" }).count()), "Draft the missing still shown once queued");
  await shot(page, "kit-queued.png");
  await acmeKit.locator("button", { hasText: "Undo" }).click();
  await acmeKit.locator("text=Undo").waitFor({ state: "detached" });
  launch = (await apiPm(page)).launches.find(c => c.product === "acme");
  expect(!launch.kit.queued, "Undo did not remove the queued kit on the server");
  expect(!(await acmeKit.locator("button").count()), "a button once the changelog is stored (it stays after Undo)");

  // Roadmaps: one card per product; store has a proposed draft, tools none, acme is in the Roadmap view.
  // Approve one row (a Next row: Now rows feed the other tabs), then Drop and Undo another.
  await tab(page, "rm");
  const rmCards = page.locator(".pm-rm");
  expect((await rmCards.count()) === 3, `roadmap cards ${await rmCards.count()} != 3`);
  const storeRm = rmCards.filter({ hasText: "Store" }), toolsRm = rmCards.filter({ hasText: "Tools" }), acmeRm = rmCards.filter({ hasText: "Acme" });
  expect((await storeRm.locator(".pm-tag").first().innerText()) === "draft ready" && (await storeRm.innerText()).includes("Sources: 12 open issues, 30 days of orders"), `store card: ${await storeRm.innerText()}`);
  expect((await storeRm.locator("th").allInnerTexts()).slice(0, 4).join(",") === "Lane,Row,Goal,Evidence", "store table columns");
  expect((await storeRm.locator("tr:has(td)").count()) === 4 && (await storeRm.locator("button", { hasText: "Approve all" }).count()) === 1, "store rows or Approve all");
  const toolsText = await toolsRm.innerText();
  expect(toolsText.includes("no roadmap") && toolsText.includes("factory pm propose-roadmap tools --file rows.json") && !(await toolsRm.locator("button").count()), `tools card: ${toolsText}`);
  expect((await acmeRm.locator(".pm-tag").first().innerText()) === "in Roadmap view", "acme card is not in Roadmap view");
  await shot(page, "roadmaps.png");
  const wish = storeRm.locator("tr", { hasText: "Wishlist sharing links" });
  await wish.locator("button", { hasText: "Approve" }).click();
  await wish.locator("text=added as store-wishlist-sharing").waitFor();
  expect(!(await wish.locator("button").count()), "an approved row still has a button (no Undo)");
  const item = await page.evaluate(async url => (await (await fetch(url)).json()).items.find(i => i.title === "Wishlist sharing links"),
    `${base.origin}/api/roadmap${base.search}&product=store`);
  expect(item && item.lane === "next" && item.note === "Goal: 1 in 10 wishlists shared · Evidence: #30: share button requested", `roadmap item: ${JSON.stringify(item)}`);
  const hist = storeRm.locator("tr", { hasText: "Order history filters" });
  await hist.locator("button", { hasText: "Drop" }).click();
  await hist.locator("text=✓ Dropped").waitFor();
  let rmApi = (await apiPm(page)).roadmaps.find(c => c.product === "store");
  expect(rmApi.rows.find(r => r.id === "store-r3").status === "dropped", "drop not stored on the server");
  await shot(page, "roadmaps-approved.png");
  await hist.locator("button", { hasText: "Undo" }).click();
  await hist.locator("button", { hasText: "Drop" }).waitFor();
  rmApi = (await apiPm(page)).roadmaps.find(c => c.product === "store");
  expect(rmApi.rows.find(r => r.id === "store-r3").status === "proposed" && rmApi.status === "draft", "Undo did not put the row back to proposed");

  // Roadmap priority: proposed order, Approve then Undo moves the row on the server and back
  await tab(page, "prio");
  const first = await page.locator(".pm-prio tr").nth(1).innerText();
  expect(first.includes("ACME-3") && first.includes("+2") && first.includes("9 tickets this week, your P3"), `first priority row: ${first}`);
  await shot(page, "pm-priority.png");
  const bulk = page.locator(".pm-prio tr", { hasText: "Bulk export for large" });
  await bulk.locator("button", { hasText: "Approve" }).click();
  await bulk.locator("text=✓ Approved").waitFor();
  let rows = (await apiPm(page)).priority[0].rows;
  expect(rows.find(r => r.id === "ACME-3").was === 1, "approve did not move ACME-3 to P1 on the server");
  await shot(page, "pm-priority-approved.png");
  await bulk.locator("button", { hasText: "Undo" }).click();
  await bulk.locator("button", { hasText: "Approve" }).waitFor();
  rows = (await apiPm(page)).priority[0].rows;
  expect(rows.find(r => r.id === "ACME-3").was === 3, "undo did not put ACME-3 back at P3");

  // Signals: mapped and unmapped; an ignored signal is gone after a reload
  await tab(page, "sig");
  expect((await page.locator(".pm-sig").count()) === 4, `signals ${await page.locator(".pm-sig").count()} != 4`);
  expect((await page.locator(".pm-sig", { hasText: "Bulk export times out" }).innerText()).includes("Maps to: ACME-3"), "support signal not mapped to ACME-3");
  const dark = page.locator(".pm-sig", { hasText: "Dark mode" });
  expect((await dark.innerText()).includes("No roadmap row covers this"), "unmapped signal text");
  await shot(page, "pm-signals.png");
  await dark.locator("button", { hasText: "Ignore" }).click();
  await dark.locator("text=Ignored for 14 days").waitFor();
  await page.reload();
  await tab(page, "sig");
  expect((await page.locator(".pm-sig").count()) === 3, "ignored signal still listed after a reload");
  expect(!(await page.locator(".pm-sig", { hasText: "Dark mode" }).count()), "ignored signal visible after a reload");

  // Projects: Merged, not in prod, with the failed product's error in its row
  await tab(page, "sweep");
  await page.waitForSelector(FROW);
  const merged = page.locator(".pm-grp", { hasText: "Merged, not in prod" });
  expect((await merged.locator("h2").innerText()).includes("2"), "merged group count != 2");
  expect((await merged.innerText()).includes("#1498 Search: highlight matches"), "merged PR title missing");
  expect((await merged.locator(".pm-row", { hasText: "shop" }).innerText()).includes("Could not read: gh: HTTP 502"), "failed product error missing");
  await shot(page, "pm-projects-merged.png");

  // default: 3 finished (fee-engine, landing, release-panel), 1 prototype, 2 quiet, 1 late
  const counts = await Promise.all(["finished", "proto", "quiet", "late"].map(g => tile(page, g)));
  expect(counts.join(",") === "3,1,2,1", `tiles ${counts} != 3,1,2,1`);
  expect((await badge(page)) === "7", `sidebar badge ${await badge(page)} != 7`);
  expect((await page.locator(FROW).count()) === 7, "default rows != 7");
  expect(await page.locator('[data-pm-act][title*="autopilot"]').first().isDisabled(), "nudge on a project off autopilot should be disabled");
  await shot(page, "pm-default.png");

  // a tile filters, a second click clears it
  await page.click('[data-pm-filter="quiet"]');
  expect((await page.locator(FROW).count()) === 2, "quiet filter rows != 2");
  expect(!(await page.locator(".pm-grp h2", { hasText: "Finished" }).count()), "finished group still visible under the quiet filter");
  expect(!(await page.locator(".pm-grp h2", { hasText: "Merged" }).count()), "merged group still visible under the quiet filter");
  await shot(page, "pm-filtered.png");
  await page.click('[data-pm-filter="quiet"]');
  expect((await page.locator(FROW).count()) === 7, "clearing the filter did not restore 7 rows");

  // Open prototype shows the prototype drawer
  await page.click("[data-pm-proto]");
  await page.waitForSelector("#protoDrawer[open]");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#protoDrawer[open]"));

  // Close then Undo; the server and the Projects list follow
  const row = page.locator(".pm-row", { hasText: "Switch the landing page" }).first();
  await row.locator("button", { hasText: "Close" }).click();
  await page.waitForSelector(".pm-row.gone");
  expect((await row.innerText()).includes("Closed"), "row shows no Closed note");
  expect((await tile(page, "finished")) === "2", "finished tile did not drop to 2");
  expect((await badge(page)) === "6", "badge did not drop to 6");
  let api = await apiPm(page);
  expect(!api.findings.some(f => f.id === "finished:landing"), "server still reports landing as finished");
  await shot(page, "pm-after-close.png");
  await row.locator("button", { hasText: "Undo" }).click();
  await page.waitForFunction(() => !document.querySelector(".pm-row.gone"));
  expect((await tile(page, "finished")) === "3", "undo did not restore the finished tile");
  api = await apiPm(page);
  expect(api.findings.some(f => f.id === "finished:landing"), "undo did not reopen landing on the server");
  await shot(page, "pm-after-undo.png");

  // Keep open snoozes (and Undo ends the snooze)
  const fee = page.locator(".pm-row", { hasText: "New fee engine" }).first();
  await fee.locator("button", { hasText: "Keep open" }).click();
  await fee.locator("text=Kept open 7 days").waitFor();
  expect(!(await apiPm(page)).findings.some(f => f.project === "fee-engine"), "snoozed project still in the sweep");
  await fee.locator("button", { hasText: "Undo" }).click();
  await page.waitForFunction(() => !document.querySelector(".pm-row.gone"));
  expect((await apiPm(page)).findings.some(f => f.project === "fee-engine"), "undo did not end the snooze");

  // Projects list: the closed project is hidden, with a toggle
  await page.goto(homeUrl);
  await page.reload();
  await page.waitForSelector(".card");
  expect((await page.locator(".card", { hasText: "Creator profile redesign" }).count()) === 0, "closed project visible by default");
  const toggle = page.locator("[data-show-closed]");
  expect((await toggle.innerText()) === "Show closed (1)", `toggle text ${await toggle.innerText()}`);
  await toggle.click();
  expect((await page.locator(".card.closed", { hasText: "Creator profile redesign" }).count()) === 1, "Show closed did not list it");
  await shot(page, "projects-show-closed.png");

  // the support digest's last run failed: a banner, and every tab still renders (the API answer is faked)
  await patchPm(page, b => ({ ...b, supportError: { error: "support.example.com timed out", from: b.signals.find(s => s.src === "Support").at } }));
  await page.goto(pmUrl);
  await page.reload();
  await page.waitForSelector(".pm-memo");
  const banner = await page.locator(".pm-err").first().innerText();  // then Lifecycle's "could not read unreleased shop"
  expect(banner.startsWith("Support did not answer (support.example.com timed out). Support signals are from "), `banner: ${banner}`);
  await shot(page, "pm-support-error.png");
  await tab(page, "sig");
  expect((await page.locator(".pm-err").count()) === 1 && (await page.locator(".pm-sig").count()) === 3, "signals tab lost the banner or its rows");
  await page.unroute("**/api/pm*");

  // quiet day: no decision, the empty state names the next run
  await patchPm(page, b => ({ ...b, decisions: [] }));
  await page.reload();
  await page.waitForSelector(".pm-empty");
  const quiet = await page.locator(".pm-empty").innerText();
  expect(quiet.startsWith("Nothing needs you today.") && /Next run \w{3} \d+ \w{3} \d\d:00 UTC/.test(quiet), `quiet day: ${quiet}`);
  await shot(page, "pm-quiet-day.png");
  await page.unroute("**/api/pm*");

  // a source that could not be read: its tab shows the error, the rest still renders
  await patchPm(page, b => ({ ...b, errors: { ...b.errors, sig: ["answers.json: 'list' object has no attribute 'items'"] } }));
  await page.reload();
  await tab(page, "sig");
  expect((await page.locator(".pm-err").innerText()).startsWith("Could not read answers.json"), "unreadable source banner missing");
  expect((await page.locator(".pm-sig").count()) === 3, "signals tab lost its rows next to the source error");
  await tab(page, "prio");
  expect(!(await page.locator(".pm-err").count()), "a signals source error leaked into the priority tab");
  await page.unroute("**/api/pm*");

  // empty and error states (the API answers are faked)
  await patchPm(page, b => ({ ...b, findings: [], counts: {}, total: 0, unreleased: [] }));
  await page.reload();
  await tab(page, "sweep");
  await page.waitForSelector(".pm-empty");
  expect((await page.locator(".pm-empty").innerText()).includes("Nothing slipped"), "empty state text");
  await shot(page, "pm-empty.png");
  await page.unroute("**/api/pm*");
  await page.route("**/api/pm*", r => r.request().method() === "GET"
    ? r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "/state/pm.json is not a product manager state file: fix or restore it" }) })
    : r.continue());
  await page.reload();
  await page.waitForSelector(".pm-err");
  await shot(page, "pm-error.png");
  await page.unroute("**/api/pm*");
  await page.click("[data-pm-run]");
  await page.waitForSelector(".pm-memo");  // Retry runs the sweep again
  await page.close();

  // 390 px: no horizontal scroll on any tab (the priority table scrolls inside its card)
  const phone = await open({ width: 390, height: 800 });
  await phone.goto(pmUrl);
  await phone.waitForSelector(".pm-memo");
  for (const k of ["brief", "life", "prio", "sig", "after", "rm", "sweep"]) {
    await tab(phone, k);
    const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow <= 0, `horizontal scroll at 390 px on ${k}: ${overflow}px`);
    if (k === "after") await shot(phone, "kit-390.png");
    if (k === "rm") await shot(phone, "roadmaps-390.png");
  }
  await tab(phone, "brief");
  await shot(phone, "pm-390.png");
  await tab(phone, "life");
  await shot(phone, "lifecycle-390.png");
  await phone.close();
} catch (e) {
  problems.push(`test crashed: ${e.stack || e}`);
} finally {
  await browser.close();
  cleanup();
}

if (problems.length) {
  console.error("FAIL\n" + problems.map(p => " - " + p).join("\n"));
  process.exit(1);
}
console.log(`PASS. screenshots in ${outDir}`);
