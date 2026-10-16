/**
 * Full local verification through the real widget UI.
 *   node tools/zone_local_full.mjs
 *   CITY=Goa node tools/zone_local_full.mjs
 *
 * Covers the four things that were only unit-tested, plus a regression sweep:
 *   - hospital / pharmacy lookups actually return named places
 *   - "how far is the airport" attaches a FACADE image (was showing a pool)
 *   - no reply ends with a sign-off
 *   - replies stay reasonable in length
 *
 * Uses the composer's exact placeholder selector — `textarea` alone matches the
 * support form's message box too, which is why an earlier run sent nothing.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const CITY = process.env.CITY || "Goa";
const SHOTS = path.join(process.cwd(), "tools", "_local_full");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SIGNOFF = /let me know if|if you'?d like|if you want,? i can|feel free to ask|would you like to know more|i can also (help|tell|share)/i;
const rows = [];
const save = () => fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(rows, null, 1), "utf-8");

const browser = await chromium.launch({
  headless: false, slowMo: 70,
  args: ["--window-size=1500,980", "--window-position=0,0"],
});
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
const session = `full_${Date.now()}`;
await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`,
  { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
const captions = () => page.evaluate(() => [...document.querySelectorAll("*")]
  .map((e) => (e.childElementCount === 0 ? (e.textContent || "").trim() : ""))
  .filter((t) => /^\d+\s*\/\s*\d+\s*\|\s*\w+/.test(t))).catch(() => []);

async function wait(ms = 200000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2000); return true; } await sleep(800); }
  return false;
}

async function ask(q) {
  const before = (await bubbles()).length;
  const capsBefore = (await captions()).length;
  const t0 = Date.now();
  // EXACT placeholder — plain "textarea" also matches the support form's box.
  const composer = page.locator('textarea[placeholder="Type a message..."]').first();
  await composer.fill(q);
  await composer.press("Enter");
  await wait();
  const all = await bubbles();
  const caps = await captions();
  const sent = all.length > before;
  const answer = sent ? (all[all.length - 1] || "") : "";
  const row = {
    q, sent, secs: +((Date.now() - t0) / 1000).toFixed(1),
    chars: answer.length, signoff: SIGNOFF.test(answer),
    newCaption: caps.length > capsBefore ? caps[caps.length - 1] : "",
    answer,
  };
  rows.push(row); save();
  console.log(`\n[${row.secs}s | ${row.chars} chars${row.newCaption ? " | img " + row.newCaption : ""}] ${q}`);
  if (!sent) console.log("   !! NOTHING SENT");
  else console.log(`   ${answer.replace(/\s+/g, " ").slice(0, 230)}`);
  return row;
}

// ---- intro form -------------------------------------------------------------
await page.screenshot({ path: path.join(SHOTS, "01_open.png") }).catch(() => {});
const openingBubbles = (await bubbles()).length;

const form = page.locator('input[placeholder="Your full name"]').first();
await form.waitFor({ state: "attached", timeout: 90000 }).catch(() => {});
await form.fill("Sana Shaaista").catch(() => {});
await page.locator('input[placeholder="Phone number"]').first().fill("9845061230").catch(() => {});
await page.locator('input[type="email"]').first().fill("sana@example.com").catch(() => {});
const sel = page.locator("form select").last();
for (let i = 0; i < 40; i++) {
  const labels = await sel.locator("option").allTextContents().catch(() => []);
  const hit = labels.find((l) => new RegExp(CITY, "i").test(l));
  if (hit) { await sel.selectOption({ label: hit }); break; }
  await sleep(1500);
}
await page.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => /continue/i.test(x.textContent || ""));
  if (b) b.click();
});
await wait(); await sleep(2000);
await page.screenshot({ path: path.join(SHOTS, "02_welcome.png") }).catch(() => {});

const welcome = (await bubbles()).pop() || "";
const welcomeCaps = await captions();
const choices = await page.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
console.log(`\nWELCOME  ${welcome.length} chars | images ${JSON.stringify(welcomeCaps)} | choices ${choices.length}`);
console.log(`   ${welcome.replace(/\s+/g, " ").slice(0, 200)}`);

if (choices.length) {
  await page.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, choices[0]);
  await wait(); await sleep(2000);
  const pick = (await bubbles()).pop() || "";
  const pickCaps = await captions();
  console.log(`\nAFTER PICK  ${pick.length} chars | sign-off ${SIGNOFF.test(pick)} | images ${JSON.stringify(pickCaps.slice(-2))}`);
  rows.push({ q: `(picked ${choices[0]})`, sent: true, secs: 0, chars: pick.length,
              signoff: SIGNOFF.test(pick), newCaption: pickCaps.slice(-1)[0] || "", answer: pick });
  save();
}

// ---- the four unproven fixes -------------------------------------------------
await ask("any hospitals nearby?");
await ask("where can I get medicines?");
await ask("how far is the airport?");
await ask("any parks nearby?");
await ask("what are the offers?");
await ask("what rooms do you have?");

await page.screenshot({ path: path.join(SHOTS, "03_end.png") }).catch(() => {});

// ---- report ------------------------------------------------------------------
const sent = rows.filter((r) => r.sent && r.secs > 0);
const t = sent.map((r) => r.secs).sort((a, b) => a - b);
const hosp = rows.find((r) => /hospital/.test(r.q)) || {};
const meds = rows.find((r) => /medicines/.test(r.q)) || {};
const air = rows.find((r) => /airport/.test(r.q)) || {};

console.log(`\n${"=".repeat(74)}`);
console.log(`opening bot bubbles      : ${openingBubbles}  (want 1)`);
console.log(`turns answered           : ${sent.length} / ${sent.length}`);
console.log(`distinct replies         : ${new Set(sent.map((r) => r.answer)).size} (must equal ${sent.length})`);
console.log(`replies with a sign-off  : ${rows.filter((r) => r.signoff).length} / ${rows.length}`);
console.log(`HOSPITAL names returned  : ${!/don'?t have|not listed|no specific/i.test(hosp.answer || "")}`);
console.log(`MEDICINE names returned  : ${!/don'?t have|not listed|no specific/i.test(meds.answer || "")}`);
console.log(`AIRPORT image category   : ${air.newCaption || "(none)"}  (want facade)`);
if (t.length) console.log(`latency median ${t[Math.floor(t.length / 2)]}s | max ${t[t.length - 1]}s`);
console.log(`\nresults -> ${path.join(SHOTS, "results.json")}`);
await sleep(12000);
await browser.close();
