// Playwright check for session search and messaging from Waiting on you (1440 and 390 px).
// Question asked outside T3: search keeps an unsaved answer, Escape clears without closing the drawer,
// Ctrl+Enter in the search saves nothing, Enter picks, a double click sends once, the row badge and inbox.json
// name the linked session. Question with a session: Ctrl+Enter in the message box sends a message, not the answer.
// Server reports another thread (a lost link race): the badge follows it; a 400 for a question linked elsewhere
// retargets the row and keeps the text. Refresh pause: a pick alone never holds it, a failed message holds it for 5 minutes. Run: PLAYWRIGHT=<playwright/index.mjs> node tests/test_session_msg_browser.mjs

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
if (!process.env.PLAYWRIGHT) { console.error("Set PLAYWRIGHT to the path of playwright/index.mjs"); process.exit(2); }
const { chromium } = await import(process.env.PLAYWRIGHT);

const proc = spawn("python3", [path.join(__dirname, "setup_session_msg_browser.py")], {
  cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, stdio: ["ignore", "pipe", "inherit"] });
const cleanup = () => { try { proc.kill("SIGTERM"); } catch (_) {} };
process.on("exit", cleanup);
const info = await new Promise((resolve, reject) => {
  let buf = "";
  proc.stdout.on("data", c => { buf += c; const i = buf.indexOf("\n"); if (i !== -1) resolve(JSON.parse(buf.slice(0, i))); });
  proc.on("exit", code => { if (code) reject(new Error(`setup exited ${code}`)); });
}).catch(e => { console.error(e); cleanup(); process.exit(1); });

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const problems = [];
const check = (name, ok, extra = "") => { if (!ok) problems.push(`${name} ${extra}`); else console.log(`  ok  ${name}`); };
const readJson = (f, dflt) => { try { return JSON.parse(fs.readFileSync(path.join(info.factoryHome, f), "utf8")); } catch (_) { return dflt; } };
const answered = id => id in readJson("answers.json", {});
// what another tab or agent does between two page loads; skips the store lock, safe only because nothing else
// writes inbox.json while the race case runs
const linkItem = (id, thread) => {
  const f = path.join(info.factoryHome, "inbox.json"), box = JSON.parse(fs.readFileSync(f, "utf8"));
  box.items.find(i => i.id === id).thread = thread;
  fs.writeFileSync(f + ".t", JSON.stringify(box)); fs.renameSync(f + ".t", f);
};

async function open(viewport, lever) {
  const page = await browser.newPage({ viewport });
  page.errors = [];
  page.on("console", m => { if (m.type() === "error" && !m.text().includes("Content Security Policy") && !m.text().includes("Failed to load resource")) page.errors.push(m.text()); });
  page.on("pageerror", e => page.errors.push(e.message));
  await page.goto(`${info.url}#/p/ds`, { waitUntil: "networkidle" });
  await page.waitForSelector("#drawer[open]");
  await page.click(`#drawer [data-lever=${lever}]`);
  return page;
}

