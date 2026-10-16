/**
 * Just the failing case: pick a Goa hotel and measure the reply.
 *   node tools/zone_pick_only.mjs
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const URL = "https://dev-reviews.kepsla.com:18080/zone/bot.html";
const CITY = process.env.CITY || "Goa";
const SHOTS = path.join(process.cwd(), "tools", "_verify9");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  headless: false, slowMo: 80,
  args: ["--window-size=1500,980", "--window-position=0,0", "--ignore-certificate-errors"],
});
const ctx = await browser.newContext({ viewport: null, ignoreHTTPSErrors: true });
const page = await ctx.newPage();
await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(4000);
await page.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => !(x.textContent || "").trim());
  if (b) b.click();
}).catch(() => {});
await sleep(3500);

let f;
for (let i = 0; i < 60; i++) {
  f = page.frames().find((x) => /bot\.kepsla\.ai\/chat/.test(x.url()));
  if (f && await f.evaluate(() => document.body?.innerText.length > 0).catch(() => false)) break;
  await sleep(1000);
}
const bubbles = () => f.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => f.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 190000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(1800); return; } await sleep(800); }
}

const fill = async (s, v) => { const e = await f.$(s); if (e) await e.fill(v); };
await fill('input[placeholder="Your full name"]', "Sana Shaaista");
await fill('input[placeholder="Phone number"]', "9845061230");
await fill('input[type="email"]', "sana@example.com");
for (let i = 0; i < 40; i++) {
  const ok = await f.evaluate((c) => {
    const s = [...document.querySelectorAll("select")].pop(); if (!s) return false;
    const o = [...s.options].find((x) => new RegExp(c, "i").test(x.textContent || ""));
    if (!o) return false;
    s.value = o.value; s.dispatchEvent(new Event("change", { bubbles: true })); return true;
  }, CITY).catch(() => false);
  if (ok) break; await sleep(1500);
}
await f.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => /continue/i.test(x.textContent || ""));
  if (b) b.click();
});
await wait(); await sleep(2500);

const choices = await f.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
console.log("choice buttons:", choices);
if (!choices.length) { console.log("no choices — aborting"); await browser.close(); process.exit(1); }

await f.evaluate((l) => {
  const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
  if (b) b.click();
}, choices[0]);
await wait(); await sleep(2500);

const reply = (await bubbles()).pop() || "";
await page.screenshot({ path: path.join(SHOTS, "pick_after_fix.png") }).catch(() => {});
const lines = reply.split("\n").filter((l) => l.trim()).length;
console.log(`\n=== REPLY AFTER PICKING "${choices[0]}" ===`);
console.log(`length: ${reply.length} chars   (was 1723 before the prompt fix)`);
console.log(`mentions dining section : ${/dining|cafe c|co-bar|restaurant/i.test(reply)}`);
console.log(`mentions amenities/pool : ${/amenit|swimming pool|wi-?fi|parking|laundry/i.test(reply)}`);
console.log(`mentions offers         : ${/offer|% off|earlybird|monsoon/i.test(reply)}`);
console.log(`\n${reply.slice(0, 1400)}`);
await sleep(15000);
await browser.close();
