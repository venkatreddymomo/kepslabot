/**
 * 30-turn guest conversation across THREE different cities.
 *
 *   node tools/zone_multicity_test.mjs             # headed, watchable
 *   HEADLESS=1 node tools/zone_multicity_test.mjs
 *
 * Three cities rather than one, because every earlier test used Kolkata and a
 * single-city sample proves nothing about the rest of the portfolio:
 *   Goa       - two properties, so multi-property answers get exercised
 *   Jaipur    - single property, heritage city
 *   Mussoorie - single property, small hill town (thin web coverage)
 *
 * A fresh browser per city also avoids the crash that killed the previous run:
 * 28 turns of image carousels in one context exhausted the renderer.
 *
 * Screenshots + transcript land in tools/_convo_shots/.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const HEADLESS = process.env.HEADLESS === "1";
const SHOTS = path.join(process.cwd(), "tools", "_convo_shots");
fs.mkdirSync(SHOTS, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const shot = (page, n) => page.screenshot({ path: path.join(SHOTS, `${n}.png`) }).catch(() => {});
const panel = (page) =>
  page.locator("div.min-h-0.flex-1.overflow-y-auto").first()
    .evaluate((el) => el.textContent || "").catch(() => "");
const bots = (page) => page.locator(".space-y-2 > div.justify-start").count();

const clickText = (page, sel, src) =>
  page.evaluate(([s, r]) => {
    const hit = [...document.querySelectorAll(s)].find((e) => new RegExp(r, "i").test(e.textContent || ""));
    if (!hit) return false;
    hit.click();
    return true;
  }, [sel, src]).catch(() => false);

async function waitReply(page, timeoutMs = 200000) {
  const end = Date.now() + timeoutMs;
  for (let i = 0; i < 40 && Date.now() < end; i++) {
    if ((await page.locator(".animate-bounce").count().catch(() => 0)) > 0) break;
    await sleep(500);
  }
  while (Date.now() < end) {
    if ((await page.locator(".animate-bounce").count().catch(() => 0)) === 0) {
      await sleep(1500);
      return true;
    }
    await sleep(800);
  }
  return false;
}

const CITIES = [
  { city: "Goa", multi: true, turns: [
    "what hotels do you have in Goa?",
    "which one is closer to the beach?",
    "tell me about the Calangute one",
    "what rooms do you have?",
    "is there a pool?",
    "any parks or gardens nearby?",
    "where can I get medicines nearby?",
    "what are the nearby attractions?",
    "how far is the airport?",
    "I want to book it",
  ]},
  { city: "Jaipur", multi: false, turns: [
    "tell me about your Jaipur hotel",
    "what room types are there?",
    "does it have a rooftop pool?",
    "what about dining?",
    "how far is Hawa Mahal?",
    "any sweet shops nearby?",
    "is there a vet nearby?",
    "what time is check-in?",
    "do you allow pets?",
    "what offers do you have?",
  ]},
  { city: "Mussoorie", multi: false, turns: [
    "tell me about Mussoorie",
    "what rooms are available?",
    "is it pet friendly?",
    "what can I do around there?",
    "any cafes nearby?",
    "how far is Kempty Falls?",
    "is there a doctor nearby?",
    "what is the check-out time?",
    "can someone from the team call me?",
    "thanks",
  ]},
];

const rows = [];
const save = () => fs.writeFileSync(path.join(SHOTS, "multicity_transcript.json"), JSON.stringify(rows, null, 1), "utf-8");

async function runCity(browser, spec, index) {
  const ctx = await browser.newContext(HEADLESS ? { viewport: { width: 1440, height: 900 } } : { viewport: null });
  const page = await ctx.newPage();
  const session = `mc_${spec.city}_${Date.now()}`;
  await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`, { waitUntil: "domcontentloaded" });
  await sleep(3000);
  await page.locator("button svg").last().click().catch(() => {});

  const form = page.locator('input[placeholder="Your full name"]').first();
  await form.waitFor({ state: "attached", timeout: 60000 }).catch(() => {});
  if (index === 0) { await shot(page, "shot_new_session"); console.log("  saved shot_new_session.png"); }

  await form.fill("Sana Shaaista").catch(() => {});
  await page.locator('input[placeholder="Phone number"]').first().fill("9845061230").catch(() => {});
  await page.locator('input[type="email"]').first().fill("sana@example.com").catch(() => {});
  const sel = page.locator("form select").last();
  for (let i = 0; i < 40; i++) {
    const labels = await sel.locator("option").allTextContents().catch(() => []);
    const hit = labels.find((l) => new RegExp(spec.city, "i").test(l));
    if (hit) { await sel.selectOption({ label: hit }); break; }
    await sleep(1500);
  }
  await clickText(page, "button", "Continue");
  await waitReply(page);
  await sleep(2000);

  console.log(`\n=== ${spec.city.toUpperCase()} ${spec.multi ? "(2 hotels)" : "(1 hotel)"} ===`);
  for (const q of spec.turns) {
    try {
      const before = await bots(page);
      const t0 = Date.now();
      const composer = page.locator('textarea[placeholder="Type a message..."]').first();
      await composer.fill(q);
      await composer.press("Enter");
      const replied = await waitReply(page);
      const secs = (Date.now() - t0) / 1000;
      const all = await page.locator(".space-y-2 > div.justify-start")
        .evaluateAll((els) => els.map((e) => (e.textContent || "").trim()));
      const answer = all[all.length - 1] || "";
      rows.push({ city: spec.city, q, secs, ok: replied && (await bots(page)) > before, answer });
      save();
      console.log(`  [${secs.toFixed(1)}s] ${q}`);
      console.log(`     ${answer.replace(/\s+/g, " ").slice(0, 155)}`);
    } catch (err) {
      rows.push({ city: spec.city, q, secs: 0, ok: false, answer: `CRASH: ${err}`.slice(0, 90) });
      save();
      console.log(`  CRASHED on: ${q}`);
      break;
    }
  }
  await shot(page, `shot_${spec.city.toLowerCase()}_end`);

  // Returning visitor: rotate the conversation. Should greet by name, no form.
  if (index === 0) {
    await page.locator('button[title="Clear chat"]').first().click().catch(() => {});
    await sleep(9000);
    await shot(page, "shot_returning_convo");
    const txt = await panel(page);
    console.log(`\n  returning conversation -> "welcome back": ${/welcome back/i.test(txt)} | form re-shown: ${(await page.locator('input[placeholder="Your full name"]').count()) > 0}`);
  }
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch({
    headless: HEADLESS,
    slowMo: HEADLESS ? 0 : 200,
    // Headed: give the window real estate and let the page use all of it.
    // A fixed 1440x900 viewport inside a maximised window rendered the widget
    // clipped off the right edge.
    args: HEADLESS ? [] : ["--window-size=1600,1000", "--window-position=0,0"],
  });
  for (let i = 0; i < CITIES.length; i++) await runCity(browser, CITIES[i], i);
  await browser.close();

  const times = rows.filter((r) => r.secs > 0).map((r) => r.secs).sort((a, b) => a - b);
  const pct = (p) => times[Math.min(times.length - 1, Math.floor(times.length * p))];
  console.log(`\n${"=".repeat(72)}\nLATENCY over ${times.length} turns`);
  console.log(`  median ${pct(0.5).toFixed(1)}s | p90 ${pct(0.9).toFixed(1)}s | min ${times[0].toFixed(1)}s | max ${times[times.length - 1].toFixed(1)}s`);
  console.log(`  over 30s: ${times.filter((t) => t > 30).length} | failed turns: ${rows.filter((r) => !r.ok).length}`);
  for (const c of CITIES) {
    const t = rows.filter((r) => r.city === c.city && r.secs > 0).map((r) => r.secs);
    if (t.length) console.log(`  ${c.city.padEnd(10)} median ${t.sort((a, b) => a - b)[Math.floor(t.length / 2)].toFixed(1)}s over ${t.length} turns`);
  }
  console.log(`\ntranscript -> ${path.join(SHOTS, "multicity_transcript.json")}`);
})();
