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

const setupScript = path.join(__dirname, "setup_comms_browser.py");
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
  console.error("Failed to start comms test environment:", err);
  cleanup();
  process.exit(1);
}

const { url, key } = serverInfo;
console.log(`Test environment ready at ${url}`);

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const problems = [];

function assert(cond, msg) {
  if (!cond) {
    console.error(`FAIL: ${msg}`);
    problems.push(msg);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

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
  // Test Suite 1: Loading Skeleton & Error State + Retry
  // ----------------------------------------------------
  console.log("\n=== Test Suite 1: Loading skeleton and Error / Retry ===");
  const { page: pageInit, errors: errs1 } = await newPage({ width: 1280, height: 800 });

  let delayComms = true;
  let failComms = false;

  await pageInit.route("**/api/comms*", async route => {
    if (failComms) {
      await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "Internal server error" }) });
      return;
    }
    if (delayComms) {
      await new Promise(r => setTimeout(r, 2000));
    }
    await route.continue();
  });

  const authUrl = url;
  await pageInit.goto(authUrl, { waitUntil: "networkidle" });

  // Navigate to Automation tab
  await pageInit.click('button[data-sec="automation"]');
  await pageInit.waitForSelector('button[data-autotab="release-comms"]');

  // Trigger loading state by switching to release-comms tab
  console.log("Checking loading skeleton...");
  await pageInit.click('button[data-autotab="release-comms"]');
  const skeletonVisible = await pageInit.locator(".rc-skeleton-shimmer").first().waitFor({ state: "visible", timeout: 1500 }).then(() => true, () => false);
  assert(skeletonVisible, "Loading skeleton is visible during delayed /api/comms fetch");

  // Wait for load to finish
  delayComms = false;
  await pageInit.waitForSelector(".rc", { state: "visible" });
  await pageInit.waitForSelector(".rc-product-selector", { state: "visible" });
  assert(true, "Release comms loaded successfully after skeleton");

  // Test error state + Retry
  console.log("Checking error state and retry...");
  failComms = true;
  await pageInit.evaluate(() => {
    // Force comms reload
    DATA.comms = null;
    loadCommsFromServer();
  });
  await pageInit.waitForSelector("#rc-error", { state: "visible" });
  const errorText = await pageInit.locator("#rc-error").textContent();
  assert(errorText.includes("Failed to load release comms"), "Error banner shown on 500 failure");

  // Click retry with failing comms resolved
  failComms = false;
  await pageInit.click("#rc-retry");
  await pageInit.waitForSelector("#rc-error", { state: "detached" });
  await pageInit.waitForSelector(".rc-product-selector", { state: "visible" });
  assert(true, "Clicking Retry recovers from error state");

  // ----------------------------------------------------
  // ----------------------------------------------------
  // Test Suite 2: Product Selector & Empty Week / Not Started
  // ----------------------------------------------------
  console.log("\n=== Test Suite 2: Product Selector & Empty Week / Not Started ===");
  // Verify product tabs exist
  const prodTabs = await pageInit.locator(".rc-product-tab").allTextContents();
  assert(prodTabs.some(t => t.includes("Web")), "Web product tab exists");
  assert(prodTabs.some(t => t.includes("Store")), "Store product tab exists");
  assert(prodTabs.some(t => t.includes("App")), "App product tab exists");
  const dotColors = await pageInit.$$eval(".rc-p-dot", els => els.map(e => e.style.background));
  assert(dotColors.some(c => c.includes("23, 201, 100")), `App tab dot uses its configured colour (${dotColors.join(" | ")})`);

  // Switch to Store (not started in fixture -> not_started notice)
  console.log("Switching to Store to test not_started notice...");
  await pageInit.click('button[data-rc-product="store"]');
  await pageInit.waitForTimeout(100);
  const notStartedVisible = await pageInit.locator("text=Comms not started for Store yet").isVisible();
  assert(notStartedVisible, "Not-started notice is displayed for Store");

  // Switch to App (started, has unsynced release -> changelog not built yet notice)
  console.log("Switching to App to test 'changelog not built yet' notice...");
  await pageInit.click('button[data-rc-product="app"]');
  await pageInit.waitForTimeout(100);
  const unsyncedNotice = await pageInit.locator(".rc-empty-week-notice").textContent();
  assert(unsyncedNotice.includes("changelog not built yet"), "A product with only an unsynced release shows the 'changelog not built yet' notice");

  // Switch to Web (PRs 1392, 1422 from 6 Nov are pending -> NO empty-week notice on Tue 10 Nov)
  console.log("Switching to Web to test that pending PRs show no empty notice...");
  await pageInit.click('button[data-rc-product="web"]');
  await pageInit.waitForTimeout(100);
  const emptyNoticeVisible = await pageInit.locator(".rc-empty-week-notice").isVisible();
  assert(!emptyNoticeVisible, "The fixture's Tue 10 Nov view shows no 'Nothing shipped' notice, because the 6 Nov PRs are pending");

  // Assert no "recipients" text in the product header
  const audienceBannerText = await pageInit.locator(".rc-audience-banner").textContent();
  assert(!audienceBannerText.toLowerCase().includes("recipients"), 'no "recipients" text in the product header');

  // ----------------------------------------------------
  // Test Suite 3: Home Timeline Screen (Real Values & Job Clicks)
  // ----------------------------------------------------
  console.log("\n=== Test Suite 3: Home Timeline Screen ===");
  // Verify next Friday timing is present and has NO unbacked "Scheduled" chip
  const nextFridayItem = pageInit.locator(".rc-timeline-item.next-friday");
  const nextFridayText = await nextFridayItem.textContent();
  assert(nextFridayText.includes("Tunis"), `Next Friday Tunis timeline item rendered: ${nextFridayText.trim()}`);
  assert(!nextFridayText.includes("Scheduled"), "Friday card without digest job has NO unbacked Scheduled chip");

  // Verify Friday card at 2026-11-14T12:00Z never says "nothing user-facing shipped"
  console.log("Checking Friday card at 2026-11-14T12:00Z...");
  const { page: pageSat } = await newPage({ width: 1280, height: 800 }, "2026-11-14T12:00:00Z");
  await pageSat.goto(authUrl, { waitUntil: "networkidle" });
  await pageSat.click('button[data-sec="automation"]');
  await pageSat.waitForSelector('button[data-autotab="release-comms"]');
  await pageSat.click('button[data-autotab="release-comms"]');
  await pageSat.waitForSelector(".rc-product-tab");
  const satFriCard = await pageSat.locator(".rc-timeline-item.next-friday").textContent();
  assert(!satFriCard.includes("nothing user-facing shipped"), "At 2026-11-14T12:00Z, the Friday card never says 'nothing user-facing shipped'");
  await pageSat.close();

  // Verify sent job with clean delivery copy and Tunis time
  const sentItem = pageInit.locator(".rc-timeline-item.sent").first();
  const sentVisible = await sentItem.isVisible();
  assert(sentVisible, "Sent job timeline item is visible");
  const sentText = await sentItem.textContent();
  assert(sentText.includes("1420 recipients"), `Sent job displays real recipient count (1420): ${sentText}`);
  assert(sentText.includes("Sent to 1420 recipients on Fri 30 Oct 13:10 Tunis."), "Sent job displays clean delivery copy with Tunis time");
  assert(!sentText.includes("Confirmed delivery to"), "invented delivery copy 'Confirmed delivery to' is removed");
  assert(!sentText.includes("Dispatch worker"), "invented delivery copy 'Dispatch worker' is removed");

  // Verify failed job with real reason
  const failedItem = pageInit.locator(".rc-timeline-item.failed").first();
  const failedVisible = await failedItem.isVisible();
  assert(failedVisible, "Failed job timeline item is visible");
  const failedText = await failedItem.textContent();
  assert(failedText.includes("SMTP error: relay server connection timed out after 30s"), "Failed job displays real failure reason");

  // Verify deferred job with target date and PR-title fallback
  const deferredItem = pageInit.locator(".rc-timeline-item.deferred").first();
  const deferredVisible = await deferredItem.isVisible();
  assert(deferredVisible, "Deferred job timeline item is visible");
  const deferredText = await deferredItem.textContent();
  assert(deferredText.includes("2026-11-20"), "Deferred job displays real scheduled date (2026-11-20)");
  assert(deferredText.includes("Direct bank payouts (#1180)"), "the PR-title fallback for a job without a subject");
  assert(!deferredText.includes("f-web-1180"), "job id is never displayed as job title");

  // Assert no raw ISO timestamp with Z is visible anywhere in the tab
  const wholeTabContent = await pageInit.locator(".rc").innerText();
  const hasZTimestamps = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z/.test(wholeTabContent);
  assert(!hasZTimestamps, "no Z timestamps visible in the tab");
  assert(wholeTabContent.includes("Fri 30 Oct 13:10"), "Tunis local time is visible in the tab");

  // Verify needs-approval job
  const waitingItem = pageInit.locator(".rc-timeline-item.waiting").first();
  const waitingVisible = await waitingItem.isVisible();
  assert(waitingVisible, "Needs-approval job timeline item is visible");
  const waitingText = await waitingItem.textContent();
  assert(waitingText.includes("Requires owner"), "Needs-approval job has 'Requires owner' status chip");

  // Test clicking "Preview Email ->" on sent job f-web-1001 carries job id
  console.log("Clicking 'Preview Email ->' on sent job f-web-1001...");
  await pageInit.click('.rc-timeline-card button[data-rc-target-job="f-web-1001"]');
  await pageInit.waitForSelector("#rc-screen-email", { state: "visible" });
  const sentTopBar = await pageInit.locator(".rc-job-header-bar").textContent();
  assert(sentTopBar.includes("f-web-1001"), "Active job id in email top bar matches clicked job f-web-1001");
  assert(sentTopBar.includes("Sent (1420 recipients)"), "Top bar shows Sent with 1420 recipients");
  const sentEmailContent = await pageInit.locator("#rcEmailCanvas").textContent();
  assert(sentEmailContent.includes("1420 recipients"), "Sent email preview shows recorded count (1420)");
  assert(!sentEmailContent.includes("Recipient count is read from the product at send time."), "Sent email does NOT show read-at-send notice");

  // Return to home screen
  await pageInit.click('button[data-rc-screen="home"]');
  await pageInit.waitForSelector("#rc-screen-home", { state: "visible" });

  // ----------------------------------------------------
  // Test Suite 4: Changelog Screen & Titles Toggle
  // ----------------------------------------------------
  console.log("\n=== Test Suite 4: Changelog Screen & Plain Copy vs PR Titles ===");
  // Test clicking "View Changelog ->" with specific target cl
  await pageInit.click('.rc-timeline-card button[data-rc-target-cl="2026-11-06T10:00:00Z"]');
  await pageInit.waitForSelector("#rc-screen-changelog", { state: "visible" });

  // In default view, plain copy should be displayed
  const changelogContentBefore = await pageInit.locator("#rcChangelogContainer").textContent();
  assert(
    changelogContentBefore.includes("Lien de contact direct accessible depuis le menu mobile"),
    "Changelog displays plain copy in default view"
  );
  assert(
    !changelogContentBefore.includes("fix(feed): add Contact to the mobile More sheet"),
    "Changelog does not display raw PR title in default view"
  );

  // Toggle titles
  console.log("Clicking titles toggle...");
  await pageInit.click('button[data-rc-titles-toggle]');
  await pageInit.waitForTimeout(100);

  const changelogContentAfter = await pageInit.locator("#rcChangelogContainer").textContent();
  assert(
    changelogContentAfter.includes("fix(feed): add Contact to the mobile More sheet"),
    "Changelog displays raw PR title after clicking toggle"
  );

  // Toggle back
  await pageInit.click('button[data-rc-titles-toggle]');
  await pageInit.waitForTimeout(100);
  const changelogContentBack = await pageInit.locator("#rcChangelogContainer").textContent();
  assert(
    changelogContentBack.includes("Lien de contact direct accessible depuis le menu mobile"),
    "Changelog restores plain copy after toggling back"
  );

  // ----------------------------------------------------
  // Test Suite 5: Email Preview Screen & FR/EN Switch
  // ----------------------------------------------------
  console.log("\n=== Test Suite 5: Email Preview Screen & FR/EN Switch ===");
  // Reset rcActiveJobId by re-selecting web product tab to test default selection
  await pageInit.click('button[data-rc-product="web"]');
  await pageInit.waitForTimeout(100);
  await pageInit.click('button[data-rc-screen="email"]');
  await pageInit.waitForSelector("#rc-screen-email", { state: "visible" });

  // Assert job selector exists and defaults to needs-approval job f-web-1155
  const defaultSelectedJob = await pageInit.locator("#rcJobSelector").inputValue();
  assert(defaultSelectedJob === "f-web-1155", `Job selector defaults to needs-approval job (found: ${defaultSelectedJob})`);

  // Assert top bar shows active job id, kind, and status
  const emailTopBar = await pageInit.locator(".rc-job-header-bar").textContent();
  assert(emailTopBar.includes("f-web-1155"), "Email top bar shows f-web-1155");
  assert(emailTopBar.includes("Feature Comms"), "Email top bar shows Feature Comms");
  assert(emailTopBar.includes("Requires owner"), "Email top bar shows Requires owner status");

  // Assert audience from draft sentences in "To:" line (second meta row)
  const toMetaRow = await pageInit.locator(".rc-email-meta-row").nth(1).textContent();
  assert(toMetaRow.includes("creators"), `Email To: line shows draft audience: ${toMetaRow}`);

  // Assert per-sentence audience tag next to PR tag
  const sentenceTag = await pageInit.locator(".rc-audience-tag").first().textContent();
  assert(sentenceTag.includes("creators"), `Sentence displays own audience tag [creators]: ${sentenceTag}`);
  const prTag = await pageInit.locator(".rc-email-sentences .rc-pr-tag").first().textContent();
  assert(prTag.includes("#1155"), `Sentence displays PR tag #1155: ${prTag}`);

  // Assert safe PR link
  const prLinkEl = pageInit.locator(".rc-email-sentences a.rc-pr-tag").first();
  const prHref = await prLinkEl.getAttribute("href");
  assert(prHref && prHref.startsWith("https://"), `PR link is safe https link: ${prHref}`);

  // Assert no count on a non-sent email preview
  const emailContentInitial = await pageInit.locator("#rcEmailCanvas").textContent();
  assert(
    emailContentInitial.includes("Recipient count is read from the product at send time."),
    "non-sent email preview shows read-at-send notice"
  );
  assert(!emailContentInitial.includes("1420 recipients"), "no count on a non-sent email preview");

  // Assert invented From line is omitted
  const metaRows = await pageInit.locator(".rc-email-meta-row").allTextContents();
  assert(!metaRows.some(r => r.startsWith("From:")), "invented From line is omitted");

  // Assert new unsubscribe footer
  assert(
    emailContentInitial.includes("The unsubscribe link is added by the product's sender."),
    "footer has new unsubscribe notice"
  );
  assert(
    !emailContentInitial.includes("Every claim backed by production PRs"),
    "old policy footer text is replaced"
  );

  // Default lang is EN or FR (for Web primary is FR, but rcLang initial is en)
  // Let's test explicit switch to EN
  await pageInit.click('button[data-rc-lang="en"]');
  await pageInit.waitForTimeout(100);
  const emailEn = await pageInit.locator("#rcEmailCanvas").textContent();
  assert(emailEn.includes("One-tap video posting"), "Email preview shows English text when EN is active");

  // Switch to FR
  await pageInit.click('button[data-rc-lang="fr"]');
  await pageInit.waitForTimeout(100);
  const emailFr = await pageInit.locator("#rcEmailCanvas").textContent();
  assert(emailFr.includes("Publication vidéo en un clic"), "Email preview shows French text when FR is active");

  // Test phone viewport toggle
  await pageInit.click('button[data-rc-viewport="phone"]');
  await pageInit.waitForTimeout(100);
  const isPhoneView = await pageInit.locator("#rcEmailCanvas.phone-view").isVisible();
  assert(isPhoneView, "Email canvas switches to phone-view class");

  await pageInit.click('button[data-rc-viewport="desktop"]');
  await pageInit.waitForTimeout(100);
  const isPhoneViewGone = await pageInit.locator("#rcEmailCanvas.phone-view").isVisible();
  assert(!isPhoneViewGone, "Email canvas switches back to desktop");

  // Verify selector and header stay in sync after clicking deferred job
  console.log("Testing selector and header sync with deferred job...");
  await pageInit.click('button[data-rc-screen="home"]');
  await pageInit.waitForSelector("#rc-screen-home", { state: "visible" });
  await pageInit.click('.rc-timeline-item.deferred button[data-rc-goto-screen="email"]');
  await pageInit.waitForSelector("#rc-screen-email", { state: "visible" });

  let curHeader = await pageInit.locator(".rc-job-header-bar").textContent();
  let curSelectVal = await pageInit.locator("#rcJobSelector").inputValue();
  assert(curHeader.includes("f-web-1180"), "Header displays deferred job f-web-1180");
  assert(curSelectVal === "f-web-1180", "Job selector is in sync with header on f-web-1180");

  // Select another job via selectOption
  await pageInit.locator("#rcJobSelector").selectOption("f-web-1155");
  await pageInit.waitForTimeout(100);
  curHeader = await pageInit.locator(".rc-job-header-bar").textContent();
  curSelectVal = await pageInit.locator("#rcJobSelector").inputValue();
  assert(curHeader.includes("f-web-1155"), "Header updated to f-web-1155 after selectOption");
  assert(curSelectVal === "f-web-1155", "Job selector remains in sync with header on f-web-1155");

  // ----------------------------------------------------
  // Test Suite 6: Video Storyboard Screen
  // ----------------------------------------------------
  console.log("\n=== Test Suite 6: Video Storyboard Screen ===");
  await pageInit.click('button[data-rc-screen="video"]');
  await pageInit.waitForSelector("#rc-screen-video", { state: "visible" });

  // Top bar on video screen shows active job id
  const videoTopBar = await pageInit.locator(".rc-job-header-bar").textContent();
  assert(videoTopBar.includes("f-web-1155"), "Video top bar shows f-web-1155");

  // Verify removed invented claims on video screen
  const videoScreenText = await pageInit.locator("#rc-screen-video").textContent();
  assert(!videoScreenText.includes("Lo-Fi"), "Invented 'Lo-Fi Instrumental' audio chip removed");
  assert(!videoScreenText.includes("0:00 -"), "Invented frame timer/durations removed");
  assert(!videoScreenText.includes("9:16 HD"), "Invented '9:16 HD' format tag removed");
  assert(videoScreenText.includes("Frame 1 of 3"), "Video screen shows frame index 'Frame 1 of 3'");

  // Verify safe video URL link
  const videoLink = pageInit.locator(".rc-phone-mockup a").first();
  const videoHref = await videoLink.getAttribute("href");
  assert(videoHref === "https://assets.example.com/videos/1155.mp4", `Video URL safely linked: ${videoHref}`);

  // Check storyboard frame cards exist
  const frameCards = await pageInit.locator(".rc-frame-card").all();
  assert(frameCards.length === 3, `Video storyboard has 3 frames (found ${frameCards.length})`);

  // Frame 1 active initially
  const initialFrameText = await pageInit.locator(".rc-video-frame-text").textContent();
  assert(initialFrameText.includes("Un seul clic pour partager") || initialFrameText.includes("One tap to share"), "Frame 1 text matches fixture");

  // Click frame 2
  await pageInit.click('.rc-frame-card[data-rc-frame-idx="1"]');
  await pageInit.waitForTimeout(100);
  const secondFrameText = await pageInit.locator(".rc-video-frame-text").textContent();
  assert(
    secondFrameText.includes("Votre vidéo en direct") || secondFrameText.includes("Your video live"),
    "Clicking frame 2 updates active frame text"
  );

  // Check caption textarea
  const captionVal = await pageInit.locator("#rcSocialCaptionText").inputValue();
  assert(captionVal.includes("#1155"), "Social media caption includes cited PR #1155");

  // Test "No draft yet" on Video for job with no draft (f-web-1180)
  console.log("Testing Video screen for job without draft...");
  await pageInit.locator("#rcJobSelector").selectOption("f-web-1180");
  await pageInit.waitForTimeout(100);
  const noDraftNotice = await pageInit.locator("#rcNoVideo").textContent();
  assert(noDraftNotice.includes("No draft yet"), "Video screen shows 'No draft yet' for job without draft");

  // Test frame index resets when switching product or job
  console.log("Testing frame index resets...");
  await pageInit.locator("#rcJobSelector").selectOption("f-web-1155");
  await pageInit.waitForTimeout(100);
  // Advance to frame 2
  await pageInit.click('.rc-frame-card[data-rc-frame-idx="1"]');
  await pageInit.waitForTimeout(100);
  let frameTextCheck = await pageInit.locator(".rc-video-frame-display").textContent();
  assert(frameTextCheck.includes("Frame 2 of 3"), "Frame 2 selected before reset");

  // Change job in selector to f-web-1001, then back to f-web-1155
  await pageInit.locator("#rcJobSelector").selectOption("f-web-1001");
  await pageInit.waitForTimeout(100);
  await pageInit.locator("#rcJobSelector").selectOption("f-web-1155");
  await pageInit.waitForTimeout(100);
  frameTextCheck = await pageInit.locator(".rc-video-frame-display").textContent();
  assert(frameTextCheck.includes("Frame 1 of 3"), "The frame index resets to 0 on job change");

  // Advance to frame 2 and change product
  await pageInit.click('.rc-frame-card[data-rc-frame-idx="1"]');
  await pageInit.waitForTimeout(100);
  await pageInit.click('button[data-rc-product="store"]');
  await pageInit.waitForTimeout(100);
  await pageInit.click('button[data-rc-product="web"]');
  await pageInit.waitForTimeout(100);
  await pageInit.click('button[data-rc-screen="video"]');
  await pageInit.waitForTimeout(100);
  frameTextCheck = await pageInit.locator(".rc-video-frame-display").textContent();
  assert(frameTextCheck.includes("Frame 1 of 3"), "The frame index resets to 0 on product change");

  // ----------------------------------------------------
  // Test Suite 7: Approval Screen (Decision Guardrail)
  // ----------------------------------------------------
  console.log("\n=== Test Suite 7: Approval Screen ===");
  await pageInit.click('button[data-rc-screen="approval"]');
  await pageInit.waitForSelector("#rc-screen-approval", { state: "visible" });

  const approvalTopBar = await pageInit.locator(".rc-job-header-bar").textContent();
  assert(approvalTopBar.includes("f-web-1155"), "Approval top bar shows f-web-1155");

  const approvalText = await pageInit.locator("#rc-screen-approval").textContent();
  assert(approvalText.includes("Approval Guardrail"), "Approval screen shows guardrail card");
  assert(approvalText.includes("#1155"), "Approval card shows cited PR #1155");
  assert(approvalText.includes("creators"), "Approval card shows draft audience (creators)");
  assert(approvalText.includes("Inbox item"), "Inbox item identifier is rendered as plain text");

  // Verify 'Open Inbox ->' button text and href
  const inboxBtn = pageInit.locator('#rc-screen-approval a.rc-btn-primary');
  const inboxBtnText = await inboxBtn.textContent();
  assert(inboxBtnText.trim() === "Open Inbox →", `Button text is exactly 'Open Inbox →' (found: ${inboxBtnText.trim()})`);
  const inboxBtnHref = await inboxBtn.getAttribute("href");
  assert(inboxBtnHref === "#/all/home", `Open Inbox button links to '#/all/home' (found: ${inboxBtnHref})`);

  // Verify preview buttons in approval card carry data-rc-target-job
  const emailPreviewFromApproval = pageInit.locator('#rc-screen-approval button[data-rc-goto-screen="email"]');
  const targetJobAttr = await emailPreviewFromApproval.getAttribute("data-rc-target-job");
  assert(targetJobAttr === "f-web-1155", `Approval preview link carries data-rc-target-job: ${targetJobAttr}`);

  // Test "View in Changelog" on approval card opens the release that holds the job's first PR
  console.log("Testing View in Changelog from approval card...");
  await pageInit.click('#rc-screen-approval button[data-rc-goto-screen="changelog"]');
  await pageInit.waitForSelector("#rc-screen-changelog", { state: "visible" });
  const clReleaseBox = await pageInit.locator("#rc-screen-changelog .rc-changelog-release-box").first().textContent();
  assert(clReleaseBox.includes("33dfe87") && clReleaseBox.includes("2026-11-06"), `Approval View in Changelog opens the right release (found: ${clReleaseBox.trim()})`);
  const clContent = await pageInit.locator("#rcChangelogContainer").textContent();
  assert(clContent.includes("#1155"), "Opened changelog contains PR #1155");

  // Switch back to approval screen for remaining assertions
  await pageInit.click('button[data-rc-screen="approval"]');
  await pageInit.waitForSelector("#rc-screen-approval", { state: "visible" });

  // Crucial check: verify NO approval buttons (Send now, Edit first, Skip) are present on this dashboard tab
  const btnTexts = await pageInit.locator("#rc-screen-approval button").allTextContents();
  const hasApproveBtn = btnTexts.some(t => t.toLowerCase() === "send now" || t.toLowerCase() === "approve" || t.toLowerCase() === "skip");
  assert(!hasApproveBtn, "Dashboard tab does NOT have state-changing approval buttons (handled only in Inbox)");

  // ----------------------------------------------------
  // Test Suite 8: Rules Screen
  // ----------------------------------------------------
  console.log("\n=== Test Suite 8: Rules Screen ===");
  await pageInit.click('button[data-rc-screen="rules"]');
  await pageInit.waitForSelector("#rc-screen-rules", { state: "visible" });

  const ruleBoxes = await pageInit.locator(".rc-rule-box").all();
  assert(ruleBoxes.length === 8, `Rules screen renders all 8 operating rules (found ${ruleBoxes.length})`);

  // ----------------------------------------------------
  // Test Suite 9: Outside Button Styles Integrity
  // ----------------------------------------------------
  console.log("\n=== Test Suite 9: Outside Button Styles Integrity ===");
  // Verify that an outside button, like #officeBtn or [data-autotab="board"], hasn't been corrupted by .rc styles
  const outsideBtn = pageInit.locator('button[data-autotab="board"]');
  const outsideBg = await outsideBtn.evaluate(el => window.getComputedStyle(el).backgroundColor);
  const outsideColor = await outsideBtn.evaluate(el => window.getComputedStyle(el).color);
  assert(outsideBg !== undefined && outsideColor !== undefined, `Outside button styles remain accessible (bg: ${outsideBg}, color: ${outsideColor})`);

  // Check that .rc-btn-primary is scoped to .rc and not leaked to global buttons
  const isGlobalBtnModified = await pageInit.evaluate(() => {
    const btn = document.querySelector('button[data-autotab="board"]');
    return btn.classList.contains("rc-btn-primary");
  });
  assert(!isGlobalBtnModified, "Global button does not have .rc styles applied");

  // ----------------------------------------------------
  // Test Suite 10: Mobile Viewport 390px (No Horizontal Scroll)
  // ----------------------------------------------------
  console.log("\n=== Test Suite 10: Mobile 390px Viewport Responsiveness ===");
  const { page: pageMobile } = await newPage({ width: 390, height: 844 });
  await pageMobile.goto(authUrl + "#/all/automation", { waitUntil: "networkidle" });
  await pageMobile.click('button[data-autotab="release-comms"]');
  await pageMobile.waitForSelector(".rc", { state: "visible" });

  const screens = ["home", "changelog", "email", "video", "approval", "rules"];
  for (const s of screens) {
    await pageMobile.click(`button[data-rc-screen="${s}"]`);
    await pageMobile.waitForTimeout(80);
    const scrollInfo = await pageMobile.evaluate(() => {
      const el = document.querySelector(".rc");
      return {
        clientWidth: el ? el.clientWidth : 0,
        scrollWidth: el ? el.scrollWidth : 0,
        bodyScrollWidth: document.body.scrollWidth,
        bodyClientWidth: document.body.clientWidth,
      };
    });
    // Allow at most 1-2px tolerance for subpixel antialiasing/scrollbar
    const fits = scrollInfo.scrollWidth <= scrollInfo.clientWidth + 2 && scrollInfo.bodyScrollWidth <= scrollInfo.bodyClientWidth + 2;
    assert(fits, `Screen '${s}' fits within 390px mobile viewport without horizontal scroll (client: ${scrollInfo.clientWidth}, scroll: ${scrollInfo.scrollWidth})`);
  }

  // Verify that scrolling at 390px keeps timeline items visible (no blank band)
  await pageMobile.click('button[data-rc-screen="home"]');
  await pageMobile.waitForSelector("#rcHomeTimeline", { state: "visible" });
  for (const scrollY of [0, 400, 800, 1200, 1600]) {
    await pageMobile.evaluate(y => window.scrollTo(0, y), scrollY);
    await pageMobile.waitForTimeout(60);
    const visibleCount = await pageMobile.evaluate(() => {
      const items = Array.from(document.querySelectorAll("#rcHomeTimeline .rc-timeline-item"));
      return items.filter(el => {
        const r = el.getBoundingClientRect();
        return r.top < window.innerHeight && r.bottom > 0;
      }).length;
    });
    assert(visibleCount > 0, `no blank band: timeline items remain visible while scrolling at 390px (scrollY: ${scrollY}, visible: ${visibleCount})`);
  }

  // Take screenshot for visual evidence
  const finalScreenshot = path.join(screenshotDir, "release-comms-view-final.png");
  await pageMobile.screenshot({ path: finalScreenshot, fullPage: true });
  console.log(`Saved screenshot to ${finalScreenshot}`);

} catch (err) {
  console.error("Test execution failed with error:", err);
  problems.push(err.message);
} finally {
  await browser.close();
  cleanup();
}

console.log("\n=================================");
if (problems.length > 0) {
  console.error(`FAILED with ${problems.length} problems:`);
  for (const p of problems) console.error(` - ${p}`);
  process.exit(1);
} else {
  console.log("ALL TESTS PASSED!");
  process.exit(0);
}
