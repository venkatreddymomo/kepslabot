/**
 * A real 30-turn guest conversation with the Zone bot, driven through the widget.
 *
 *   node tools/zone_conversation_test.mjs            # headed, watchable
 *   HEADLESS=1 node tools/zone_conversation_test.mjs
 *
 * Captures per-turn latency and a transcript, plus the two screenshots asked for:
 *   shot_new_session      - a brand-new visitor's opening screen
 *   shot_returning_convo  - a NEW conversation for a visitor whose lead is
 *                           already on file (should greet by name, no form)
 *
 * Everything lands in tools/_convo_shots/.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const HEADLESS = process.env.HEADLESS === "1";
const SHOTS = path.join(process.cwd(), "tools", "_convo_shots");
fs.mkdirSync(SHOTS, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const shot = (page, name) =>
  page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });

const panel = (page) =>
  page.locator("div.min-h-0.flex-1.overflow-y-auto").first()
    .evaluate((el) => el.textContent || "").catch(() => "");

async function clickText(page, selector, source) {
  return page.evaluate(
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

const botBubbles = (page) =>
  page.locator(".space-y-2 > div.justify-start").count();

async function waitForReply(page, timeoutMs = 200000) {
  const deadline = Date.now() + timeoutMs;
  for (let i = 0; i < 40 && Date.now() < deadline; i++) {
    if ((await page.locator(".animate-bounce").count()) > 0) break;
    await sleep(500);
  }
  while (Date.now() < deadline) {
    if ((await page.locator(".animate-bounce").count()) === 0) {
      await sleep(1500);
      return true;
    }
    await sleep(800);
  }
  return false;
}

// A plausible guest journey: browse, compare, narrow down, ask practical
// questions about the area, then move toward booking.
const TURNS = [
  "hi",
  "what hotels do you have in Kolkata?",
  "which one is better for a family holiday?",
  "tell me about the New Town one",
  "what rooms do you have?",
  "which room is the biggest?",
  "does it have a bathtub?",
  "what about the dining options?",
  "is there a bar?",
  "do you have a pool?",
  "is there a gym?",
  "what time is check-in?",
  "can I check in early?",
  "do you allow pets?",
  "is breakfast included?",
  "how far is the airport?",
  "any parks nearby?",
  "is there a vet nearby?",
  "any sweet shops nearby?",
  "where can I get my phone repaired?",
  "what are the nearby attractions?",
  "how do I get to Sector V from there?",
  "is it good for business travellers?",
  "what about the New Market property?",
  "which of the two is closer to the airport?",
  "ok I want to book the New Town one",
  "for 2 adults",
  "what offers do you have?",
  "can someone from the team call me?",
  "thanks, that's all",
];

(async () => {
  const browser = await chromium.launch({
    headless: HEADLESS,
    slowMo: HEADLESS ? 0 : 250,
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const session = `convo_${Date.now()}`;
  const url = `${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`;

  await page.goto(url, { waitUntil: "domcontentloaded" });
  await sleep(3000);
  await page.locator("button svg").last().click().catch(() => {});

  const form = page.locator('input[placeholder="Your full name"]').first();
  await form.waitFor({ state: "attached", timeout: 60000 }).catch(() => {});

  // ---- SCREENSHOT 1: brand-new session -----------------------------------
  await shot(page, "shot_new_session");
  console.log("saved shot_new_session.png");

  // Fill the intro form (waiting for the city list, which loads slowly).
  await form.fill("Sana Shaaista");
  await page.locator('input[placeholder="Phone number"]').first().fill("9845061230");
  await page.locator('input[type="email"]').first().fill("sana@example.com");
  const select = page.locator("form select").last();
  for (let i = 0; i < 40; i++) {
    const labels = await select.locator("option").allTextContents().catch(() => []);
    const hit = labels.find((l) => /kolkata/i.test(l));
    if (hit) { await select.selectOption({ label: hit }); break; }
    await sleep(1500);
  }
  await clickText(page, "button", "Continue");
  await waitForReply(page);
  await sleep(2000);

  // ---- the conversation --------------------------------------------------
  const rows = [];
  const composer = page.locator('textarea[placeholder="Type a message..."]').first();

  for (let i = 0; i < TURNS.length; i++) {
    const q = TURNS[i];
    try {
    const before = await botBubbles(page);
    const started = Date.now();
    await composer.fill(q);
    await composer.press("Enter");
    const replied = await waitForReply(page);
    const seconds = (Date.now() - started) / 1000;

    const bubbles = await page.locator(".space-y-2 > div.justify-start")
      .evaluateAll((els) => els.map((e) => (e.textContent || "").trim()));
    const answer = bubbles[bubbles.length - 1] || "";
    const grew = (await botBubbles(page)) > before;

    rows.push({ n: i + 1, q, seconds, ok: replied && grew, words: answer.split(/\s+/).length, answer });
    // Persist every turn: a browser crash at turn 24 previously destroyed the
    // whole transcript because it was only written at the end.
    fs.writeFileSync(path.join(SHOTS, "transcript.json"), JSON.stringify(rows, null, 1), "utf-8");
    console.log(`${String(i + 1).padStart(2)}. [${seconds.toFixed(1)}s] ${q}`);
    console.log(`    ${answer.replace(/\s+/g, " ").slice(0, 150)}`);
    if (i === 4) await shot(page, "shot_mid_rooms");
    if (i === 17) await shot(page, "shot_mid_nearby");
    } catch (err) {
      console.log(`${String(i + 1).padStart(2)}. CRASHED: ${String(err).slice(0, 80)}`);
      rows.push({ n: i + 1, q, seconds: 0, ok: false, words: 0, answer: "CRASH" });
      break;
    }
  }
  await shot(page, "shot_end_of_chat");

  // ---- SCREENSHOT 2: NEW conversation, lead already on file --------------
  // The dustbin rotates the conversation id. The visitor is known by now, so
  // this must greet by name and ask only for a location - never the form again.
  await clickText(page, "button", "Clear chat").catch(() => {});
  await page.locator('button[title="Clear chat"]').first().click().catch(() => {});
  await sleep(9000);
  await shot(page, "shot_returning_convo");
  const returningText = await panel(page);
  const greeted = /welcome back/i.test(returningText);
  const formBack = await page.locator('input[placeholder="Your full name"]').count();
  console.log(`\nreturning conversation -> greeted by name: ${greeted} | form shown again: ${formBack > 0}`);

  // ---- report ------------------------------------------------------------
  const times = rows.map((r) => r.seconds).sort((a, b) => a - b);
  const pct = (p) => times[Math.min(times.length - 1, Math.floor(times.length * p))];
  console.log(`\n${"=".repeat(70)}\nLATENCY over ${rows.length} turns`);
  console.log(`  median ${pct(0.5).toFixed(1)}s | p90 ${pct(0.9).toFixed(1)}s | min ${times[0].toFixed(1)}s | max ${times[times.length - 1].toFixed(1)}s`);
  console.log(`  turns over 25s: ${times.filter((t) => t > 25).length}`);
  console.log(`  failed turns  : ${rows.filter((r) => !r.ok).length}`);
  const slowest = [...rows].sort((a, b) => b.seconds - a.seconds).slice(0, 5);
  console.log("\n  slowest:");
  slowest.forEach((r) => console.log(`    ${r.seconds.toFixed(1)}s  ${r.q}`));

  fs.writeFileSync(
    path.join(SHOTS, "transcript.json"),
    JSON.stringify(rows, null, 1),
    "utf-8",
  );
  console.log(`\ntranscript + screenshots -> ${SHOTS}`);
  if (!HEADLESS) await sleep(5000);
  await browser.close();
})();
