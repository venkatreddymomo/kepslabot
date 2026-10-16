/**
 * Verify the 9 reported issues against the DEPLOYED Zone bot.
 *   node tools/zone_verify9.mjs
 *
 * Goa, because items 4-6 only exist in a two-hotel city.
 * Prints PASS / FAIL per item with the evidence it used.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const URL = process.env.URL || "https://dev-reviews.kepsla.com:18080/zone/bot.html";
const CITY = process.env.CITY || "Goa";
const SHOTS = path.join(process.cwd(), "tools", "_verify9");
fs.mkdirSync(SHOTS, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const record = (n, title, pass, evidence) => {
  results.push({ n, title, pass, evidence });
  console.log(`\n${pass ? "PASS" : "FAIL"}  ${n}. ${title}`);
  console.log(`      ${String(evidence).replace(/\s+/g, " ").slice(0, 260)}`);
};

const browser = await chromium.launch({
  headless: false, slowMo: 90,
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

const frame = async () => {
  for (let i = 0; i < 60; i++) {
    const f = page.frames().find((x) => /bot\.kepsla\.ai\/chat/.test(x.url()));
    if (f && await f.evaluate(() => document.body?.innerText.length > 0).catch(() => false)) return f;
    await sleep(1000);
  }
  throw new Error("widget never loaded");
};
const f = await frame();
const shot = (n) => page.screenshot({ path: path.join(SHOTS, `${n}.png`) }).catch(() => {});

const bubbles = () => f.$$eval(".space-y-2 > div.justify-start",
  (els) => els.map((e) => (e.textContent || "").trim())).catch(() => []);
const typing = () => f.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 190000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(1800); return true; } await sleep(800); }
  return false;
}
async function ask(text) {
  const t0 = Date.now();
  await f.evaluate((t) => {
    const ta = document.querySelector("textarea");
    const set = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    set.call(ta, t); ta.dispatchEvent(new Event("input", { bubbles: true }));
  }, text);
  await f.press("textarea", "Enter");
  await wait();
  const all = await bubbles();
  return { secs: (Date.now() - t0) / 1000, answer: all[all.length - 1] || "" };
}

// ---------- ITEM 1: how many messages on open --------------------------------
await shot("item1_open");
const opening = await bubbles();
const hasForm = await f.$$eval("input", (els) =>
  els.some((i) => /full name/i.test(i.placeholder || ""))).catch(() => false);
record(1, "Window opens with two messages (should be form only for a NEW visitor)",
  opening.length <= 1,
  `bot bubbles on open = ${opening.length}; intro form present = ${hasForm}. ` +
  `first bubble: "${(opening[0] || "").replace(/\s+/g, " ").slice(0, 110)}"`);

// fill the form
const fill = async (sel, v) => { const e = await f.$(sel); if (e) await e.fill(v); };
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
await shot("item45_city_intro");

// ---------- ITEMS 4 + 5: two-hotel intro -------------------------------------
const introText = (await bubbles()).pop() || "";
const gallery = await f.evaluate(() => {
  const caps = [...document.querySelectorAll("*")]
    .map((e) => (e.childElementCount === 0 ? (e.textContent || "").trim() : ""))
    .filter((t) => /^\d+\s*\/\s*\d+\s*\|\s*\w+/.test(t));
  return caps;
}).catch(() => []);
record(4, "Two hotels: images show only one hotel and start with food",
  gallery.length > 0 && /facade|room/i.test(gallery[0] || ""),
  `image captions found: ${JSON.stringify(gallery)} (want first = facade or room)`);

const choices = await f.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim())
  .filter((t) => /zone/i.test(t) && t.length < 70)).catch(() => []);
record(5, "Two+ hotels: offer buttons to pick which hotel to continue with",
  choices.length >= 2, `hotel choice buttons = ${JSON.stringify(choices)}`);

// ---------- ITEM 6: pick a hotel -> rooms + images ---------------------------
let item6 = "no choice button to click";
if (choices.length) {
  await f.evaluate((label) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === label);
    if (b) b.click();
  }, choices[0]);
  await wait(); await sleep(2500);
  await shot("item6_after_pick");
  const after = (await bubbles()).pop() || "";
  const caps = await f.evaluate(() => [...document.querySelectorAll("*")]
    .map((e) => (e.childElementCount === 0 ? (e.textContent || "").trim() : ""))
    .filter((t) => /^\d+\s*\/\s*\d+\s*\|\s*\w+/.test(t))).catch(() => []);
  const named = /room|suite|deluxe|premium|connect/i.test(after);
  item6 = `reply mentions room types = ${named}; captions = ${JSON.stringify(caps.slice(-3))}`;
  record(6, "After selecting a hotel, auto-show room categories + images",
    named && caps.some((c) => /room/i.test(c)), item6);
} else {
  record(6, "After selecting a hotel, auto-show room categories + images", false, item6);
}

// ---------- ITEM 3: length + repeated preference asking ----------------------
const r1 = await ask("which hotel do you recommend?");
const asksAgain = /what (kind|type)|preference|are you looking for|could you (tell|share)|may i know/i.test(r1.answer);
record(3, "Stop re-asking travel preference after already recommending",
  !asksAgain, `[${r1.secs.toFixed(1)}s] ${r1.answer.replace(/\s+/g, " ").slice(0, 200)}`);

const r2 = await ask("tell me about the rooms");
record(31, "Replies should be shorter", r2.answer.length < 700,
  `reply length = ${r2.answer.length} chars (target < 700)`);

// ---------- ITEM 7: distances ------------------------------------------------
const r3 = await ask("how far is the beach and the airport?");
const hasNum = /\d+(\.\d+)?\s*(km|kilometre|kilometer|m\b|min)/i.test(r3.answer);
record(7, "Distances from key places based on hotel location", hasNum,
  `[${r3.secs.toFixed(1)}s] ${r3.answer.replace(/\s+/g, " ").slice(0, 220)}`);

// ---------- ITEM 8: suggestion chips -----------------------------------------
await sleep(2500);
const chipInfo = await f.evaluate(() => {
  const cands = [...document.querySelectorAll("div")].filter((d) => {
    const kids = [...d.children];
    return kids.length >= 2 && kids.every((k) => k.tagName === "BUTTON") &&
      kids.some((k) => (k.textContent || "").trim().length > 6);
  });
  if (!cands.length) return { found: false };
  const el = cands[cands.length - 1];
  const cs = getComputedStyle(el);
  return {
    found: true, chips: [...el.children].map((c) => (c.textContent || "").trim()),
    overflowX: cs.overflowX, flexWrap: cs.flexWrap,
    scrollW: el.scrollWidth, clientW: el.clientWidth,
    clipped: el.scrollWidth > el.clientWidth + 4,
  };
}).catch(() => ({ found: false }));
await shot("item8_chips_desktop");
record(8, "Suggestion chips visible on laptop (wrap, not clipped)",
  chipInfo.found && !chipInfo.clipped,
  `desktop: ${JSON.stringify(chipInfo)}`);

// phone viewport
const mob = await ctx.newPage();
await mob.setViewportSize({ width: 390, height: 844 });
await mob.goto(URL, { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(6000);
await mob.screenshot({ path: path.join(SHOTS, "item8_chips_mobile.png") }).catch(() => {});
await mob.close();

// ---------- ITEM 9: assistance-from-team -------------------------------------
const r4 = await ask("I want to book it");
const btns = await f.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter(Boolean)).catch(() => []);
const hasWeb = btns.some((b) => /book on website/i.test(b));
const hasHelp = btns.some((b) => /help from our team|assistance/i.test(b));
record(9, "\"Assistance from team\" button beside \"Book on website\"",
  hasWeb && hasHelp, `buttons: ${JSON.stringify(btns.filter((b) => /book|team|assist/i.test(b)))}`);

if (hasHelp) {
  await f.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => /help from our team|assistance/i.test(x.textContent || ""));
    if (b) b.click();
  });
  await sleep(3500);
  await shot("item9_form");
  const fields = await f.$$eval("input, textarea", (els) =>
    els.map((i) => i.placeholder || i.getAttribute("name") || i.type)).catch(() => []);
  record(91, "Assistance form asks for dates / guests / message",
    fields.length >= 3, `fields: ${JSON.stringify(fields)}`);
}

await shot("99_end");
fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(results, null, 1), "utf-8");
console.log(`\n${"=".repeat(74)}`);
console.log(`PASS ${results.filter((r) => r.pass).length} / ${results.length}`);
for (const r of results.filter((x) => !x.pass)) console.log(`   still broken -> ${r.n}. ${r.title}`);
console.log(`\nshots + results -> ${SHOTS}`);
await sleep(20000);
await browser.close();