async function runView(name, viewport, { free, known, race }) {
  console.log(`[${name}] ${viewport.width}x${viewport.height} (${free}, ${known}, ${race})`);
  const d = `#drawer [data-lm=${free}]`;
  let page = await open(viewport, free);
  const busyHc = (await page.locator(`#drawer [data-lever=${known}] .badge`).first().innerText()).trim();
  // the server links the picked session, but this reply says the question went to busy: the badge must follow the reply
  await page.route("**/api/messages*", async route => {
    if (route.request().method() !== "POST") return route.continue();
    const res = await route.fetch(), body = await res.json();
    route.fulfill({ response: res, json: { ...body, itemThread: "busy" } });
  });
  // an unsaved answer survives typing in the session search
  await page.check(`#drawer [data-form=${free}] input[value=keep]`);
  await page.fill(`#drawer [data-form=${free}] textarea[name=note]`, "half typed note");
  await page.click(`${d} [data-lmq]`);
  await page.keyboard.type("zz");
  check("no-match note", await page.locator(d).innerText().then(t => t.includes("No session matches")));
  await page.keyboard.press("Escape");
  check("Escape in search keeps the drawer open", await page.evaluate(() => document.querySelector("#drawer").open));
  check("Escape clears the search", await page.inputValue(`${d} [data-lmq]`) === "");
  await page.keyboard.press("Control+Enter");
  await page.waitForTimeout(300);
  check("Ctrl+Enter in search saves no answer", !answered(free));
  await page.keyboard.type("inbox");
  check("search narrows to one session", (await page.locator(`${d} .ss-count`).innerText()).includes("1 of"));
  check("focus stays in the search", await page.evaluate(() => document.activeElement.dataset.lmq) === free);
  check("answer draft kept", await page.inputValue(`#drawer [data-form=${free}] textarea[name=note]`) === "half typed note"
    && await page.isChecked(`#drawer [data-form=${free}] input[value=keep]`));
  await page.keyboard.press("Enter");
  const hc = (await page.locator(`${d} .row .badge`).first().innerText()).trim();
  check("a picked session alone does not hold the refresh", await page.evaluate(() => !lmDraft()));
  await page.fill(`${d} [data-lm-text]`, "Expiry script ready, run it Thursday.");
  check("Enter picks and the message box takes focus", await page.evaluate(() => document.activeElement.dataset.lmText) === free);
  await page.dblclick(`${d} [data-lm-send]`);
  await page.waitForSelector(`${d} .msg-item`);
  await page.waitForTimeout(300);
  const sent = readJson("relay.json", []).filter(e => (e.raw || "").startsWith(`About ${free} (`));
  check("double click sends once", sent.length === 1, `got ${sent.length}`);
  check("message quotes the question", sent[0]?.raw.endsWith("): Expiry script ready, run it Thursday."), sent[0]?.raw);
  const item = readJson("inbox.json", { items: [] }).items.find(i => i.id === free);
  check("inbox.json links the question to the picked session", item?.thread === sent[0]?.thread && Boolean(item?.thread));
  const badge = (await page.locator(`#drawer [data-lever=${free}] .badge`).first().innerText()).trim();
  check("row badge names the session the server reported", badge === busyHc && badge !== hc, `${badge} vs ${busyHc}`);
  check("a sent message does not hold the refresh", await page.evaluate(() => !lmDraft()));
  // a failed message holds the refresh for 5 minutes, not forever
  const holds = await page.evaluate(id => {
    const m = LM[id], at = ago => new Date(Date.now() - ago).toISOString();
    m.sent.push({ id: "f1", state: "failed", at: at(60 * 60e3), failedAt: Date.now() - 60e3 }); const fresh = lmDraft();
    m.sent.at(-1).failedAt = Date.now() - 6 * 60e3; const old = lmDraft();
    m.sent.pop(); return { fresh, old };
  }, free);
  check("failed message holds the refresh for 5 minutes after it failed", holds.fresh && !holds.old, JSON.stringify(holds));
  check("no horizontal scroll", await page.evaluate(() => document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth + 1));
  check("no console errors", page.errors.length === 0, page.errors.join(" | "));
  await page.close();

  // question that has a session: Ctrl+Enter in the message box sends a message, never the answer
  page = await open(viewport, known);
  const k = `#drawer [data-lm=${known}]`;
  await page.fill(`${k} [data-lm-text]`, "Go with 6 h");
  await page.press(`${k} [data-lm-text]`, "Control+Enter");
  await page.waitForSelector(`${k} .msg-item`);
  check("Ctrl+Enter sends the message", readJson("relay.json", []).some(e => e.raw === `About ${known} (Pick the retry window (${name})): Go with 6 h` && e.thread === "busy"));
  check("Ctrl+Enter in the message box saves no answer", !answered(known));
  check("no console errors (known session)", page.errors.length === 0, page.errors.join(" | "));
  await page.close();

  // the page loaded the question with no session, then it was linked to busy: the send is refused, the row retargets
  page = await open(viewport, race);
  const r = `#drawer [data-lm=${race}]`;
  await page.fill(`${r} [data-lmq]`, "inbox");
  await page.press(`${r} [data-lmq]`, "Enter");
  await page.fill(`${r} [data-lm-text]`, "Ship it");
  // linked to a session that cannot take a message: no retarget, the error and the text stay on screen
  linkItem(race, "ghost");
  await page.click(`${r} [data-lm-send]`);
  await page.waitForSelector(`${r} .as-note.err >> text=belongs to another session`);
  check("refused send to a closed session keeps the box and the text", await page.inputValue(`${r} [data-lm-text]`) === "Ship it");
  linkItem(race, "busy");
  await page.click(`${r} [data-lm-send]`);
  await page.waitForFunction(id => DATA.levers.find(l => l.id === id).thread === "busy", race);
  check("refused send shows the reason", (await page.locator(r).innerText()).includes("belongs to another session"));
  check("refused send keeps the text", await page.inputValue(`${r} [data-lm-text]`) === "Ship it");
  check("row retargets to the session the question has", (await page.locator(`#drawer [data-lever=${race}] .badge`).first().innerText()).trim() === busyHc);
  check("nothing queued for the refused send", !readJson("relay.json", []).some(e => (e.raw || "").startsWith(`About ${race} (`)));
  // the status poll reports a failure: lmPoll stamps failedAt, so the refresh holds
  await page.route("**/api/messages*", async route => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch(), list = await res.json();
    route.fulfill({ response: res, json: list.map(e => (e.raw || "").startsWith(`About ${race} (`) ? { ...e, state: "failed", lastError: "test" } : e) });
  });
  await page.click(`${r} [data-lm-send]`);
  await page.waitForSelector(`${r} .msg-badge.failed`);
  check("a real failure holds the refresh", await page.evaluate(() => lmDraft()));
  check("second send goes to the question's session", readJson("relay.json", []).some(e => (e.raw || "").startsWith(`About ${race} (`) && e.thread === "busy"));
  check("no console errors (race)", page.errors.length === 0, page.errors.join(" | "));
  await page.close();
}

try {
  await runView("desktop", { width: 1440, height: 1000 }, info.items.desktop);
  await runView("mobile", { width: 390, height: 844 }, info.items.mobile);
  if (problems.length) { console.error(`\nFAILED (${problems.length}):\n` + problems.join("\n")); process.exitCode = 1; }
  else console.log("\nSession messaging browser checks PASSED.");
} finally {
  await browser.close();
  cleanup();
}
