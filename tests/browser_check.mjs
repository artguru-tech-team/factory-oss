// Browser check of the built page: 390 and 1440 px, console errors, horizontal scroll,
// screenshots, and one answer saved through POST /api/answers.
// Usage: PLAYWRIGHT=/path/to/playwright/index.mjs [FACTORY_BIN=bin/factory] [STORAGE_STATE=access.json] node tests/browser_check.mjs <url with ?k=> <out dir> [lever id]
const { chromium } = await import(process.env.PLAYWRIGHT || "playwright");
const [url, out, leverId] = process.argv.slice(2);
const PRODUCT = process.env.PRODUCT || "acme", PROJECT = process.env.PROJECT || "ds";
if (!url || !out) throw new Error("usage: browser_check.mjs <url?k=...> <out dir> [lever id]");

const browser = await chromium.launch();
const problems = [];
for (const [name, width, height] of [["phone", 390, 844], ["desktop", 1440, 900]]) {
  // STORAGE_STATE: a saved Cloudflare Access session, to run the check through factory.example.com
  const page = await browser.newPage({ viewport: { width, height }, storageState: process.env.STORAGE_STATE });
  // Through factory.example.com, Cloudflare injects GTM scripts that the page's CSP blocks on purpose (serve.csp).
  // A blocked script of our own would leave the page without its cards, which the waits below catch.
  page.on("console", m => { if (m.type() === "error" && !m.text().includes("violates the following Content Security Policy")) problems.push(`${name} console: ${m.text()}`); });
  page.on("pageerror", e => problems.push(`${name} page error: ${e.message}`));
  // Playwright's Chromium reports a CSP block as "csp"; Chrome itself as net::ERR_BLOCKED_BY_CSP
  page.on("requestfailed", r => { if (!["csp", "net::ERR_BLOCKED_BY_CSP"].includes(r.failure()?.errorText)) problems.push(`${name} request failed: ${r.url()}`); });
  // a request the CSP blocked never leaves the browser; one that got a response did
  page.on("response", r => { if (!r.url().startsWith(new URL(url).origin)) problems.push(`${name} external request: ${r.url()}`); });
  const overflow = async view => {
    const w = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    if (w > 0) problems.push(`${name} ${view}: horizontal scroll of ${w}px`);
  };
  const base = url.split("#")[0];
  // every element of every chart must sit inside its card, on both sides, with no NaN coordinate
  const charts = async view => {
    const bad = await page.evaluate(() => [...document.querySelectorAll("svg.chart")].filter(svg => svg.getClientRects().length).flatMap(svg => {
      const card = svg.closest(".box,.card");
      if (!card) return ["a chart outside any card"];
      const box = card.getBoundingClientRect();
      return [...svg.querySelectorAll("*")].flatMap(el => {
        if ([...el.attributes].some(a => /NaN|Infinity/.test(a.value))) return [`${el.tagName} has a NaN coordinate`];
        const r = el.getBoundingClientRect();
        if (!r.width && !r.height) return [];
        return r.left < box.left - 1 || r.right > box.right + 1 ? [`${el.tagName} ${el.textContent.trim().slice(0, 20)} outside its card`] : [];
      });
    }));
    for (const b of [...new Set(bad)]) problems.push(`${name} ${view}: ${b}`);
  };

  await page.goto(base + "#/" + PRODUCT);
  await page.waitForSelector(".card");
  await overflow("home");
  await charts("home");
  await page.screenshot({ path: `${out}/${name}-home.png`, fullPage: true });

  await page.goto(base + "#/p/" + PROJECT);
  await page.waitForSelector("#drawer[open] .pp-title");
  await page.waitForTimeout(400);
  await overflow("drawer");
  await charts("drawer");
  await page.screenshot({ path: `${out}/${name}-ds-drawer.png` });

  const scriptCount = await page.evaluate(() => document.querySelectorAll("script:not([src])").length);
  if (scriptCount !== 1) problems.push(`${name}: expected 1 inline script, found ${scriptCount}`);

  const onAttrs = await page.evaluate(() => {
    const found = [];
    for (const el of document.querySelectorAll("*")) {
      for (const attr of el.attributes) {
        if (/^on/i.test(attr.name)) found.push(`${el.tagName.toLowerCase()}[${attr.name}]`);
      }
    }
    return found;
  });
  if (onAttrs.length > 0) problems.push(`${name}: found on*= attributes: ${onAttrs.join(", ")}`);

  const closedMsgCheck = await page.evaluate(() => [
    closedMsg({ resolvedAt: "2026-10-30T10:00:00Z" }, true),
    closedMsg({}, true),
    closedMsg({ answeredAt: "2026-10-30T10:00:00Z" }, true),
  ]);
  if (closedMsgCheck[1] !== "Already handled by the agent. Your draft is kept; saving will not change anything.") {
    problems.push(`${name} closedMsg wording: ${closedMsgCheck[1]}`);
  }

  await page.goto(base + "#/" + PRODUCT + "/agents");
  await page.waitForSelector(".tbl", { state: "attached" });
  await overflow("agents");
  const agentStats = await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".tbl tbody tr:not(.agent-msg-tr)")];
    let enabled = 0, disabled = 0, subagents = 0, subagentsWithHint = 0;
    const errors = [];
    for (const r of rows) {
      const isSub = r.textContent.includes("Subagent") || r.querySelector(".agent")?.textContent.trim().startsWith("3");
      const btn = r.querySelector("button.msg-btn");
      if (btn && btn.disabled) {
        disabled++;
        const hint = r.querySelector(".disabled-hint")?.textContent.trim() || "";
        if (isSub) {
          subagents++;
          if (hint.startsWith("subagent: message its parent")) {
            subagentsWithHint++;
          } else {
            errors.push(`subagent missing parent hint: ${hint}`);
          }
        }
      } else if (btn && !btn.disabled) {
        enabled++;
      }
    }
    return { enabled, disabled, subagents, subagentsWithHint, errors };
  });
  console.log(`${name} agents: ${agentStats.enabled} enabled, ${agentStats.disabled} disabled (${agentStats.subagents} subagents)`);
  if (agentStats.errors.length) problems.push(...agentStats.errors.map(e => `${name}: ${e}`));

  const topAgentBtn = page.locator(".tbl tbody tr button.msg-btn:not([disabled])").first();
  if (await topAgentBtn.count() > 0) {
    await topAgentBtn.click();
    await page.waitForSelector(".agent-msg-panel");
    await page.screenshot({ path: `${out}/${name}-agent-panel.png` });
    await topAgentBtn.click();
  }
  await page.screenshot({ path: `${out}/${name}-agents.png`, fullPage: true });

  await page.goto(base + "#/all");
  await page.waitForSelector(".list", { state: "attached" });
  await overflow("all");

  if (leverId && name === "desktop") {
    await page.goto(base + "#/" + PRODUCT);
    await page.click(`[data-lever="${leverId}"]`);
    const form = page.locator(`[data-form="${leverId}"]`);
    await form.locator("input[name=c]").nth(1).check();
    await form.locator("textarea").fill("browser check note");
    const [resp] = await Promise.all([
      page.waitForResponse(r => r.url().includes("/api/answers") && r.request().method() === "POST"),
      form.locator("button[type=submit]").click(),
    ]);
    if (resp.status() !== 200) problems.push(`save answered ${resp.status()}`);
    await page.waitForSelector(`.ib.done[data-id="${leverId}"]`);
    if (process.env.FACTORY_BIN) {  // the agent side of the round trip: factory answers must return it
      const { execFileSync } = await import("node:child_process");
      const got = JSON.parse(execFileSync(process.env.FACTORY_BIN, ["--json", "answers"], { encoding: "utf8" }));
      const a = got.find(x => x.id === leverId);
      if (!a || a.note !== "browser check note") problems.push(`factory answers did not return ${leverId}`);
    }
    await page.screenshot({ path: `${out}/${name}-answered.png` });
  }
  await page.close();
}
await browser.close();
console.log(problems.length ? problems.join("\n") : "browser check: no problems");
process.exit(problems.length ? 1 : 0);
