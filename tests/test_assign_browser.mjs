// Playwright check for "Assign an agent" on a project page (390 and 1440 px).
// Empty box -> sheet -> New agent (dispatcher thread gets the ask, "starting" row appears, brief pre-filled),
// failure + Retry, Existing session (link written, agent row appears), offline note, no horizontal scroll.
// Screenshots land in docs/screenshots/assign-*.png. Run: PLAYWRIGHT=<playwright/index.mjs> node tests/test_assign_browser.mjs

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
if (!process.env.PLAYWRIGHT) { console.error("Set PLAYWRIGHT to the path of playwright/index.mjs"); process.exit(2); }
const { chromium } = await import(process.env.PLAYWRIGHT);
const outDir = path.resolve(repoRoot, "docs", "screenshots");
fs.mkdirSync(outDir, { recursive: true });

const proc = spawn("python3", [path.join(__dirname, "setup_assign_browser.py")], {
  cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, stdio: ["ignore", "pipe", "inherit"] });
const cleanup = () => { try { proc.kill("SIGTERM"); } catch (_) {} };
process.on("exit", cleanup);
const info = await new Promise((resolve, reject) => {
  let buf = "";
  proc.stdout.on("data", c => { buf += c; const i = buf.indexOf("\n"); if (i !== -1) resolve(JSON.parse(buf.slice(0, i))); });
  proc.on("exit", code => { if (code) reject(new Error(`setup exited ${code}`)); });
}).catch(e => { console.error(e); cleanup(); process.exit(1); });
const base = info.url;

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const problems = [];
const check = (name, ok, extra = "") => { if (!ok) problems.push(`${name} ${extra}`); else console.log(`  ok  ${name}`); };

async function open(viewport, project) {
  const page = await browser.newPage({ viewport });
  page.errors = [];
  page.on("console", m => { if (m.type() === "error" && !m.text().includes("Content Security Policy") && !m.text().includes("Failed to load resource")) page.errors.push(m.text()); });
  page.on("pageerror", e => page.errors.push(e.message));
  await page.goto(`${base}#/p/${project}`, { waitUntil: "networkidle" });
  await page.waitForSelector("#drawer[open]");
  return page;
}
const noHscroll = page => page.evaluate(() => document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth + 1);

