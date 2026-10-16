/**
 * Does the booking-assistance form EDIT the existing ticket, or create a second?
 *   node tools/zone_ticket_edit.mjs
 *
 * Flow: pick a hotel -> "I want to book it" (this silently raises a ticket)
 *       -> click "Get help from our team" -> fill dates/guests/message -> Submit.
 *
 * The requirement is that the submit UPDATES the ticket the booking intent
 * already created, rather than leaving two rows for one guest in the dashboard.
 * Ticket refs are read from the DB afterwards.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(process.cwd(), "tools", "_ticket_edit");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({ headless: false, slowMo: 90,
  args: ["--window-size=1500,980", "--window-position=0,0"] });
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
const session = `ticket_${Date.now()}`;
console.log(`session_id = ${session}\n`);

await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`,
  { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 220000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2500); return true; } await sleep(800); }
  return false;
}
async function say(q) {
  const c = page.locator('textarea[placeholder="Type a message..."]').first();
  await c.fill(q); await c.press("Enter"); await wait();
  const all = await bubbles();
  console.log(`> ${q}\n  ${(all[all.length - 1] || "").replace(/\s+/g, " ").slice(0, 130)}\n`);
}

// intro form
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
  }, choices[0]);
  await wait(); await sleep(2000);
}

// STEP 1 — booking intent, which raises the first ticket
await say("I want to book it");
await page.screenshot({ path: path.join(SHOTS, "01_booking.png") }).catch(() => {});

// STEP 2 — open the assistance form
const opened = await page.evaluate(() => {
  const b = [...document.querySelectorAll("button")]
    .find((x) => /help from our team|assistance/i.test(x.textContent || ""));
  if (!b) return false;
  b.click(); return true;
});
console.log(`assistance button clicked: ${opened}`);
await sleep(4000);
await page.screenshot({ path: path.join(SHOTS, "02_form.png") }).catch(() => {});

// STEP 3 — fill every field the form shows, then submit
const fields = await page.evaluate(() => [...document.querySelectorAll("input,textarea,select")]
  .map((e) => ({ tag: e.tagName, type: e.type || "", ph: e.placeholder || "", name: e.name || "" })));
console.log("form fields:", JSON.stringify(fields).slice(0, 300));

await page.evaluate(() => {
  const set = (el, v) => {
    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement : window.HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, v);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const dates = [...document.querySelectorAll('input[type="date"]')];
  if (dates[0]) set(dates[0], "2026-12-20");
  if (dates[1]) set(dates[1], "2026-12-23");
  for (const el of document.querySelectorAll('input[type="number"], input[placeholder*="2"]')) set(el, "2");
  for (const el of document.querySelectorAll("textarea")) {
    if (!/Type a message/i.test(el.placeholder || "")) set(el, "Please call me about the best rate for these dates.");
  }
});
await sleep(1200);
await page.screenshot({ path: path.join(SHOTS, "03_filled.png") }).catch(() => {});

const submitted = await page.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => /^submit$/i.test((x.textContent || "").trim()));
  if (!b) return false;
  b.click(); return true;
});
console.log(`submit clicked: ${submitted}`);
await wait(); await sleep(6000);
await page.screenshot({ path: path.join(SHOTS, "04_after_submit.png") }).catch(() => {});
console.log(`\nlast reply: ${((await bubbles()).pop() || "").replace(/\s+/g, " ").slice(0, 180)}`);
console.log(`\nSESSION=${session}`);
fs.writeFileSync(path.join(SHOTS, "session.txt"), session, "utf-8");
await sleep(6000);
await browser.close();
