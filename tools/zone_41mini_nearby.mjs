/**
 * The nearby-decline set, re-run on gpt-4.1-mini.
 *
 *   WIDGET_KEY=wk_kOKo8Vwt1rGBE7ZCSLhYaFeC node tools/zone_41mini_nearby.mjs
 *
 * WIDGET_KEY matters: the zone widget row pins gpt-4.1-mini for EVERY role, and
 * the pin beats the per-role .env models. Without it the harness answers on
 * 5.4-mini and the run describes something no guest ever sees.
 *
 * Same 10 questions and same 3-run shape as the 5.4-mini baseline (5/10, 7/10,
 * 6/10 perfect) so the numbers are comparable.
 *
 * PASS is positive evidence — the reply must carry a real distance. An earlier
 * detector keyed off decline PHRASES and reported 0/8 declines when three had
 * declined, because the bot worded it as "aren't something the hotel details I
 * have cover". Never score this on phrasing again.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WK = process.env.WIDGET_KEY ? `&embed=1&widget_key=${process.env.WIDGET_KEY}` : "";
const RUNS = Number(process.env.RUNS || 3);
const OUT = path.join(process.cwd(), "tools", "_41mini");
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const QUESTIONS = [
  "any hospitals nearby?",
  "where can I get medicines?",
  "what is nearby for chilling?",
  "any good place to watch the sunset?",
  "where can I go shopping?",
  "is there an ATM close by?",
  "how long would it take to walk to the beach?",
  "any place to party nearby?",
  "any nice cafes around?",
  "what's worth seeing around here?",
];

// a distance is the thing that proves it used the verified facts
const HAS_DISTANCE = /\b\d[\d.,]*\s*(km|kms|kilometres?|kilometers?)\b|\b\d[\d.,]*\s*m\b|\b\d+\s*(min|mins|minutes)\b/i;

const clean = (t) => String(t || "")
  .replace(/Loading image\.\.\.[\s\S]*$/, "")
  .replace(/Prev[\s\S]*?\d+\s*\/\s*\d+\s*\|\s*\w+\s*Next/g, "")
  .replace(/(Book on website|Get help from our team|Show less|Read more)/g, "")
  .replace(/\d{1,2}:\d{2}\s*(AM|PM)\s*$/i, "").trim();

const browser = await chromium.launch({
  headless: false, slowMo: 40,
  args: ["--window-size=1500,980", "--window-position=0,0"],
});

const all = [];
for (let run = 1; run <= RUNS; run++) {
  const ctx = await browser.newContext({ viewport: null });
  const page = await ctx.newPage();

  const modelsSeen = new Set();
  page.on("console", () => {});

  await page.goto(
    `${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=m41_${run}_${Date.now()}${WK}`,
    { waitUntil: "domcontentloaded", timeout: 90000 },
  );
  await sleep(3000);
  await page.locator("button svg").last().click().catch(() => {});

  const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
    (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
  const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
  async function wait(ms = 240000) {
    const end = Date.now() + ms;
    for (let i = 0; i < 40 && Date.now() < end; i++) {
      if ((await typing()) > 0) break;
      await sleep(500);
    }
    while (Date.now() < end) {
      if ((await typing()) === 0) { await sleep(2300); return true; }
      await sleep(800);
    }
    return false;
  }

  // intro form — must wait for it to actually render, a fill on a missing
  // field silently no-ops and every later turn then fails for the wrong reason
  const nameBox = page.locator('input[placeholder="Your full name"]').first();
  await nameBox.waitFor({ state: "visible", timeout: 90000 }).catch(() => {});
  await nameBox.fill("Sana Shaaista").catch(() => {});
  await page.locator('input[placeholder="Phone number"]').first().fill("9845061230").catch(() => {});
  await page.locator('input[type="email"]').first().fill("sana@example.com").catch(() => {});
  const sel = page.locator("form select").last();
  for (let i = 0; i < 40; i++) {
    const labels = await sel.locator("option").allTextContents().catch(() => []);
    const hit = labels.find((l) => /goa/i.test(l));
    if (hit) { await sel.selectOption({ label: hit }); break; }
    await sleep(1500);
  }
  await page.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => /continue/i.test(x.textContent || ""));
    if (b) b.click();
  });
  await wait(); await sleep(2000);

  const choices = await page.evaluate(() => [...document.querySelectorAll("button")]
    .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
  if (choices.length) {
    const pick = choices.find((c) => /calangute/i.test(c)) || choices[0];
    await page.evaluate((l) => {
      const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
      if (b) b.click();
    }, pick);
    await wait(); await sleep(2000);
    console.log(`\n########## RUN ${run}  (picked: ${pick}) ##########`);
  } else {
    console.log(`\n########## RUN ${run}  (no property list) ##########`);
  }

  const rows = [];
  for (const q of QUESTIONS) {
    const t0 = Date.now();
    const c = page.locator('textarea[placeholder="Type a message..."]').first();
    const ok = await c.isVisible().catch(() => false);
    if (!ok) { console.log(`SKIP (no composer): ${q}`); continue; }
    await c.fill(q); await c.press("Enter");
    await wait();
    const a = clean((await bubbles()).pop());
    const secs = ((Date.now() - t0) / 1000).toFixed(1);
    const pass = HAS_DISTANCE.test(a);
    rows.push({ run, q, secs, a, pass });
    all.push({ run, q, secs, a, pass });
    console.log("-".repeat(96));
    console.log(`[${pass ? "PASS" : "FAIL"}] ${q}   (${secs}s)`);
    console.log(a.slice(0, 420));
    fs.writeFileSync(path.join(OUT, "transcript.json"), JSON.stringify(all, null, 1), "utf-8");
  }
  const p = rows.filter((r) => r.pass).length;
  console.log(`\n>>> RUN ${run}: ${p}/${rows.length} answered with a real distance`);
  await ctx.close();
}

console.log(`\n${"=".repeat(96)}`);
const byQ = {};
for (const r of all) { (byQ[r.q] ||= []).push(r.pass); }
for (const [q, v] of Object.entries(byQ)) {
  console.log(`${v.filter(Boolean).length}/${v.length}  ${q}`);
}
const tot = all.filter((r) => r.pass).length;
console.log(`\nTOTAL: ${tot}/${all.length} answered with a real distance`);
console.log(`(5.4-mini baseline was 18/30)`);
await browser.close();
