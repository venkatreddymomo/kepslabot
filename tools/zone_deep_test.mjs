/**
 * Deep regression suite for the Zone bot — real UI, ground-truth WS frames.
 *   node tools/zone_deep_test.mjs
 *   CITY=Kolkata node tools/zone_deep_test.mjs
 *
 * Cases are grouped by what they exercise. Each declares what the answer must
 * contain, must NOT contain, and which image category it should carry. Cases
 * whose correctness is a judgement call are marked review:true — reported for
 * a human to read, never counted as pass/fail, so the verdict stays honest.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const CITY = process.env.CITY || "Goa";
const SHOTS = path.join(process.cwd(), "tools", "_deep");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SIGNOFF = /let me know if|if you'?d like|if you want,? i can|feel free to ask|would you like to know more|i can also (help|tell|share)/i;
const CONFIRMED_ACTION = /i(?:'ve| have) (?:arranged|booked|ordered|scheduled|sent|placed|confirmed)|will be (?:sent|delivered) to your room|your order (?:is|has been) placed/i;
const DECLINED = /during your stay|once you(?:'ve| have) checked in|after check[- ]?in|not available (?:right now|before)|only available.*stay|can'?t .*(?:before|until).*(?:check|stay)|when you are staying/i;

const CASES = [
  // ---- KB facts -----------------------------------------------------------
  { g: "KB", q: "what time is check-in and check-out?", expect: /\d{1,2}(:\d{2})?\s*(am|pm|noon|hours)/i },
  { g: "KB", q: "do you allow pets?",                   expect: /pet|allow|not allow|no pets/i },
  { g: "KB", q: "is wifi free?",                        expect: /wi.?fi|internet/i },
  { g: "KB", q: "is breakfast included?",               expect: /breakfast|meal plan/i },
  { g: "KB", q: "is there parking?",                    expect: /park/i },
  { g: "KB", q: "what rooms do you have?",              wantImg: "room" },
  { g: "KB", q: "what are the dining options?",         wantImg: "dining" },
  { g: "KB", q: "what are the offers?",                 expect: /%|off|offer/i },

  // ---- distances / nearby -------------------------------------------------
  { g: "GEO", q: "how far is the airport?",             expect: /\d+(\.\d+)?\s*(km|minutes|mins)/i, wantImg: "facade" },
  { g: "GEO", q: "any hospitals nearby?",               forbid: /don'?t have|not listed|no specific/i },
  { g: "GEO", q: "where can I get medicines?",          forbid: /don'?t have|not listed|no specific/i },
  { g: "GEO", q: "any good restaurants nearby?",        forbid: /don'?t have|not listed|no specific/i },

  // ---- phase gating: in-stay asks during PRE-BOOKING -----------------------
  { g: "PHASE", q: "please send extra towels to my room", forbid: CONFIRMED_ACTION, expect: DECLINED, review: true },
  { g: "PHASE", q: "I want to order butter chicken to my room", forbid: CONFIRMED_ACTION, review: true },
  { g: "PHASE", q: "can you clean my room now?",        forbid: CONFIRMED_ACTION, review: true },
  { g: "PHASE", q: "I need laundry picked up",          forbid: CONFIRMED_ACTION, review: true },

  // ---- out of scope -------------------------------------------------------
  { g: "SCOPE", q: "tell me a joke",                    forbid: /why did|knock knock|haha/i },
  { g: "SCOPE", q: "what is 27 times 43?",              forbid: /1161|1,161/ },
  { g: "SCOPE", q: "who is the prime minister of India?", forbid: /modi/i },
  { g: "SCOPE", q: "how do I buy a car?",               review: true },

  // ---- booking ------------------------------------------------------------
  { g: "BOOK", q: "I want to book a room",              expect: /book|website|team/i },
  { g: "BOOK", q: "for 2 adults from 20th to 22nd December", review: true },

  // ---- recall / context ---------------------------------------------------
  { g: "RECALL", q: "which one did I ask about first?", review: true },
  { g: "RECALL", q: "is that the one near the beach?",  review: true },

  // ---- language -----------------------------------------------------------
  { g: "LANG", q: "kya aapke paas swimming pool hai?",  expect: /[ऀ-ॿ]|hai|hain|aapke|kripya|pool/i, review: true },

  // ---- edge ---------------------------------------------------------------
  { g: "EDGE", q: "asdkjhaskjdh",                        review: true },
  { g: "EDGE", q: "?",                                   review: true },
  { g: "EDGE", q: "wat abt teh rooom prise",             review: true },

  // LAST: renders a form that replaces the composer, so nothing can be typed after it.
  { g: "BOOK", q: "can someone from the team call me?", expect: /detail|contact|form|team/i },
];

const frames = [];
const browser = await chromium.launch({
  headless: false, slowMo: 40,
  args: ["--window-size=1500,980", "--window-position=0,0"],
});
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
page.on("websocket", (ws) => {
  ws.on("framereceived", (f) => {
    const s = typeof f.payload === "string" ? f.payload : "";
    if (!s || s.length < 20) return;
    try {
      const j = JSON.parse(s);
      if (j.type !== "response") return;
      const md = (j.data && j.data.metadata) || {};
      frames.push({
        images: (md.images || []).map((i) => `${i.title}(${i.category})`),
        reason: md.smart_image_selection_reason || "",
      });
    } catch { /* ignore */ }
  });
});

