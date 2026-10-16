/**
 * Send a cancel on the session prepared by zone_cancel_after_restart.py --setup,
 * through the real widget rather than the HTTP endpoint, so the turn takes the
 * same path a guest's does.
 *
 *   node tools/zone_cancel_drive.mjs
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WK = process.env.WIDGET_KEY ? `&embed=1&widget_key=${process.env.WIDGET_KEY}` : "";
const CANCEL = process.env.CANCEL || "please cancel the airport pickup i asked for";

const state = JSON.parse(
  fs.readFileSync(path.join(process.cwd(), "tools", "_cancel_restart_state.json"), "utf-8"),
);
const SID = state.session_id;
console.log(`session_id = ${SID}`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const browser = await chromium.launch({ headless: false, slowMo: 40,
  args: ["--window-size=1400,940", "--window-position=0,0"] });
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${SID}${WK}`,
  { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 240000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2500); return true; } await sleep(800); }
  return false;
}

// intro form, if the widget shows one for this fresh browser session
const nameBox = page.locator('input[placeholder="Your full name"]').first();
if (await nameBox.isVisible({ timeout: 45000 }).catch(() => false)) {
  await nameBox.fill("Sana Shaaista").catch(() => {});
  await page.locator('input[placeholder="Phone number"]').first().fill("9845061230").catch(() => {});
  await page.locator('input[type="email"]').first().fill("sana@example.com").catch(() => {});
  const sel = page.locator("form select").last();
  for (let i = 0; i < 40; i++) {
    const labels = await sel.locator("option").allTextContents().catch(() => []);
    const hit = labels.find((l) => /coimbatore/i.test(l));
    if (hit) { await sel.selectOption({ label: hit }); break; }
    await sleep(1500);
  }
  await page.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => /continue/i.test(x.textContent || ""));
    if (b) b.click();
  });
  await wait(); await sleep(2000);
}

const composer = page.locator('textarea[placeholder="Type a message..."]').first();
await composer.waitFor({ state: "visible", timeout: 60000 });
await composer.fill(CANCEL);
await composer.press("Enter");
await wait();
await sleep(2500);

const reply = String((await bubbles()).pop() || "")
  .replace(/Loading image\.\.\.[\s\S]*$/, "")
  .replace(/(Book on website|Get help from our team|Show less|Read more)/g, "")
  .trim();
console.log("\nGUEST:", CANCEL);
console.log("BOT  :", reply.slice(0, 500));
await sleep(3000);
await browser.close();