async function runView(name, viewport) {
  console.log(`[${name}] ${viewport.width}x${viewport.height}`);
  // ---- project ds: empty Agents box, New agent ----
  let page = await open(viewport, "ds");
  check("empty state + primary button", await page.locator("#dwSlide .as-empty [data-assign]").count() === 1);
  await page.screenshot({ path: `${outDir}/assign-${name}-1-empty.png` });
  await page.click("#dwSlide [data-assign=ds]");
  await page.waitForSelector("#assignDrawer[open] #asBrief");
  const brief = await page.inputValue("#asBrief");
  check("brief pre-filled from update and Next", brief.includes("PR #12") && brief.includes("- Claude: start PR2") && brief.includes("FEAT-42"), brief);
  check("three providers", await page.locator("[data-as-prov]").count() === 3);
  check("no horizontal scroll with sheet open", await noHscroll(page));
  await page.click("[data-as-prov=opencode]");
  await page.screenshot({ path: `${outDir}/assign-${name}-2-new.png` });

  // a failing POST shows the error, keeps the brief, Retry succeeds
  let fail = true;
  await page.route("**/api/assign*", route => { if (fail) { fail = false; route.fulfill({ status: 502, contentType: "application/json", body: '{"error":"relay down"}' }); } else route.continue(); });
  await page.fill("#asBrief", brief + "\nEXTRA LINE");
  await page.click("#asGo");
  await page.waitForSelector("#asErr");
  check("failure shown", (await page.textContent("#asErr")).includes("relay down"));
  await page.screenshot({ path: `${outDir}/assign-${name}-3-failed.png` });
  await page.click("[data-as-edit]");
  check("edit brief keeps the text", (await page.inputValue("#asBrief")).endsWith("EXTRA LINE"));
  await page.click("#asGo");
  await page.waitForSelector("#asOk", { timeout: 8000 }).catch(async e => { console.error("page errors:", page.errors, (await page.textContent("#asSlide")).slice(0, 300)); throw e; });
  check("sent note", (await page.textContent("#asOk")).includes("starting"));
  await page.screenshot({ path: `${outDir}/assign-${name}-4-sent.png` });
  // the dispatcher thread received the ask: it is in the outbox, readable through the history endpoint
  const hist = await page.evaluate(async (q) => (await fetch("api/messages" + q + "&thread=disp")).json(), new URL(base).search);
  check("dispatcher got the ask", hist.some(m => m.raw.includes("opencode") && m.raw.includes("factory agent assign ds --thread") && m.raw.includes("EXTRA LINE")), JSON.stringify(hist).slice(0, 300));
  await page.click("[data-as-done]");
  await page.waitForSelector("#assignDrawer:not([open])");
  check("starting row in the Agents box", (await page.textContent("#dwSlide .side-list")).includes("Starting an agent") && (await page.textContent("#dwSlide .side-list")).includes("OpenCode"));
  check("+ Assign in the header now", await page.locator("#dwSlide h2 .btn.sm[data-assign]").count() === 1);
  await page.screenshot({ path: `${outDir}/assign-${name}-5-starting.png` });

  // a second ask while one is starting: the server refuses, the sheet offers "Start another agent"
  await page.click("#dwSlide h2 [data-assign]");
  await page.waitForSelector("#assignDrawer[open] #asBrief");
  await page.click("#asGo");
  await page.waitForSelector("#asErr");
  check("second ask warns it is already starting", (await page.textContent("#asErr")).includes("already starting"));
  check("button offers another agent", (await page.textContent("#asGo")).includes("Start another agent"));
  await page.click("#asGo");
  await page.waitForSelector("#asOk");
  check("forced second ask sent", (await page.textContent("#asOk")).includes("starting"));
  await page.click("[data-as-done]");
  await page.waitForSelector("#assignDrawer:not([open])");

  // ---- Existing session on the same project ----
  await page.click("#dwSlide h2 [data-assign]");
  await page.click("[data-as-tab=existing]");
  const free = await page.locator("[data-as-sess]").count();
  check("two free sessions (dispatcher and busy one excluded)", free === 2, String(free));
  check("send disabled until a session is picked", await page.locator("#asGo").isDisabled());
  await page.screenshot({ path: `${outDir}/assign-${name}-6-existing.png` });
  await page.click("[data-as-sess=free1]");
  await page.click("#asGo");
  await page.waitForSelector("#asOk");
  check("existing: now counts as the project's agent", (await page.textContent("#asOk")).includes("now counts"));
  await page.click("[data-as-done]");
  check("agent row appears", (await page.textContent("#dwSlide .side-list")).includes("acme/chore/sentry-noise"));
  check("no console errors", page.errors.length === 0, page.errors.join(" | "));
  check("no horizontal scroll", await noHscroll(page));
  await page.close();

  // ---- the link survives a reload: the session is on ds in the rebuilt data ----
  const links = JSON.parse(fs.readFileSync(path.join(info.factoryHome, "thread-projects.json"), "utf8"));
  check("link file written", links.free1 === "ds", JSON.stringify(links));
}

try {
  await runView("1440", { width: 1440, height: 900 });
  // ---- 390 px on a project with a running agent ("+ Assign" in the header) and the "no free session" empty state
  console.log("[390] 390x844");
  const page = await open({ width: 390, height: 844 }, "rel");
  check("+ Assign on a project with an agent", await page.locator("#dwSlide h2 .btn.sm[data-assign=rel]").count() === 1);
  await page.locator("#dwSlide .side-list").first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${outDir}/assign-390-0-box.png` });
  await page.click("#dwSlide h2 [data-assign]");
  await page.waitForSelector("#asBrief");
  check("sheet fits 390", await noHscroll(page) && await page.evaluate(() => { const r = document.querySelector("#assignDrawer").getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }));
  await page.screenshot({ path: `${outDir}/assign-390-1-new.png` });
  // Existing tab, then a dispatcher that went offline: server answers 409 with the fix
  await page.route("**/api/assign*", route => route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: "the dispatcher session is offline", fix: "factory config set assign.dispatcher <thread>" }) }));
  await page.click("#asGo");
  await page.waitForSelector("#asErr");
  check("offline fix command shown", (await page.textContent("#asErr")).includes("factory config set assign.dispatcher <thread>"));
  await page.click("[data-as-tab=new]");
  await page.waitForSelector("#asOffline");
  check("offline note on the New tab, start disabled", await page.locator("#asGo").isDisabled());
  await page.screenshot({ path: `${outDir}/assign-390-2-offline.png` });
  await page.keyboard.press("Escape");
  check("Escape closes the sheet, drawer stays", await page.evaluate(() => !document.querySelector("#assignDrawer").open && document.querySelector("#drawer").open));
  check("no console errors", page.errors.length === 0, page.errors.join(" | "));
  await page.close();

  if (problems.length) { console.error(`\nFAILED (${problems.length}):\n` + problems.join("\n")); process.exitCode = 1; }
  else console.log("\nAssign browser checks PASSED.");
} finally {
  await browser.close();
  cleanup();
}