const session = `deep_${Date.now()}`;
await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`,
  { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 210000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2200); return true; } await sleep(800); }
  return false;
}

const form = page.locator('input[placeholder="Your full name"]').first();
await form.waitFor({ state: "attached", timeout: 90000 }).catch(() => {});
const openingBubbles = (await bubbles()).length;
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
const choices = await page.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
if (choices.length) {
  await page.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, choices[0]);
  await wait(); await sleep(1500);
}
await page.screenshot({ path: path.join(SHOTS, "01_start.png") }).catch(() => {});
console.log(`city ${CITY} | choices ${choices.length} | starting ${CASES.length} cases\n`);

const rows = [];
const ONLY = (process.env.GROUPS || "").split(",").map((x) => x.trim()).filter(Boolean);
for (const c of (ONLY.length ? CASES.filter((c) => ONLY.includes(c.g)) : CASES)) {
  const before = (await bubbles()).length;
  const nf = frames.length;
  const t0 = Date.now();
  try {
    const composer = page.locator('textarea[placeholder="Type a message..."]').first();
    await composer.fill(c.q);
    await composer.press("Enter");
    await wait();
  } catch (e) {
    rows.push({ ...c, sent: false, note: String(e).slice(0, 60) });
    console.log(`${c.g.padEnd(6)} SEND-FAIL  ${c.q}`);
    continue;
  }
  const all = await bubbles();
  const sent = all.length > before;
  const a = sent ? (all[all.length - 1] || "") : "";
  const fr = frames.slice(nf).pop() || { images: [], reason: "" };
  const secs = +((Date.now() - t0) / 1000).toFixed(1);

  const failures = [];
  if (!sent) failures.push("no reply");
  if (SIGNOFF.test(a)) failures.push("sign-off");
  if (c.expect && !c.expect.test(a)) failures.push("missing expected");
  if (c.forbid && c.forbid.test(a)) failures.push("said forbidden thing");
  if (c.wantImg && !fr.images.some((s) => new RegExp(`\\(${c.wantImg}\\)`, "i").test(s)))
    failures.push(`no ${c.wantImg} image`);

  const verdict = c.review ? (failures.length ? "REVIEW*" : "review") : (failures.length ? "FAIL" : "pass");
  rows.push({ ...c, sent, secs, chars: a.length, images: fr.images, failures, answer: a, verdict });
  console.log(`${c.g.padEnd(6)} ${verdict.padEnd(8)} ${String(secs).padStart(5)}s ${String(c.chars || a.length).padStart(5)}ch  ${c.q.slice(0, 42).padEnd(44)}${failures.join(",")}`);
  console.log(`         ${a.replace(/\s+/g, " ").slice(0, 150)}`);
  fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(rows, null, 1), "utf-8");
}
await page.screenshot({ path: path.join(SHOTS, "02_end.png") }).catch(() => {});

const scored = rows.filter((r) => !r.review);
const failed = scored.filter((r) => r.failures && r.failures.length);
const reviews = rows.filter((r) => r.review);
const times = rows.filter((r) => r.secs).map((r) => r.secs).sort((a, b) => a - b);
console.log(`\n${"=".repeat(78)}`);
console.log(`scored cases : ${scored.length - failed.length} / ${scored.length} passed`);
console.log(`review cases : ${reviews.length} (read these yourself, not auto-scored)`);
console.log(`sign-offs    : ${rows.filter((r) => SIGNOFF.test(r.answer || "")).length}`);
if (times.length) console.log(`latency      : median ${times[Math.floor(times.length / 2)]}s | max ${times[times.length - 1]}s`);
if (failed.length) {
  console.log(`\nFAILURES:`);
  for (const f of failed) console.log(`  [${f.g}] ${f.q}\n      ${f.failures.join(", ")}\n      ${String(f.answer).replace(/\s+/g, " ").slice(0, 190)}`);
}
console.log(failed.length ? `\nDO NOT DEPLOY — ${failed.length} scored failure(s)` : `\nSCORED CHECKS ALL PASS (${CITY})`);
console.log(`\nfull transcript -> ${path.join(SHOTS, "results.json")}`);
await sleep(8000);
await browser.close();
