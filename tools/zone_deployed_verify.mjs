/**
 * Post-deploy verification against the live embed.
 *   node tools/zone_deployed_verify.mjs
 *   CITY=Kolkata node tools/zone_deployed_verify.mjs
 *
 * Same checks proven locally, but driven through the real embed page, whose
 * widget lives in a cross-origin iframe (bot.kepsla.ai) — so every interaction
 * goes through the frame exactly as a guest's would.
 *
 * Images are read from the WebSocket frames where available, falling back to
 * the carousel caption. The DOM caption alone lies (it renders only the visible
 * slide), which produced both a false pass and a false fail during local work.
 *
 * The callback case runs LAST: it renders a form that replaces the composer,
 * so nothing can be typed after it.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const URL = process.env.URL || "https://dev-reviews.kepsla.com:18080/zone/bot.html";
const CITY = process.env.CITY || "Goa";
const SHOTS = path.join(process.cwd(), "tools", "_deployed_verify");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SIGNOFF = /let me know if|if you'?d like|if you want,? i can|feel free to ask|would you like to know more|i can also (help|tell|share)/i;
const CONFIRMED = /i(?:'ve| have) (?:arranged|booked|ordered|scheduled|sent|placed)|will be (?:sent|delivered) to your room/i;
const NOT_FOUND = /don'?t have|not listed|no specific|do not have/i;

const CASES = [
  { g: "KB",    q: "what rooms do you have?",              wantImg: "room" },
  { g: "KB",    q: "tell me about the Junior Suite",       wantImg: "room" },
  { g: "KB",    q: "what are the dining options?",         wantImg: "dining" },
  { g: "KB",    q: "what time is check-in?",               expect: /\d{1,2}(:\d{2})?\s*(am|pm|noon)/i },
  { g: "KB",    q: "do you allow pets?",                   expect: /pet/i },
  { g: "GEO",   q: "any hospitals nearby?",                forbid: NOT_FOUND },
  { g: "GEO",   q: "where can I get medicines?",           forbid: NOT_FOUND },
  { g: "GEO",   q: "how far is the airport?",              expect: /\d+(\.\d+)?\s*(km|min)/i, wantImg: "facade" },
  { g: "PHASE", q: "please send extra towels to my room",  forbid: CONFIRMED, review: true },
  { g: "PHASE", q: "I need laundry picked up",             forbid: CONFIRMED, review: true },
  { g: "SCOPE", q: "tell me a joke",                       forbid: /why did|knock knock/i },
  { g: "SCOPE", q: "what is 27 times 43?",                 forbid: /1161|1,161/ },
  { g: "SWITCH", q: "what about the Parra one?",           review: true },
  { g: "SWITCH", q: "what rooms does it have?",            forbid: /junior suite|executive suite/i },
  { g: "SWITCH", q: "go back to the Calangute one",        review: true },
  { g: "SWITCH", q: "what rooms do you have?",             forbid: /family area/i },
  { g: "BOOK",  q: "I want to book a room",                expect: /book|website|team/i },
  { g: "BOOK",  q: "can someone from the team call me?",   expect: /detail|contact|form|team/i },
];

const wsFrames = [];
const browser = await chromium.launch({
  headless: false, slowMo: 60,
  args: ["--window-size=1500,980", "--window-position=0,0", "--ignore-certificate-errors"],
});
const ctx = await browser.newContext({ viewport: null, ignoreHTTPSErrors: true });
const page = await ctx.newPage();
page.on("websocket", (ws) => {
  ws.on("framereceived", (fr) => {
    const s = typeof fr.payload === "string" ? fr.payload : "";
    if (!s || s.length < 20) return;
    try {
      const j = JSON.parse(s);
      if (j.type !== "response") return;
      const md = (j.data && j.data.metadata) || {};
      wsFrames.push({
        images: (md.images || []).map((i) => `${i.title}(${i.category})`),
        reason: md.smart_image_selection_reason || "",
      });
    } catch { /* ignore */ }
  });
});

console.log(`opening ${URL}\ncity: ${CITY}\n`);
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
if (!f) { console.log("widget never loaded"); await browser.close(); process.exit(1); }
console.log("widget ready\n");

const bubbles = () => f.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => f.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
const caption = () => f.evaluate(() => {
  const c = [...document.querySelectorAll("*")]
    .map((e) => (e.childElementCount === 0 ? (e.textContent || "").trim() : ""))
    .filter((t) => /^\d+\s*\/\s*\d+\s*\|\s*\w+/.test(t));
  return c[c.length - 1] || "";
}).catch(() => "");

async function wait(ms = 200000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2200); return true; } await sleep(800); }
  return false;
}

// ---- intro form ------------------------------------------------------------
await page.screenshot({ path: path.join(SHOTS, "01_open.png") }).catch(() => {});
const openingBubbles = (await bubbles()).length;
// Wait for the form to exist first. Filling before it renders made the helper
// silently skip every field (it no-ops when the element is missing), Continue
// was clicked on an empty form, and the composer stayed locked — 18 turns then
// timed out identically and looked like a broken deployment.
await f.locator('input[placeholder="Your full name"]')
  .waitFor({ state: "visible", timeout: 90000 }).catch(() => {});
