/**
 * The tail of the 50-turn sweep that never ran: in the long run an UNSUBMITTED
 * support form left the composer un-typeable, so turns 48-51 sent nothing.
 * This re-runs that segment properly — and treats "form is open" as something a
 * real guest resolves (submit it) rather than a dead end.
 *   node tools/zone_prebooking_tail.mjs
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(process.cwd(), "tools", "_prebooking50");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rows = [];
const save = () =>
  fs.writeFileSync(path.join(SHOTS, "tail_results.json"), JSON.stringify(rows, null, 1), "utf-8");

const browser = await chromium.launch({
  headless: false,
  slowMo: 60,
  args: ["--window-size=1500,980", "--window-position=0,0"],
});
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
const session = `pbtail_${Date.now()}`;
console.log(`session ${session}`);
await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`, {
  waitUntil: "domcontentloaded",
  timeout: 90000,
});
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () =>
  page
    .$$eval(".space-y-2 > div.justify-start", (e) => e.map((x) => (x.textContent || "").trim()))
    .catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
const submitBtn = () =>
  page
    .evaluate(() =>
      [...document.querySelectorAll("button")].some((b) =>
        /^(submit|send request|confirm)$/i.test((b.textContent || "").trim())
      )
    )
    .catch(() => false);

async function wait(ms = 210000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) {
    if ((await typing()) > 0) break;
    await sleep(500);
  }
  while (Date.now() < end) {
    if ((await typing()) === 0) {
      await sleep(2000);
      return true;
    }
    await sleep(800);
  }
  return false;
}

async function fillAndSubmitForm(issue) {
  await page.evaluate((txt) => {
    const setVal = (el, v) => {
      const proto =
        el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    };
    document
      .querySelectorAll('textarea:not([placeholder="Type a message..."])')
      .forEach((t) => setVal(t, txt));
    document.querySelectorAll("input").forEach((i) => {
      if ((i.value || "").trim()) return;
      if (i.type === "email") setVal(i, "sana@example.com");
      else if (i.type === "tel" || /phone/i.test(i.placeholder || "")) setVal(i, "9845061230");
      else if (i.type === "text" || !i.type) setVal(i, "Sana Shaaista");
    });
  }, issue);
  await sleep(800);
  await page.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) =>
      /^(submit|send request|confirm)$/i.test((x.textContent || "").trim())
    );
    if (b) b.click();
  });
  await wait();
  await sleep(2000);
}

async function ask(label, q) {
  // A guest facing an open form deals with it first; only then do they type.
  if (await submitBtn()) {
    console.log("   (form was open — submitting it first, like a guest would)");
    await fillAndSubmitForm("Please see my earlier message.");
  }
  const before = (await bubbles()).length;
  const t0 = Date.now();
  let blocked = false;
  const composer = page.locator('textarea[placeholder="Type a message..."]').first();
  try {
    await composer.fill(q, { timeout: 25000 });
    await composer.press("Enter");
    await wait();
  } catch {
    blocked = true;
  }
  const all = await bubbles();
  const answer = all.length > before ? all[all.length - 1] || "" : "";
  const row = {
    label,
    q,
    secs: +((Date.now() - t0) / 1000).toFixed(1),
    composer_blocked: blocked,
    answer,
  };
  rows.push(row);
  save();
  console.log(`\n[${label}] ${row.secs}s${blocked ? " COMPOSER_BLOCKED" : ""}`);
  console.log(`   Q: ${q}`);
  console.log(`   A: ${answer.replace(/\s+/g, " ").slice(0, 240)}`);
  return row;
}

// intro
const nameBox = page.locator('input[placeholder="Your full name"]').first();
await nameBox.waitFor({ state: "attached", timeout: 90000 }).catch(() => {});
await nameBox.fill("Sana Shaaista").catch(() => {});
await page.locator('input[placeholder="Phone number"]').first().fill("9845061230").catch(() => {});
await page.locator('input[type="email"]').first().fill("sana@example.com").catch(() => {});
const sel = page.locator("form select").last();
for (let i = 0; i < 40; i++) {
  const labels = await sel.locator("option").allTextContents().catch(() => []);
  const hit = labels.find((l) => /goa/i.test(l));
  if (hit) {
    await sel.selectOption({ label: hit });
    break;
  }
  await sleep(1500);
}
await page.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => /continue/i.test(x.textContent || ""));
  if (b) b.click();
});
await wait();
await sleep(2500);
const picks = await page.evaluate(() =>
  [...document.querySelectorAll("button")]
    .map((b) => (b.textContent || "").trim())
    .filter((t) => /zone/i.test(t) && t.length < 70)
);
if (picks.length) {
  await page.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, picks[0]);
  await wait();
  await sleep(2000);
}

// commit a real complaint ticket first (this arms the ticket-edit interceptor)
await ask("complaint", "the staff were rude to me, i want to raise a complaint");
await fillAndSubmitForm("The front desk staff were rude at check-in.");
console.log("   ↳ complaint form submitted (ticket committed)");

await ask("cancel", "actually cancel that complaint");
await ask("callback", "can someone from your team call me back?");
await ask("recall", "remind me which hotels we discussed");
await ask("closing", "thanks, that's all for now");

await page.screenshot({ path: path.join(SHOTS, "tail_end.png") }).catch(() => {});
console.log(`\n${"=".repeat(70)}`);
console.log(`blocked composer turns : ${rows.filter((r) => r.composer_blocked).length} / ${rows.length}`);
console.log(`empty replies          : ${rows.filter((r) => !r.answer).length} / ${rows.length}`);
console.log(`-> ${path.join(SHOTS, "tail_results.json")}`);
await browser.close();
