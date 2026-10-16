/**
 * Same hotel-pick case, against the LOCAL server so code changes are in play.
 *   node tools/zone_pick_local.mjs
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const CITY = process.env.CITY || "Goa";
const SHOTS = path.join(process.cwd(), "tools", "_verify9");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  headless: false, slowMo: 80,
  args: ["--window-size=1500,980", "--window-position=0,0"],
});
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
const session = `pick_${Date.now()}`;
await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`,
  { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 190000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(1800); return; } await sleep(800); }
}

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
await wait(); await sleep(2500);

const choices = await page.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
console.log("choice buttons:", choices);
if (!choices.length) { console.log("no choices"); await browser.close(); process.exit(1); }

await page.evaluate((l) => {
  const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
  if (b) b.click();
}, choices[0]);
await wait(); await sleep(2500);

const reply = (await bubbles()).pop() || "";
await page.screenshot({ path: path.join(SHOTS, "pick_local.png") }).catch(() => {});
console.log(`\n=== LOCAL REPLY AFTER PICKING "${choices[0]}" ===`);
console.log(`length: ${reply.length} chars`);
console.log(`  dining section : ${/dining|cafe c|co-bar|restaurant/i.test(reply)}`);
console.log(`  amenities/pool : ${/amenities|swimming pool|wi-?fi|parking|laundry/i.test(reply)}`);
console.log(`  offers section : ${/current offers|earlybird|monsoon/i.test(reply)}`);
console.log(`  sign-off       : ${/let me know if|if you'?d like|feel free to ask|would you like to know more/i.test(reply)}`);
console.log(`\n${reply.slice(0, 1200)}`);
await sleep(12000);
await browser.close();
