/**
 * The two questions that produced a Hilton in California and a list of Treebo
 * hotels, plus the nearby asks that must still work.
 *   node tools/zone_competitor_test.mjs
 *
 * Full replies printed — the point is to read what a guest would read.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(process.cwd(), "tools", "_competitor");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const TURNS = [
  "hm which hotel is closer to a beach",
  "any hotel in coimbatore",
  "can you suggest some other hotels nearby",
  "any hospitals nearby?",
  "where can I get medicines?",
  "what is nearby for chilling?",
];

// a competitor slipping through is the failure this run exists to catch
const COMPETITOR = /hilton|treebo|ivy residency|oyo|marriott|taj |radisson|novotel|hyatt|resort\b|guest house|homestay/i;
const OURS = /zone /i;

const browser = await chromium.launch({ headless: false, slowMo: 60,
  args: ["--window-size=1500,980", "--window-position=0,0"] });
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
// WIDGET_KEY makes the deployment pin apply. The zone widget row pins
// gpt-4.1-mini for EVERY role, so without this the harness runs the book
// answerer on 5.4-mini and the run does not describe what guests get.
const WK = process.env.WIDGET_KEY ? `&embed=1&widget_key=${process.env.WIDGET_KEY}` : "";
await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=comp_${Date.now()}${WK}`,
  { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 240000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2300); return true; } await sleep(800); }
  return false;
}
const clean = (t) => String(t || "")
  .replace(/Loading image\.\.\.[\s\S]*$/, "")
  .replace(/Prev[\s\S]*?\d+\s*\/\s*\d+\s*\|\s*\w+\s*Next/g, "")
  .replace(/(Book on website|Get help from our team|Show less|Read more)/g, "")
  .replace(/\d{1,2}:\d{2}\s*(AM|PM)\s*$/i, "").trim();

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
  await page.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, choices[1] || choices[0]);          // Parra, as in the reported conversation
  await wait(); await sleep(2000);
}

const out = [];
for (const q of TURNS) {
  const t0 = Date.now();
  const c = page.locator('textarea[placeholder="Type a message..."]').first();
  await c.fill(q); await c.press("Enter");
  await wait();
  const a = clean((await bubbles()).pop());
  const secs = ((Date.now() - t0) / 1000).toFixed(1);
  // "Zone ..." is ours; a competitor name is the failure
  const leaked = COMPETITOR.test(a.replace(OURS, ""));
  const dumped = /^Here'?s what I found near the hotel/i.test(a);
  out.push({ q, secs, a, leaked, dumped });
  console.log("=".repeat(98));
  console.log(`GUEST: ${q}   [${secs}s]`);
  console.log(`BOT:   ${a}`);
  console.log(`       competitor named: ${leaked}   raw dump: ${dumped}`);
  fs.writeFileSync(path.join(SHOTS, "transcript.json"), JSON.stringify(out, null, 1), "utf-8");
}
await page.screenshot({ path: path.join(SHOTS, "end.png") }).catch(() => {});
console.log(`\ncompetitor leaks: ${out.filter((r) => r.leaked).length} / ${out.length}`);
console.log(`raw dumps:        ${out.filter((r) => r.dumped).length} / ${out.length}`);
await sleep(8000);
await browser.close();
