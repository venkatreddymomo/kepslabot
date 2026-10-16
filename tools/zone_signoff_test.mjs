/**
 * Deploy sign-off test — real UI, ground-truth data.
 *   node tools/zone_signoff_test.mjs           (Goa)
 *   CITY=Kolkata node tools/zone_signoff_test.mjs
 *
 * Drives the widget exactly as a guest does, but reads images from the
 * WebSocket frames rather than the DOM. The DOM carousel only renders the
 * visible slide, so caption-scraping reported stale values twice and produced
 * both a false pass and a false fail.
 *
 * The listener is attached BEFORE navigation — a socket opened during page load
 * is missed otherwise.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const CITY = process.env.CITY || "Goa";
const SHOTS = path.join(process.cwd(), "tools", "_signoff");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SIGNOFF = /let me know if|if you'?d like|if you want,? i can|feel free to ask|would you like to know more|i can also (help|tell|share)/i;
const NOT_FOUND = /don'?t have|not listed|no specific|do not have/i;

const TURNS = [
  { q: "what rooms do you have?",         want: "room"          },
  { q: "tell me about the Junior Suite",  want: "room|suite"    },
  { q: "what are the dining options?",    want: "dining"        },
  { q: "any hospitals nearby?",           want: "facade", named: true },
  { q: "where can I get medicines?",      want: "facade", named: true },
  { q: "how far is the airport?",         want: "facade"        },
  { q: "is there a pool?",                want: "pool|facade|room" },
  { q: "what are the offers?",            want: "*"             },
];

// ---- capture every image the server sends, per frame -----------------------
const frames = [];
const browser = await chromium.launch({
  headless: false, slowMo: 60,
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
        at: Date.now(),
        text: (j.data && j.data.message) || "",
        images: (md.images || []).map((i) => `${i.title}(${i.category})`),
        reason: md.smart_image_selection_reason || "",
      });
    } catch { /* not our frame */ }
  });
});

const session = `signoff_${Date.now()}`;
await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`,
  { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 200000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2200); return true; } await sleep(800); }
  return false;
}

// ---- intro form ------------------------------------------------------------
await page.screenshot({ path: path.join(SHOTS, "01_open.png") }).catch(() => {});
const openingBubbles = (await bubbles()).length;
const formShown = await page.locator('input[placeholder="Your full name"]').count();

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
await page.screenshot({ path: path.join(SHOTS, "02_welcome.png") }).catch(() => {});

const welcome = (await bubbles()).pop() || "";
const choices = await page.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
const welcomeFrame = frames[frames.length - 1] || {};
console.log(`\nWELCOME   ${welcome.length} chars | choices ${choices.length} | images ${JSON.stringify(welcomeFrame.images || [])}`);

if (choices.length) {
  await page.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, choices[0]);
  await wait(); await sleep(2000);
}
const pick = (await bubbles()).pop() || "";
const pickFrame = frames[frames.length - 1] || {};
console.log(`AFTER PICK ${pick.length} chars | sign-off ${SIGNOFF.test(pick)} | images ${JSON.stringify(pickFrame.images || [])}`);

// ---- turns -----------------------------------------------------------------
const results = [];
console.log(`\n${"QUESTION".padEnd(34)}${"secs".padStart(5)}  ${"REASON".padEnd(26)}IMAGES`);
console.log("-".repeat(116));
for (const t of TURNS) {
  const before = (await bubbles()).length;
  const nFrames = frames.length;
  const t0 = Date.now();
  const composer = page.locator('textarea[placeholder="Type a message..."]').first();
  await composer.fill(t.q);
  await composer.press("Enter");
  await wait();
  const all = await bubbles();
  const sent = all.length > before;
  const answer = sent ? (all[all.length - 1] || "") : "";
  const fr = frames.slice(nFrames).pop() || { images: [], reason: "" };
  const secs = +((Date.now() - t0) / 1000).toFixed(1);
  const imgOk = t.want === "*" ? true
    : (fr.images.length === 0 ? false
      : fr.images.some((s) => new RegExp(`\\((${t.want})\\)`, "i").test(s)));
  results.push({ ...t, sent, secs, chars: answer.length, signoff: SIGNOFF.test(answer),
                 notFound: t.named ? NOT_FOUND.test(answer) : false,
                 images: fr.images, reason: fr.reason, imgOk, answer });
  console.log(`${t.q.padEnd(34)}${String(secs).padStart(5)}  ${String(fr.reason || "-").padEnd(26)}${JSON.stringify(fr.images).slice(0, 60)}`);
}
await page.screenshot({ path: path.join(SHOTS, "03_end.png") }).catch(() => {});
fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(results, null, 1), "utf-8");

// ---- verdict ---------------------------------------------------------------
const sent = results.filter((r) => r.sent);
const distinct = new Set(sent.map((r) => r.answer)).size;
const times = sent.map((r) => r.secs).sort((a, b) => a - b);
const checks = [
  ["opening shows form only",     openingBubbles <= 1 && formShown > 0],
  ["every turn answered",         sent.length === TURNS.length],
  ["replies all distinct",        distinct === sent.length],
  ["no sign-off anywhere",        results.every((r) => !r.signoff) && !SIGNOFF.test(pick)],
  ["hospitals named",             !(results.find((r) => /hospital/.test(r.q)) || {}).notFound],
  ["medicines named",             !(results.find((r) => /medicines/.test(r.q)) || {}).notFound],
  ["welcome has choice buttons",  choices.length >= (CITY === "Goa" || CITY === "Kolkata" ? 2 : 1)],
  ["images correct per topic",    results.every((r) => r.imgOk)],
];
console.log(`\n${"=".repeat(70)}`);
for (const [name, ok] of checks) console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}`);
const failed = checks.filter(([, ok]) => !ok);
console.log(`\n${checks.length - failed.length} / ${checks.length} checks passed`);
if (times.length) console.log(`latency median ${times[Math.floor(times.length / 2)]}s | max ${times[times.length - 1]}s`);
console.log(failed.length ? `\nDO NOT DEPLOY — ${failed.map(([n]) => n).join(", ")}` : `\nSAFE TO DEPLOY (${CITY})`);
await sleep(10000);
await browser.close();
