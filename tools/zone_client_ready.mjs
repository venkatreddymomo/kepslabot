/**
 * Client-readiness check for the Zone bot.
 *   node tools/zone_client_ready.mjs
 *
 * Scores the ORIGINAL nine requirements plus everything added since, in one
 * Goa conversation (two hotels, so the multi-property behaviour is exercised).
 * Judgement calls are reported, never auto-scored.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(process.cwd(), "tools", "_client_ready");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SIGNOFF = /let me know if|if you'?d like|if you want,? i can|feel free to ask|would you like to know more|i can also (help|tell|share)/i;
const SQFT = /sq\.?\s*ft|square (feet|foot)/i;
const CONFIRMED = /i(?:'ve| have) (?:arranged|booked|ordered|scheduled|sent|placed)|will be (?:sent|delivered) to your room/i;
const NOTFOUND = /don'?t have|not listed|no specific|do not have/i;

const CASES = [
  { g: "KB",     q: "what rooms do you have?",             img: "room",   noSqft: true },
  { g: "KB",     q: "tell me about the Junior Suite",      img: "room",   noSqft: true },
  { g: "KB",     q: "what are the dining options?",        img: "dining" },
  { g: "KB",     q: "what time is check-in?",              expect: /\d{1,2}(:\d{2})?\s*(am|pm|noon)/i },
  { g: "KB",     q: "do you allow pets?",                  expect: /pet/i },
  { g: "GEO",    q: "any hospitals nearby?",               forbid: NOTFOUND, expect: /\d+\s*(m|km)\b/i },
  { g: "GEO",    q: "where can I get medicines?",          forbid: NOTFOUND, expect: /\d+\s*(m|km)\b/i },
  { g: "GEO",    q: "how far is the airport?",             expect: /\d+\s*km/i, img: "facade" },
  { g: "PHASE",  q: "please send extra towels to my room", forbid: CONFIRMED, review: true },
  { g: "SCOPE",  q: "tell me a joke",                      forbid: /why did|knock knock/i },
  { g: "SCOPE",  q: "what is 27 times 43?",                forbid: /1161|1,161/ },
  { g: "SWITCH", q: "what about the Parra one?",           review: true },
  { g: "SWITCH", q: "what rooms does it have?",            forbid: /junior suite|executive suite/i, noSqft: true },
  { g: "SWITCH", q: "go back to the Calangute one",        review: true },
  { g: "SWITCH", q: "what rooms do you have?",             forbid: /family area/i },
  { g: "BOOK",   q: "I want to book a room",               expect: /book|website|team/i },
  { g: "BOOK",   q: "can someone from the team call me?",  expect: /detail|contact|form|team/i },  // LAST
];

const ws = [];
const browser = await chromium.launch({ headless: false, slowMo: 40,
  args: ["--window-size=1500,980", "--window-position=0,0"] });
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
page.on("websocket", (s) => s.on("framereceived", (fr) => {
  const t = typeof fr.payload === "string" ? fr.payload : "";
  if (!t || t.length < 20) return;
  try {
    const j = JSON.parse(t);
    if (j.type !== "response") return;
    const md = (j.data && j.data.metadata) || {};
    ws.push({ images: (md.images || []).map((i) => `${i.title}(${i.category})`) });
  } catch {}
}));

const session = `ready_${Date.now()}`;
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
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2200); return true; } await sleep(800); }
  return false;
}

// ---- REQ 1: opening screen -------------------------------------------------
await page.screenshot({ path: path.join(SHOTS, "01_open.png") }).catch(() => {});
const openBubbles = (await bubbles()).length;
const formShown = await page.locator('input[placeholder="Your full name"]').count();

const form = page.locator('input[placeholder="Your full name"]').first();
await form.waitFor({ state: "visible", timeout: 90000 }).catch(() => {});
await form.fill("Sana Shaaista").catch(() => {});
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
await page.screenshot({ path: path.join(SHOTS, "02_welcome.png") }).catch(() => {});

// ---- REQ 5: multi-property welcome + choice buttons ------------------------
const welcome = (await bubbles()).pop() || "";
const welcomeImgs = (ws[ws.length - 1] || {}).images || [];
const choices = await page.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));

// ---- REQ 6: reply after picking one property -------------------------------
const nfPick = ws.length;
const cal = choices.find((c) => /calangute/i.test(c)) || choices[0];
if (cal) {
  await page.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, cal);
  await wait(); await sleep(2000);
}
const pick = (await bubbles()).pop() || "";
const pickImgs = (ws.slice(nfPick).pop() || {}).images || [];
await page.screenshot({ path: path.join(SHOTS, "03_after_pick.png") }).catch(() => {});

console.log(`\nWELCOME      ${welcome.length} ch | choices ${choices.length} | imgs ${welcomeImgs.length}`);
console.log(`AFTER PICK   ${pick.length} ch | imgs ${JSON.stringify(pickImgs).slice(0, 60)}`);
console.log(`             sq.ft ${SQFT.test(pick)} | dining ${/dining|cafe c|co-?bar/i.test(pick)} | offers ${/offer|% off/i.test(pick)} | sign-off ${SIGNOFF.test(pick)}`);
console.log(`\n${"CASE".padEnd(42)}${"secs".padStart(5)} ${"ch".padStart(5)}  result`);
console.log("-".repeat(104));

const rows = [];
for (const c of CASES) {
  const before = (await bubbles()).length;
  const nf = ws.length;
  const t0 = Date.now();
  let sent = false;
  try {
    const composer = page.locator('textarea[placeholder="Type a message..."]').first();
    await composer.fill(c.q); await composer.press("Enter");
    await wait();
    sent = (await bubbles()).length > before;
  } catch { sent = false; }
  const all = await bubbles();
  const a = sent ? (all[all.length - 1] || "") : "";
  const imgs = (ws.slice(nf).pop() || {}).images || [];
  const secs = +((Date.now() - t0) / 1000).toFixed(1);

  const bad = [];
  if (!sent) bad.push("no reply");
  if (SIGNOFF.test(a)) bad.push("sign-off");
  if (c.expect && !c.expect.test(a)) bad.push("missing expected");
  if (c.forbid && c.forbid.test(a)) bad.push("forbidden content");
  if (c.noSqft && SQFT.test(a)) bad.push("volunteered sq.ft");
  if (c.img && !imgs.some((s) => new RegExp(`\\(${c.img}\\)`, "i").test(s))) bad.push(`no ${c.img} image`);

  rows.push({ ...c, sent, secs, chars: a.length, imgs, bad, answer: a });
  console.log(`${(c.g + " " + c.q).slice(0, 40).padEnd(42)}${String(secs).padStart(5)} ${String(a.length).padStart(5)}  ${c.review ? "review" : (bad.length ? "FAIL " + bad.join(",") : "pass")}`);
  fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify({ welcome, pick, choices, rows }, null, 1), "utf-8");
}
await page.screenshot({ path: path.join(SHOTS, "04_end.png") }).catch(() => {});

const scored = rows.filter((r) => !r.review);
const failed = scored.filter((r) => r.bad.length);
const t = rows.filter((r) => r.secs).map((r) => r.secs).sort((a, b) => a - b);
const REQ = [
  ["1  opening shows the form only, not two messages", openBubbles <= 1 && formShown > 0],
  ["3a replies stay reasonable (median under 600ch)",  rows.map((r) => r.chars).sort((a, b) => a - b)[Math.floor(rows.length / 2)] < 600],
  ["3b no sign-off anywhere",                          rows.every((r) => !SIGNOFF.test(r.answer)) && !SIGNOFF.test(pick)],
  ["4  welcome images start facade/room",              /\((facade|room)\)/i.test(welcomeImgs[0] || "")],
  ["5  multi-property welcome offers choice buttons",  choices.length >= 2],
  ["6  picking a hotel shows its rooms + images",      /room/i.test(pick) && pickImgs.length > 0],
  ["6b pick reply is not bloated (under 800ch)",       pick.length < 800],
  ["6c pick reply volunteers no square footage",       !SQFT.test(pick)],
  ["7  distances to key places are real",              !!(rows.find((r) => /airport/.test(r.q)) || {}).sent && !(rows.find((r) => /airport/.test(r.q)) || {}).bad.length],
  ["9  book-on-website + team assist both offered",    !(rows.find((r) => /book a room/.test(r.q)) || {}).bad.length],
  ["G  nearby lookups return named places + distance", !(rows.find((r) => /hospitals/.test(r.q)) || {}).bad.length && !(rows.find((r) => /medicines/.test(r.q)) || {}).bad.length],
  ["P  property switching keeps scope clean",          rows.filter((r) => r.g === "SWITCH" && !r.review).every((r) => !r.bad.length)],
];
console.log(`\n${"=".repeat(70)}`);
for (const [n, ok] of REQ) console.log(`  ${ok ? "PASS" : "FAIL"}  ${n}`);
const reqFail = REQ.filter(([, ok]) => !ok);
console.log(`\nrequirements ${REQ.length - reqFail.length}/${REQ.length} | scored cases ${scored.length - failed.length}/${scored.length}`);
if (t.length) console.log(`latency median ${t[Math.floor(t.length / 2)]}s | max ${t[t.length - 1]}s`);
if (failed.length) { console.log("\nfailures:"); for (const f of failed) console.log(`  ${f.q} -> ${f.bad.join(", ")}\n     ${String(f.answer).replace(/\s+/g, " ").slice(0, 160)}`); }
console.log((reqFail.length || failed.length) ? "\nNOT READY" : "\nREADY FOR CLIENTS");
await sleep(10000);
await browser.close();
