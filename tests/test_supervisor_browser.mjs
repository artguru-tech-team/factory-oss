// Playwright check for the Supervisor (Automation) tab: cards, expand, tables, VPS, dead banner.
// Run: PLAYWRIGHT=<path to playwright/index.mjs> node tests/test_supervisor_browser.mjs

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
if (!process.env.PLAYWRIGHT) { console.error("Set PLAYWRIGHT to the path of playwright/index.mjs"); process.exit(2); }
const { chromium } = await import(process.env.PLAYWRIGHT);

const proc = spawn("python3", [path.join(__dirname, "setup_supervisor_browser.py")], {
  cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, stdio: ["ignore", "pipe", "inherit"],
});
const cleanup = () => { try { proc.kill("SIGTERM"); } catch (_) {} };
process.on("exit", cleanup);
const info = await new Promise((resolve, reject) => {
  let buf = "";
  proc.stdout.on("data", c => { buf += c; if (buf.includes("\n")) resolve(JSON.parse(buf.split("\n")[0])); });
  proc.on("exit", code => reject(new Error(`setup exited ${code}`)));
});

const browser = await chromium.launch({ args: ["--no-sandbox"], executablePath: process.env.CHROMIUM || undefined });
const problems = [];
const check = (ok, msg) => { if (!ok) problems.push(msg); };
try {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    const errors = [];
    page.on("console", m => { if (m.type() === "error" && !m.text().includes("Content Security Policy")) errors.push(m.text()); });
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(info.url + "#/bk/automation", { waitUntil: "networkidle" });
    await page.click('[data-autotab="supervisor"]');
    await page.waitForSelector("[data-sv-card]");
    const w = viewport.width;
    const text = () => page.locator("body").innerText();

    check(await page.locator("[data-sv-card]").count() === 6, `${w}: want 6 cards (the 30 h old resolved one is dropped)`);
    const first = await page.locator("[data-sv-card]").nth(1).innerText();
    check(first.includes("acme: escalated one") && first.includes("escalated · inbox a42") && !first.includes("quiet"), `${w}: escalated crit second (older than the disk one), naming its inbox item: ${first}`);
    const disk = await page.locator("[data-sv-card]").first().innerText();
    check(disk.includes("VPS disk is filling up") && disk.includes("would infra") && !/of \d in 24 h/.test(disk), `${w}: newest crit first, infra card, no budget line: ${disk}`);
    const closed = await page.locator("[data-sv-card]", { hasText: "acme: closed one" }).innerText();
    check(closed.includes("resolved") && !closed.includes("would infra"), `${w}: closed card shows its status, not an old action: ${closed}`);
    const quoted = await page.locator("[data-sv-card]", { hasText: "quoted" }).innerText();
    check(quoted.includes("<i>quoted</i>") && quoted.includes("1 of 2 in 24 h"), `${w}: title escaped, dispatch budget shown: ${quoted}`);
    let t = await text();
    check(/ALIVE/.test(t) && /last run \d\d:\d\d UTC \(5 min ago\)/.test(t) && /next \d\d:\d\d UTC/.test(t), `${w}: header line`);
    check(t.includes("Dry run: nothing is sent") && t.includes("factory config set supervisor.live true"), `${w}: dry-run badge`);
    check(t.includes("4 open") && t.includes("6 total"), `${w}: counts`);
    check(!t.includes("factory supervise mute"), `${w}: cards start collapsed`);

    const diskCard = () => page.locator("[data-sv-card]").first();
    await diskCard().click();
    t = await text();
    check(t.includes("factory supervise mute vps:disk --hours 24") && t.includes("factory supervise false-alarm vps:disk") && /Status open/.test(t), `${w}: expanded CLI lines and status`);
    check(await diskCard().getAttribute("aria-expanded") === "true", `${w}: aria-expanded`);
    await page.locator(".sv-detail pre").click();
    check((await text()).includes("factory supervise mute"), `${w}: clicking the commands (to copy them) keeps the card open`);
    await diskCard().click({ position: { x: 5, y: 5 } });
    check(!(await text()).includes("factory supervise mute"), `${w}: second click collapses`);
    await diskCard().focus();
    await page.keyboard.press("Enter");
    check((await text()).includes("factory supervise mute"), `${w}: Enter on a focused card expands it`);
    check(await page.evaluate(() => document.activeElement?.hasAttribute("data-sv-card")), `${w}: focus stays on the card after re-render`);
    await page.keyboard.press(" ");
    check(!(await text()).includes("factory supervise mute"), `${w}: Space collapses it`);
    await page.locator("[data-sv-card]", { hasText: "quoted" }).click();
    check((await text()).includes(`factory supervise mute 'acme:"q"<x>' --hours 24`), `${w}: a signature with quotes expands, escaped and shell-quoted`);
    await page.locator("[data-sv-card]", { hasText: "quoted" }).click({ position: { x: 5, y: 5 } });
    await page.evaluate(() => { const r = document.createRange(); r.selectNodeContents(document.querySelector("h2")); getSelection().removeAllRanges(); getSelection().addRange(r); });
    await diskCard().focus();
    await page.keyboard.press("Enter");
    check((await text()).includes("factory supervise mute vps:disk"), `${w}: Enter toggles even while text is selected`);
    await page.keyboard.press("Enter");
    await page.evaluate(() => getSelection().removeAllRanges());
    await diskCard().evaluate(e => e.scrollIntoView({ block: "center" }));
    const ev = await diskCard().locator("div.fog").first().boundingBox();  // drag across the evidence text to select it
    await page.mouse.move(ev.x + 2, ev.y + ev.height / 2); await page.mouse.down();
    await page.mouse.move(ev.x + ev.width - 2, ev.y + ev.height / 2, { steps: 5 }); await page.mouse.up();
    const sel = await page.evaluate(() => String(getSelection()));
    check(sel.length > 0, `${w}: the drag selected text`);
    check(!(await text()).includes("factory supervise mute vps:disk"), `${w}: drag-selecting card text does not toggle it`);
    await page.evaluate(() => getSelection().removeAllRanges());

    t = await text();
    check(/chatscan/.test(t) && /digest/.test(t) && /\blate\b/.test(t), `${w}: job table`);
    check(t.includes("Project health") && t.includes("disagree") && t.includes("at risk"), `${w}: health table with disagree`);
    check(await page.locator("tr", { hasText: "web" }).innerText().then(x => !x.includes("disagree")), `${w}: agreeing row not flagged`);
    check(t.includes("92.1%") && t.includes("55%") && t.includes("arrive with the infra agent"), `${w}: VPS block`);
    const wide = await page.evaluate(() => [...document.querySelectorAll("#main *")].filter(e => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1).slice(0, 3).map(e => e.tagName + "." + e.className + " " + Math.round(e.getBoundingClientRect().right) + " " + e.textContent.slice(0, 40)));
    check(!(await page.evaluate(() => document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth + 1)), `${w}: horizontal scroll ${JSON.stringify(wide)}`);

    await page.evaluate(() => { DATA.supervisor.status = "dead"; DATA.supervisor.minutes = 400; render(); });
    check((await text()).includes("Supervisor is dead") && (await text()).includes("400 min") && (await text()).includes("overdue"), `${w}: dead banner, next shown as overdue`);
    await page.evaluate(() => { DATA.supervisor.minutes = null; render(); });
    check((await text()).includes("over an hour") && !(await text()).includes("null"), `${w}: dead with no minutes`);
    await page.evaluate(() => { DATA.supervisor.findings = []; DATA.supervisor.lastRun = null; render(); });
    check((await text()).includes("The supervisor has not run yet") && (await text()).includes("Install the cron line from deploy/crontab.txt."), `${w}: never-ran state`);
    await page.evaluate(() => { DATA.supervisor.error = "<b>bad</b>"; render(); });
    check((await text()).includes("<b>bad</b>"), `${w}: error shown escaped`);
    check(errors.length === 0, `${w}: console errors ${errors}`);
    console.log(`[${problems.length ? "FAIL" : "PASS"}] ${w}px`);
    await page.close();
  }
} finally { await browser.close(); cleanup(); }
if (problems.length) { console.error(problems.join("\n")); process.exit(1); }
console.log("Supervisor browser checks PASSED.");
