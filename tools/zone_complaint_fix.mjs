/**
 * Reproduces the two screenshots and checks the fix.
 *   node tools/zone_complaint_fix.mjs
 *
 * Session A — the exact reported failure:
 *   1. pick a Goa hotel, raise a REAL complaint, submit the form  -> ticket exists
 *      (that ticket is what arms the pre-router ticket-edit interceptor)
 *   2. "rise complaint on bangalore hotel"  -> must DECLINE (no Bangalore hotel)
 *   3. "rise complaint on patna hotel"      -> must OPEN THE FORM for Patna
 *   Neither may answer "I've noted your updated complaint" — that is the bug.
 *
 * Session B — a brand-level complaint with no hotel named must ASK which hotel.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(process.cwd(), "tools", "_complaint_fix");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The bug's fingerprint: the ticket-edit interceptor's "I noted your update" reply.
const EDIT_REPLY = /noted your (updated|request to update)|updated (complaint|request) for the team/i;

const out = { A: [], B: [] };
const save = () =>
  fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(out, null, 1), "utf-8");

const browser = await chromium.launch({
  headless: false,
  slowMo: 60,
  args: ["--window-size=1500,980", "--window-position=0,0"],
});

function wire(page) {
  const bubbles = () =>
    page
      .$$eval(".space-y-2 > div.justify-start", (e) => e.map((x) => (x.textContent || "").trim()))
      .catch(() => []);
  const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);

  async function wait(ms = 200000) {
    const end = Date.now() + ms;
    for (let i = 0; i < 40 && Date.now() < end; i++) {
      if ((await typing()) > 0) break;
      await sleep(500);
    }
    while (Date.now() < end) {
      if ((await typing()) === 0) {
        await sleep(2200);
        return true;
      }
      await sleep(800);
    }
    return false;
  }

  // A support form is on screen when its own message box / submit button is present.
  const formOpen = () =>
    page.evaluate(() => {
      const hasSubmit = [...document.querySelectorAll("button")].some((b) =>
        /^(submit|send request|confirm)$/i.test((b.textContent || "").trim())
      );
      const boxes = document.querySelectorAll(
        'textarea:not([placeholder="Type a message..."])'
      ).length;
      return hasSubmit || boxes > 0;
    }).catch(() => false);

  async function ask(q, bucket) {
    const before = (await bubbles()).length;
    const t0 = Date.now();
    const composer = page.locator('textarea[placeholder="Type a message..."]').first();
    await composer.fill(q);
    await composer.press("Enter");
    await wait();
    const all = await bubbles();
    const answer = all.length > before ? all[all.length - 1] || "" : "";
    const row = {
      q,
      secs: +((Date.now() - t0) / 1000).toFixed(1),
      form_opened: await formOpen(),
      looks_like_ticket_edit: EDIT_REPLY.test(answer),
      answer,
    };
    bucket.push(row);
    save();
    console.log(`\n[${row.secs}s | form=${row.form_opened} | editReply=${row.looks_like_ticket_edit}] ${q}`);
    console.log(`   ${answer.replace(/\s+/g, " ").slice(0, 260)}`);
    return row;
  }

  return { bubbles, wait, ask, formOpen };
}

async function intro(page, wait, city) {
  const name = page.locator('input[placeholder="Your full name"]').first();
  await name.waitFor({ state: "attached", timeout: 90000 }).catch(() => {});
  await name.fill("Sana Shaaista").catch(() => {});
  await page.locator('input[placeholder="Phone number"]').first().fill("9845061230").catch(() => {});
  await page.locator('input[type="email"]').first().fill("sana@example.com").catch(() => {});
  if (city) {
    const sel = page.locator("form select").last();
    for (let i = 0; i < 40; i++) {
      const labels = await sel.locator("option").allTextContents().catch(() => []);
      const hit = labels.find((l) => new RegExp(city, "i").test(l));
      if (hit) {
        await sel.selectOption({ label: hit });
        break;
      }
      await sleep(1500);
    }
  }
  await page.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) =>
      /continue/i.test(x.textContent || "")
    );
    if (b) b.click();
  });
  await wait();
  await sleep(2500);
}

// Fill whatever the support form asks for and submit it, so a REAL ticket exists.
async function submitSupportForm(page, wait) {
  await page.evaluate(() => {
    document
      .querySelectorAll('textarea:not([placeholder="Type a message..."])')
      .forEach((t) => {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype,
          "value"
        ).set;
        setter.call(t, "The front desk staff were rude to us at check-in.");
        t.dispatchEvent(new Event("input", { bubbles: true }));
      });
    document.querySelectorAll('form input[type="text"], form input:not([type])').forEach((i) => {
      if ((i.value || "").trim()) return;
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value"
      ).set;
      setter.call(i, "Sana Shaaista");
      i.dispatchEvent(new Event("input", { bubbles: true }));
    });
  });
  await sleep(600);
  await page.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) =>
      /^(submit|send request|confirm)$/i.test((x.textContent || "").trim())
    );
    if (b) b.click();
  });
  await wait();
  await sleep(2500);
}

// ══ SESSION A ═══════════════════════════════════════════════════════════════
{
  const ctx = await browser.newContext({ viewport: null });
  const page = await ctx.newPage();
  const { bubbles, wait, ask, formOpen } = wire(page);
  const session = `cfixA_${Date.now()}`;
  console.log(`\n########## SESSION A  (${session}) ##########`);
  await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`, {
    waitUntil: "domcontentloaded",
    timeout: 90000,
  });
  await sleep(3000);
  await page.locator("button svg").last().click().catch(() => {});
  await intro(page, wait, "Goa");

  const choices = await page.evaluate(() =>
    [...document.querySelectorAll("button")]
      .map((b) => (b.textContent || "").trim())
      .filter((t) => /zone/i.test(t) && t.length < 70)
  );
  console.log("hotel choices:", choices);
  if (choices.length) {
    await page.evaluate((l) => {
      const b = [...document.querySelectorAll("button")].find(
        (x) => (x.textContent || "").trim() === l
      );
      if (b) b.click();
    }, choices[0]);
    await wait();
    await sleep(2500);
  }

  // 1. a real complaint -> form -> submit -> ticket exists for this session
  await ask("the staff here were very rude to me, i want to raise a complaint", out.A);
  if (await formOpen()) {
    console.log("   -> support form open, submitting it to create the ticket");
    await submitSupportForm(page, wait);
    out.A.push({ q: "(submitted support form)", answer: (await bubbles()).pop() || "" });
    save();
  } else {
    console.log("   !! no form appeared — the ticket may not exist, later steps are weaker");
  }
  await page.screenshot({ path: path.join(SHOTS, "A1_after_ticket.png") }).catch(() => {});

  // 2. unsupported city  3. supported city
  await ask("rise complaint on bangalore hotel", out.A);
  await page.screenshot({ path: path.join(SHOTS, "A2_bangalore.png") }).catch(() => {});
  await ask("rise complaint on patna hotel", out.A);
  await page.screenshot({ path: path.join(SHOTS, "A3_patna.png") }).catch(() => {});
  await ctx.close();
}

// ══ SESSION B — brand-level complaint, no hotel named ═══════════════════════
{
  const ctx = await browser.newContext({ viewport: null });
  const page = await ctx.newPage();
  const { wait, ask } = wire(page);
  const session = `cfixB_${Date.now()}`;
  console.log(`\n########## SESSION B  (${session}) ##########`);
  await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`, {
    waitUntil: "domcontentloaded",
    timeout: 90000,
  });
  await sleep(3000);
  await page.locator("button svg").last().click().catch(() => {});
  // City is required by the intro form, but we deliberately do NOT pick one of
  // the hotel buttons — so the complaint arrives with 2 properties in scope and
  // none chosen, which is the "LLM has no clue which hotel" case.
  await intro(page, wait, "Goa");
  await page
    .locator('textarea[placeholder="Type a message..."]')
    .first()
    .waitFor({ state: "visible", timeout: 60000 })
    .catch(() => {});
  await ask("i want to raise a complaint about zone hotels", out.B);
  await page.screenshot({ path: path.join(SHOTS, "B1_group.png") }).catch(() => {});
  await ctx.close();
}

// ══ REPORT ══════════════════════════════════════════════════════════════════
const bang = out.A.find((r) => /bangalore/i.test(r.q || "")) || {};
const patna = out.A.find((r) => /patna/i.test(r.q || "")) || {};
const group = out.B[0] || {};
const declined =
  /don'?t have|not have|have no|no bangalore|no hotel|not in (our|scope)|don'?t see that property|don'?t operate/i.test(
    bang.answer || ""
  );
const asksHotel = /which (hotel|one|of our hotels)|which property/i.test(group.answer || "");

console.log(`\n${"=".repeat(74)}`);
console.log(`BANGALORE declined as unsupported     : ${declined}          (want true)`);
console.log(`BANGALORE answered as a ticket edit   : ${!!bang.looks_like_ticket_edit}          (want false)`);
console.log(`PATNA opened the support form         : ${!!patna.form_opened}          (want true)`);
console.log(`PATNA answered as a ticket edit       : ${!!patna.looks_like_ticket_edit}          (want false)`);
console.log(`GROUP complaint asked which hotel     : ${asksHotel}          (want true)`);
console.log(`\nshots + json -> ${SHOTS}`);

await sleep(4000);
await browser.close();