const fill = async (s, v) => {
  const el = await f.$(s);
  if (!el) { console.log(`  !! field not found: ${s}`); return false; }
  await el.fill(v);
  return true;
};
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
await page.screenshot({ path: path.join(SHOTS, "02_welcome.png") }).catch(() => {});

// Abort loudly rather than reporting 18 phantom failures if the form is still up.
const formStillUp = await f.locator('input[placeholder="Your full name"]').count().catch(() => 0);
const composerReady = await f.locator('textarea[placeholder="Type a message..."]').count().catch(() => 0);
if (formStillUp > 0 || composerReady === 0) {
  console.log(`
ABORT — intro form did not submit (form=${formStillUp} composer=${composerReady}).`);
  console.log("This is a test-harness problem, not a bot result. See 02_welcome.png.");
  await browser.close();
  process.exit(2);
}

const welcome = (await bubbles()).pop() || "";
const choices = await f.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
console.log(`WELCOME ${welcome.length} chars | choices ${choices.length} | ${welcome.replace(/\s+/g, " ").slice(0, 110)}\n`);

const calangute = choices.find((c) => /calangute/i.test(c)) || choices[0];
if (calangute) {
  await f.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, calangute);
  await wait(); await sleep(2000);
}
const pick = (await bubbles()).pop() || "";
console.log(`AFTER PICK ${pick.length} chars | sign-off ${SIGNOFF.test(pick)}\n`);

// ---- cases -------------------------------------------------------------------
const rows = [];
for (const c of CASES) {
  const before = (await bubbles()).length;
  const nf = wsFrames.length;
  const t0 = Date.now();
  let sent = false;
  try {
    const composer = f.locator('textarea[placeholder="Type a message..."]').first();
    await composer.fill(c.q);
    await composer.press("Enter");
    await wait();
    sent = (await bubbles()).length > before;
  } catch { sent = false; }
  const all = await bubbles();
  const a = sent ? (all[all.length - 1] || "") : "";
  const secs = +((Date.now() - t0) / 1000).toFixed(1);
  const fr = wsFrames.slice(nf).pop();
  const imgs = fr ? fr.images : [];
  const cap = imgs.length ? "" : await caption();

  const fails = [];
  if (!sent) fails.push("no reply");
  if (SIGNOFF.test(a)) fails.push("sign-off");
  if (c.expect && !c.expect.test(a)) fails.push("missing expected");
  if (c.forbid && c.forbid.test(a)) fails.push("forbidden content");
  if (c.wantImg) {
    const hit = imgs.some((s) => new RegExp(`\\(${c.wantImg}\\)`, "i").test(s))
      || new RegExp(c.wantImg, "i").test(cap);
    if (!hit) fails.push(`no ${c.wantImg} image`);
  }
  const verdict = c.review ? (fails.length ? "review*" : "review") : (fails.length ? "FAIL" : "pass");
  rows.push({ ...c, sent, secs, chars: a.length, imgs, cap, fails, answer: a, verdict });
  console.log(`${c.g.padEnd(7)}${verdict.padEnd(8)}${String(secs).padStart(5)}s ${String(a.length).padStart(4)}ch  ${c.q.slice(0, 38).padEnd(40)}${(imgs.length ? JSON.stringify(imgs) : cap).slice(0, 40)}  ${fails.join(",")}`);
  console.log(`        ${a.replace(/\s+/g, " ").slice(0, 150)}`);
  fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(rows, null, 1), "utf-8");
}
await page.screenshot({ path: path.join(SHOTS, "03_end.png") }).catch(() => {});

const scored = rows.filter((r) => !r.review);
const failed = scored.filter((r) => r.fails.length);
const t = rows.filter((r) => r.secs).map((r) => r.secs).sort((a, b) => a - b);
console.log(`\n${"=".repeat(76)}`);
console.log(`opening bubbles : ${openingBubbles} (want 1)`);
console.log(`choice buttons  : ${choices.length}`);
console.log(`scored          : ${scored.length - failed.length} / ${scored.length}`);
console.log(`sign-offs       : ${rows.filter((r) => SIGNOFF.test(r.answer || "")).length} / ${rows.length}`);
if (t.length) console.log(`latency         : median ${t[Math.floor(t.length / 2)]}s | max ${t[t.length - 1]}s`);
if (failed.length) {
  console.log(`\nFAILURES:`);
  for (const x of failed) console.log(`  [${x.g}] ${x.q}\n      ${x.fails.join(", ")}\n      ${String(x.answer).replace(/\s+/g, " ").slice(0, 180)}`);
}
console.log(failed.length ? `\nDEPLOYED HAS ${failed.length} FAILURE(S)` : `\nDEPLOYED LOOKS GOOD`);
await sleep(12000);
await browser.close();
