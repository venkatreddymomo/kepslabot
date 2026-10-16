/**
 * Chat with the bot like a guest would, through the widget, and print the FULL
 * reply for each turn so the answers can be judged by reading them.
 *   node tools/zone_chat_real.mjs
 *
 * One continuous conversation, no fresh sessions between questions — that is
 * how a real guest uses it.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(process.cwd(), "tools", "_chat_real");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const TURNS = [
  "what rooms do you have?",
  "how far is the airport?",
  "how far is Calangute Beach?",
  "any hospitals nearby?",
  "where can I get medicines?",
  "which pharmacy is open right now?",
  "what is nearby for chilling?",
  "where can I party tonight?",
  "something fun to do with kids?",
];

const browser = await chromium.launch({ headless: false, slowMo: 60,
  args: ["--window-size=1500,980", "--window-position=0,0"] });
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
const session = `real_${Date.now()}`;
await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`,
  { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 230000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2300); return true; } await sleep(800); }
  return false;
}
// strip the carousel + button chrome so the printed text is what the guest reads
const clean = (t) => String(t || "")
  .replace(/Loading image\.\.\.[\s\S]*$/, "")
  .replace(/Prev[\s\S]*?\d+\s*\/\s*\d+\s*\|\s*\w+\s*Next/g, "")
  .replace(/(Book on website|Get help from our team|Show less|Read more)/g, "")
  .replace(/\d{1,2}:\d{2}\s*(AM|PM)\s*$/i, "")
  .trim();

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
  console.log(`\n[clicked] ${choices[0]}`);
  await page.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, choices[0]);
  await wait(); await sleep(2000);
  console.log(clean((await bubbles()).pop()) + "\n");
}

const out = [];
for (const q of TURNS) {
  const t0 = Date.now();
  const c = page.locator('textarea[placeholder="Type a message..."]').first();
  await c.fill(q); await c.press("Enter");
  await wait();
  const a = clean((await bubbles()).pop());
  const secs = ((Date.now() - t0) / 1000).toFixed(1);
  out.push({ q, secs, a });
  console.log(`${"=".repeat(96)}`);
  console.log(`GUEST: ${q}   [${secs}s]`);
  console.log(`BOT:   ${a}`);
  fs.writeFileSync(path.join(SHOTS, "transcript.json"), JSON.stringify(out, null, 1), "utf-8");
}
await page.screenshot({ path: path.join(SHOTS, "end.png") }).catch(() => {});
console.log(`\ntranscript -> ${path.join(SHOTS, "transcript.json")}`);
await sleep(8000);
await browser.close();
