/**
 * Chat with the DEPLOYED Zone bot through the real embed page, headed.
 *
 *   node tools/zone_deployed_chat.mjs
 *   CITY=Jaisalmer node tools/zone_deployed_chat.mjs
 *
 * The page hosts the widget in a cross-origin iframe (bot.kepsla.ai), so every
 * interaction goes through the frame, exactly as a real guest's would.
 *
 * Transcript + screenshots land in tools/_deployed_shots/.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const URL = process.env.URL || "https://dev-reviews.kepsla.com:18080/zone/bot.html";
const CITY = process.env.CITY || "Udaipur";
const SHOTS = path.join(process.cwd(), "tools", "_deployed_shots");
fs.mkdirSync(SHOTS, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rows = [];
const save = () =>
  fs.writeFileSync(path.join(SHOTS, "transcript.json"), JSON.stringify(rows, null, 1), "utf-8");

const TURNS = [
  "what rooms do you have?",
  "which one is best for a couple?",
  "any hospitals nearby?",
  "any parks nearby?",
  "where can I get medicines?",
  "what are the offers?",
  "is there a pool?",
  "how far is the airport?",
  "I want to book it",
  "can someone from the team call me?",
];

const browser = await chromium.launch({
  headless: false,
  slowMo: 120,
  args: ["--window-size=1500,980", "--window-position=0,0", "--ignore-certificate-errors"],
});
const ctx = await browser.newContext({ viewport: null, ignoreHTTPSErrors: true });
const page = await ctx.newPage();

console.log(`opening ${URL}\ncity: ${CITY}\n`);
await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(4000);

// Open the launcher if the widget starts collapsed.
await page.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => !(x.textContent || "").trim());
  if (b) b.click();
}).catch(() => {});
await sleep(3000);

// The widget frame.
const widget = async () => {
  for (let i = 0; i < 60; i++) {
    const f = page.frames().find((x) => /bot\.kepsla\.ai\/chat/.test(x.url()));
    if (f) {
      const ready = await f.evaluate(() => document.body && document.body.innerText.length > 0).catch(() => false);
      if (ready) return f;
    }
    await sleep(1000);
  }
  throw new Error("widget frame never became ready");
};
const f = await widget();
console.log("widget frame ready\n");

const shot = (n) => page.screenshot({ path: path.join(SHOTS, `${n}.png`) }).catch(() => {});
const botTexts = (fr) =>
  fr.$$eval(".space-y-2 > div.justify-start", (els) => els.map((e) => (e.textContent || "").trim()))
    .catch(() => []);
const typing = (fr) => fr.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);

async function waitReply(fr, ms = 180000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) {
    if ((await typing(fr)) > 0) break;
    await sleep(500);
  }
  while (Date.now() < end) {
    if ((await typing(fr)) === 0) { await sleep(1800); return true; }
    await sleep(800);
  }
  return false;
}

// ---- intro form -----------------------------------------------------------
await shot("01_new_session");
console.log("saved 01_new_session.png");

const fill = async (sel, val) => {
  const el = await f.$(sel);
  if (el) { await el.fill(val); return true; }
  return false;
};
await fill('input[placeholder="Your full name"]', "Sana Shaaista");
await fill('input[placeholder="Phone number"]', "9845061230");
await fill('input[type="email"]', "sana@example.com");

// location dropdown — the list loads from the API, so poll for the city
let picked = false;
for (let i = 0; i < 40 && !picked; i++) {
  picked = await f.evaluate((city) => {
    const sel = [...document.querySelectorAll("select")].pop();
    if (!sel) return false;
    const opt = [...sel.options].find((o) => new RegExp(city, "i").test(o.textContent || ""));
    if (!opt) return false;
    sel.value = opt.value;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }, CITY).catch(() => false);
  if (!picked) await sleep(1500);
}
console.log(picked ? `picked ${CITY}` : `COULD NOT find ${CITY} in the dropdown`);
await sleep(600);

await f.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => /continue|submit|start/i.test(x.textContent || ""));
  if (b) b.click();
}).catch(() => {});

await waitReply(f);
await sleep(2500);
await shot("02_welcome");
const welcome = (await botTexts(f)).pop() || "";
console.log("\n=== WELCOME MESSAGE ===");
console.log(welcome.replace(/\s+/g, " ").slice(0, 700));
rows.push({ n: 0, q: `(picked ${CITY})`, secs: 0, answer: welcome });
save();

// ---- conversation ---------------------------------------------------------
console.log(`\n=== ${TURNS.length} TURNS ===`);
for (let i = 0; i < TURNS.length; i++) {
  const q = TURNS[i];
  try {
    const before = (await botTexts(f)).length;
    const t0 = Date.now();
    const sent = await f.evaluate((text) => {
      const ta = document.querySelector('textarea[placeholder="Type a message..."]')
        || document.querySelector("textarea");
      if (!ta) return false;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
      setter.call(ta, text);
      ta.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    }, q);
    if (!sent) { console.log("  no composer found — stopping"); break; }
    await f.press("textarea", "Enter").catch(async () => {
      await f.evaluate(() => {
        const b = [...document.querySelectorAll("button")].find((x) => /send/i.test(x.getAttribute("aria-label") || ""));
        if (b) b.click();
      });
    });

    const ok = await waitReply(f);
    const secs = (Date.now() - t0) / 1000;
    const all = await botTexts(f);
    const answer = all[all.length - 1] || "";
    rows.push({ n: i + 1, q, secs, ok: ok && all.length > before, answer });
    save();
    console.log(`\n${String(i + 1).padStart(2)}. [${secs.toFixed(1)}s] ${q}`);
    console.log(`    ${answer.replace(/\s+/g, " ").slice(0, 320)}`);
    if (i === 2 || i === 3) await shot(`03_turn${i + 1}`);
  } catch (err) {
    console.log(`\n${i + 1}. CRASHED: ${String(err).slice(0, 120)}`);
    rows.push({ n: i + 1, q, secs: 0, ok: false, answer: "CRASH" });
    save();
    break;
  }
}
await shot("04_end");

const t = rows.filter((r) => r.secs > 0).map((r) => r.secs).sort((a, b) => a - b);
console.log(`\n${"=".repeat(70)}`);
if (t.length) {
  const p = (x) => t[Math.min(t.length - 1, Math.floor(t.length * x))];
  console.log(`LATENCY over ${t.length} turns — median ${p(0.5).toFixed(1)}s | p90 ${p(0.9).toFixed(1)}s | max ${t[t.length - 1].toFixed(1)}s`);
}
console.log(`failed turns: ${rows.filter((r) => r.ok === false).length}`);
console.log(`transcript -> ${path.join(SHOTS, "transcript.json")}`);
console.log("\nbrowser stays open 25s…");
await sleep(25000);
await browser.close();
