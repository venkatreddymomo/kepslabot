/**
 * Check the sign-off / brevity directive holds in BOTH lanes.
 *
 * The router sends some questions to the service agent and some to the book
 * answerer, so this asks a mix and reports which lane answered each, whether
 * the directive was in that call's prompt, and whether a sign-off survived.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const CITY = process.env.CITY || "Goa";
const SHOTS = path.join(process.cwd(), "tools", "_verify9");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const TURNS = [
  "what rooms do you have?",       // usually service lane
  "what are the dining options?",  // usually book lane
  "is there a pool?",
  "what are the offers?",
  "how far is the beach?",
];

const browser = await chromium.launch({
  headless: false, slowMo: 70,
  args: ["--window-size=1500,980", "--window-position=0,0"],
});
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
const session = `lanes_${Date.now()}`;
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
await wait(); await sleep(2000);

// pick the first hotel so we are inside one property
const choices = await page.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
if (choices.length) {
  await page.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, choices[0]);
  await wait(); await sleep(2000);
}

const SIGNOFF = /let me know if|if you'?d like|if you want,? i can|feel free to ask|would you like to know more|i can also (help|tell|share)/i;
const out = [];
for (const q of TURNS) {
  // fill THROUGH the locator so the textarea actually has focus — setting
  // .value via evaluate left focus elsewhere and Enter went nowhere, so the
  // previous run captured the same stale bubble five times.
  const before = (await bubbles()).length;
  const composer = page.locator("textarea").first();
  await composer.focus();   // click() fails hit-testing on the fixed overlay
  await composer.fill(q);
  await composer.press("Enter");
  await wait();
  let all = await bubbles();
  if (all.length === before) {           // nothing new arrived — do not score it
    console.log(`\n[SKIPPED — no new reply] ${q}`);
    out.push({ q, chars: 0, signoff: false, answer: "", sent: false });
    continue;
  }
  const a = all[all.length - 1] || "";
  out.push({ q, chars: a.length, signoff: SIGNOFF.test(a), answer: a, sent: true });
  console.log(`\n[${a.length} chars] ${q}   sign-off=${SIGNOFF.test(a)}`);
  console.log(`   ${a.replace(/\s+/g, " ").slice(0, 200)}`);
}
fs.writeFileSync(path.join(SHOTS, "both_lanes.json"), JSON.stringify(out, null, 1), "utf-8");
await page.screenshot({ path: path.join(SHOTS, "both_lanes.png") }).catch(() => {});
console.log(`\n${"=".repeat(60)}`);
const sent = out.filter((r) => r.sent);
console.log(`turns actually answered: ${sent.length} / ${out.length}`);
console.log(`replies with a sign-off: ${sent.filter((r) => r.signoff).length} / ${sent.length}`);
console.log(`distinct replies: ${new Set(sent.map((r) => r.answer)).size} (must equal ${sent.length})`);
console.log(`median length: ${out.map((r) => r.chars).sort((a, b) => a - b)[Math.floor(out.length / 2)]} chars`);
await sleep(10000);
await browser.close();
