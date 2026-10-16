/**
 * Playwright walkthrough of the Zone changes, at laptop AND phone width.
 *
 * Run with the backend on :8011 and admin_ui built:
 *   node tools/verify_zone_changes.mjs            # headed, slow, watchable
 *   HEADLESS=1 node tools/verify_zone_changes.mjs # CI-ish
 *
 * Screenshots land in tools/_verify_shots/.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const HOTEL = process.env.HOTEL || "ZONE";
const HEADLESS = process.env.HEADLESS === "1";
const SHOTS = path.join(process.cwd(), "tools", "_verify_shots");
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
function check(name, pass, detail = "") {
  results.push({ name, pass, detail });
  console.log(`  ${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// textContent (not innerText) — visibility-independent, which matters because
// the widget renders as a fixed overlay whose innerText comes back empty.
async function panel(page) {
  return await page
    .locator("div.min-h-0.flex-1.overflow-y-auto")
    .first()
    .evaluate((el) => el.textContent || "")
    .catch(() => "");
}

// Playwright's text engine can't see this overlay, so find controls by scanning
// textContent in the page and clicking via the DOM.
async function countByText(page, selector, source) {
  return await page.evaluate(
    ([sel, src]) =>
      Array.from(document.querySelectorAll(sel)).filter((e) =>
        new RegExp(src, "i").test(e.textContent || ""),
      ).length,
    [selector, source],
  );
}

async function clickByText(page, selector, source) {
  return await page.evaluate(
    ([sel, src]) => {
      const hit = Array.from(document.querySelectorAll(sel)).find((e) =>
        new RegExp(src, "i").test(e.textContent || ""),
      );
      if (!hit) return false;
      hit.click();
      return true;
    },
    [selector, source],
  );
}

async function shot(page, name) {
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });
}

// Count only ASSISTANT bubbles. Counting all rows returned as soon as the
// guest's own message rendered, so assertions ran while the bot was still
// typing and everything downstream looked missing.
async function botBubbles(page) {
  return await page.locator(".space-y-2 > div.justify-start").count();
}

async function waitForBotReply(page, before, timeoutMs = 180000) {
  // Bubble counts alone proved racy (the guest's own row, and late-rendering
  // rows from the previous turn, both tripped it). The reliable signal is the
  // typing indicator: wait for it to APPEAR, then for it to CLEAR.
  const deadline = Date.now() + timeoutMs;
  for (let i = 0; i < 30 && Date.now() < deadline; i++) {
    if ((await page.locator(".animate-bounce").count()) > 0) break;
    await sleep(700);
  }
  while (Date.now() < deadline) {
    if ((await page.locator(".animate-bounce").count()) === 0) {
      await sleep(2000);
      return (await botBubbles(page)) > before;
    }
    await sleep(1000);
  }
  return false;
}

async function run(device) {
  const isPhone = device === "phone";
  const browser = await chromium.launch({ headless: HEADLESS, slowMo: HEADLESS ? 0 : 350 });
  const context = await browser.newContext({
    viewport: isPhone ? { width: 390, height: 844 } : { width: 1440, height: 900 },
    hasTouch: isPhone,
    isMobile: isPhone,
  });
  const page = await context.newPage();
  const session = `verify_${device}_${Date.now()}`;
  const url = `${BASE}/chat?hotel_code=${HOTEL}&phase=pre_booking&session_id=${session}&device=${isPhone ? "mobile" : "desktop"}`;

  // Must be registered BEFORE navigating: Playwright only reports websockets
  // opened after the listener exists, and the widget connects on page load.
  const wsImages = [];
  page.on("websocket", (ws) =>
    ws.on("framereceived", (f) => {
      try {
        const d = JSON.parse(f.payload);
        if (d.type !== "response") return;
        const md = (d.data || {}).metadata || {};
        wsImages.push((md.images || []).map((i) => String(i.category || "")));
      } catch { /* non-JSON frame */ }
    }),
  );

  console.log(`\n=== ${device.toUpperCase()} ===`);
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await sleep(4000);

  // On /chat (non-embed) the widget starts collapsed behind a launcher bubble;
  // the real embed opens itself. Click it open so we test the same surface.
  const launcher = page.locator('button[aria-label*="chat" i], button[title*="chat" i]').last();
  if ((await launcher.count()) > 0) {
    await launcher.click().catch(() => {});
  } else {
    await page.locator("button svg").last().click().catch(() => {});
  }
  // Wait on the form itself rather than a fixed sleep: the property-manifest
  // call is slow on a cold server and fixed sleeps made this flaky.
  // Match on an ATTRIBUTE, not text: Playwright's text engine returns nothing
  // for this fixed-overlay subtree, so hasText/getByText never match here.
  const introForm = page.locator('input[placeholder="Your full name"]').first();
  await introForm.waitFor({ state: "attached", timeout: 60000 }).catch(() => {});

  // ---- Item 1: only the intro form, no separate welcome bubble -------------
  // Locator counts, not innerText/innerHTML scraping: the widget panel is a
  // fixed-position overlay and text scraping proved unreliable against it.
  const hasIntroForm = (await introForm.count()) > 0;
  const panelText = await panel(page);
  const hasOldGreeting = /choose the location you/i.test(panelText);
  check("item1: old welcome bubble gone", !hasOldGreeting);
  check("item1: intro form shown", hasIntroForm);
  await shot(page, `${device}_1_open`);

  if (!hasIntroForm) {
    await browser.close();
    return;
  }

  // ---- Fill the intro form, choosing Kolkata (a 2-hotel city) --------------
  await page.fill('input[placeholder="Your full name"]', "Test Guest");
  await page.fill('input[placeholder="Phone number"]', "9845061230");
  await page.fill('input[type="email"]', "test.guest@example.com");
  const citySelect = page.locator("form select").last();
  await citySelect.selectOption({ label: "Kolkata" }).catch(async () => {
    const opts = await citySelect.locator("option").allTextContents();
    const k = opts.find((o) => /kolkata/i.test(o));
    if (k) await citySelect.selectOption({ label: k });
  });
  await shot(page, `${device}_2_form_filled`);

  const before = await botBubbles(page);
  // "Continue" on the intro form — NOT the chat composer's send button, which
  // also matches form button[type=submit] and is disabled while the form is up.
  await clickByText(page, "button", "Continue");
  await waitForBotReply(page, before, 120000);
  // Assert on CONTENT, not bubble count: the intro form itself renders inside an
  // assistant-side wrapper, so the count is already 1 before the reply lands.
  const introArrived = await panel(page);
  check("intro: city pick produced a reply", /Kolkata/i.test(introArrived));
  await sleep(2500);
  await shot(page, `${device}_3_city_intro`);

  const introText = await panel(page);

  // ---- Items 2 + 3: shorter, and no repeated preference question ----------
  const asksPreference =
    /Are you planning a \*{0,2}business trip/i.test(introText) ||
    /happy to recommend the (property|perfect room)/i.test(introText);
  check("item3: no repeated 'what kind of trip' question", !asksPreference);

  const bubbles = await page.locator(".space-y-2 > div").evaluateAll((els) =>
    els.map((e) => e.textContent || ""),
  );
  const intro = bubbles.filter((b) => /Kolkata/i.test(b)).sort((a, b) => b.length - a.length)[0] || "";
  const words = intro.trim().split(/\s+/).length;
  check("item2: intro is compact (< 300 words)", words > 0 && words < 300, `${words} words`);

  // ---- Item 5: a choice button per hotel ----------------------------------
  const nCount = await countByText(page, "button", "New Town");
  const mCount = await countByText(page, "button", "New Market");
  check("item5: 'Best for' differentiators present", /Best for:/i.test(introText));
  check("item5: one choice button per hotel", nCount >= 1 && mCount >= 1, `newTown=${nCount} newMarket=${mCount}`);

  // ---- Item 6: images attached to the city intro --------------------------
  const imgCount = await page.locator(".space-y-2 img").count();
  check("item6: images on the city intro", imgCount > 0, `${imgCount} img visible in carousel`);

  // ---- Item 8: chips must not be hidden in a scroller on laptop -----------
  // Must be the CHIP row specifically: the composer container shares
  // `shrink-0 border-t`, and matching it made this check flap depending on
  // whether chips had loaded yet.
  const chipRow = page.locator("div.shrink-0.border-t:not(:has(textarea)):has(button)").first();
  await chipRow.waitFor({ state: "attached", timeout: 45000 }).catch(() => {});
  if ((await chipRow.count()) > 0) {
    const info = await chipRow.evaluate((el) => ({
      cls: el.className,
      scrollW: el.scrollWidth,
      clientW: el.clientWidth,
    }));
    const wraps = /flex-wrap/.test(info.cls);
    const overflows = info.scrollW > info.clientW + 4;
    if (isPhone) {
      check("item8: phone keeps the horizontal chip scroller", /overflow-x-auto/.test(info.cls));
    } else {
      check("item8: laptop wraps chips (no hidden overflow)", wraps && !overflows,
        `wrap=${wraps} scrollW=${info.scrollW} clientW=${info.clientW}`);
    }
  } else {
    check("item8: chip row rendered", false, "no chip row found");
  }

  // ---- Item 5 follow-through + item 9: booking CTAs -----------------------
  if (nCount >= 1) {
    const b2 = await botBubbles(page);
    await clickByText(page, "button", "New Town");
    await waitForBotReply(page, b2, 120000);
    await sleep(2000);
    await shot(page, `${device}_4_property_picked`);
  }

  const b3 = await botBubbles(page);
  // The harness page has its own textareas (extra-metadata JSON); target the
  // widget composer explicitly or the message never reaches the bot.
  const composer = page.locator('textarea[placeholder="Type a message..."]').first();
  await composer.fill("I want to book a room");
  await composer.press("Enter");
  await waitForBotReply(page, b3, 120000);
  await sleep(3000);
  await shot(page, `${device}_5_booking`);

  check("item9: 'Book on website' shown", (await countByText(page, "a", "Book on website")) > 0);
  const helpCount = await countByText(page, "button", "Get help from our team");
  check("item9: 'Get help from our team' shown", helpCount > 0);

  if (helpCount > 0) {
    await clickByText(page, "button", "Get help from our team");
    await sleep(1200);
    const formText = await panel(page);
    check("item9: assistance form opens", /Let our team help you book/i.test(formText));
    const dates = await page.locator('input[type="date"]').count();
    const num = await page.locator('input[type="number"]').count();
    check("item9: form has dates + guests + note",
      dates >= 2 && num >= 1, `dates=${dates} guests=${num}`);
    await shot(page, `${device}_6_assist_form`);

    // Submit it and confirm the ticket path acknowledges.
    const today = new Date();
    const d1 = new Date(today.getTime() + 7 * 864e5).toISOString().slice(0, 10);
    const d2 = new Date(today.getTime() + 9 * 864e5).toISOString().slice(0, 10);
    await page.locator('input[type="date"]').nth(0).fill(d1);
    await page.locator('input[type="date"]').nth(1).fill(d2);
    await page.locator('input[type="number"]').first().fill("2");
    await page.locator('textarea[placeholder*="Room preference"]').first()
      .fill("High floor if possible, arriving late.");
    const b4 = await botBubbles(page);
    await clickByText(page, "button", "Send to the team");
    const acked = await waitForBotReply(page, b4, 60000);
    const ackText = await panel(page);
    check("item9: submission acknowledged",
      acked && /passed your details to our team/i.test(ackText));
    await sleep(1500);
    await shot(page, `${device}_7_assist_submitted`);
  }

  // ---- Item 6b: topic images mid-conversation -----------------------------
  // Images must follow what the turn is ABOUT (the KB books the router opened),
  // and must NOT appear on a turn with nothing visual to show.
  const topicCases = [
    ["What rooms do you have?", "room"],
    ["Tell me about the dining options", "dining"],
    ["Do you have a spa or a pool?", "spa|pool|fitness"],
  ];
  for (const [question, expected] of topicCases) {
    const bBefore = await botBubbles(page);
    wsImages.length = 0;
    const composer2 = page.locator('textarea[placeholder="Type a message..."]').first();
    await composer2.fill(question);
    await composer2.press("Enter");
    await waitForBotReply(page, bBefore, 150000);
    await sleep(2500);
    const cats = wsImages.flat();
    check(
      `item6: "${question.slice(0, 24)}…" shows ${expected} images`,
      cats.length > 0 && new RegExp(`^(${expected})$`).test(cats[0]),
      `got [${cats.join(", ")}]`,
    );
  }
  await shot(page, `${device}_9_topic_images`);

  // A policy question has nothing visual — it must attach NO images.
  {
    const bBefore = await botBubbles(page);
    wsImages.length = 0;
    const composer3 = page.locator('textarea[placeholder="Type a message..."]').first();
    await composer3.fill("What is your cancellation policy?");
    await composer3.press("Enter");
    await waitForBotReply(page, bBefore, 150000);
    await sleep(2000);
    check("item6: policy question attaches NO images", wsImages.flat().length === 0,
      `got [${wsImages.flat().join(", ")}]`);
  }

  // ---- NEW SESSION: the reset path that used to leave the form stuck ------
  // Rotating the conversation must land back on a clean opener: intro form
  // only, no greeting bubble, no leftover messages.
  await clickByText(page, "button", "New Session");
  await sleep(3000);
  const introAfterReset = page.locator('input[placeholder="Your full name"]').first();
  await introAfterReset.waitFor({ state: "attached", timeout: 45000 }).catch(() => {});
  const freshFormShown = (await introAfterReset.count()) > 0;
  const freshPanel = await panel(page);
  const freshGreeting = /choose the location you/i.test(freshPanel);
  const leftoverChat = /Best for:/i.test(freshPanel);
  check("new session: intro form returns", freshFormShown);
  check("new session: still no welcome bubble", !freshGreeting);
  check("new session: previous conversation cleared", !leftoverChat);
  await shot(page, `${device}_8_new_session`);

  if (!HEADLESS) await sleep(4000);
  await browser.close();
}

(async () => {
  await run("laptop");
  await run("phone");

  const failed = results.filter((r) => !r.pass);
  console.log(`\n=========== ${results.length - failed.length}/${results.length} checks passed ===========`);
  if (failed.length) {
    console.log("failed:");
    for (const f of failed) console.log(`  - ${f.name}${f.detail ? " (" + f.detail + ")" : ""}`);
  }
  console.log(`screenshots: ${SHOTS}`);
  process.exit(failed.length ? 1 : 0);
})();
