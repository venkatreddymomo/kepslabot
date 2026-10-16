/**
 * 50-turn single conversation against the deployed embed.
 *   node tools/zone_50turn.mjs
 *
 * Switches property and city repeatedly, records the images attached to every
 * turn, and checks three things about them:
 *   1. present   — did a turn that should show pictures show any
 *   2. on-topic  — room question -> room photo, dining -> dining, etc.
 *   3. no leak   — the photo belongs to the property being discussed
 *
 * (3) is provable because the four labelled galleries have unique titles:
 *   Junior Suite / Executive Suite / Co-Bar / The Lawns -> Calangute only
 *   Family Area / Connect Rooms / Premium Rooms         -> Parra only
 *   Zone Luxury / Zone Suite                            -> New Town only
 *   Zone Luxe / Flurys                                  -> New Market only
 *
 * Image accuracy is scored ONLY for Goa and Kolkata, the properties whose
 * galleries have been rebuilt. Other cities still run, for answer quality and
 * latency, but their images are recorded without judgement.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const URL = process.env.URL || "https://dev-reviews.kepsla.com:18080/zone/bot.html";
const SHOTS = path.join(process.cwd(), "tools", "_50turn");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SIGNOFF = /let me know if|if you'?d like|if you want,? i can|feel free to ask|would you like to know more|i can also (help|tell|share)/i;

// title -> the only property it can belong to
const FINGERPRINT = {
  "junior suite": "calangute", "executive suite": "calangute", "co-bar": "calangute", "the lawns": "calangute",
  "family area": "parra", "connect rooms": "parra", "premium rooms": "parra",
  "zone luxury": "newtown", "zone suite": "newtown",
  "zone luxe": "newmarket", "flurys": "newmarket",
};
const SCOPED = { calangute: /calangute/i, parra: /parra/i, newtown: /new town/i, newmarket: /new market/i };

// scope: which property we EXPECT to be active. null = don't score images.
const T = (scope, q, wantImg) => ({ scope, q, wantImg });
const TURNS = [
  // --- Goa / Calangute
  T("calangute", "what rooms do you have?", "room"),
  T("calangute", "tell me about the Junior Suite", "room"),
  T("calangute", "what about the Executive Suite?", "room"),
  T("calangute", "what are the dining options?", "dining"),
  T("calangute", "tell me about Cafe C", "dining"),
  T("calangute", "is there a pool?", null),
  T("calangute", "what about Co-Bar?", "dining"),
  T("calangute", "do you have a banquet hall?", null),
  T("calangute", "what time is check-in?", null),
  T("calangute", "do you allow pets?", null),
  T("calangute", "what are the offers?", null),
  T("calangute", "how far is the airport?", null),
  // --- switch to Parra
  T("parra", "what about the Parra one?", null),
  T("parra", "what rooms does it have?", "room"),
  T("parra", "tell me about the Family Area", "room"),
  T("parra", "what dining is there?", "dining"),
  T("parra", "is there a pool there?", null),
  T("parra", "how far is the beach?", null),
  T("parra", "does it have a town hall?", null),
  T("parra", "which is better for families?", null),
  // --- switch city: Kolkata / New Town
  T(null,      "tell me about your Kolkata hotels", null),
  T("newtown", "tell me about the New Town one", null),
  T("newtown", "what rooms do you have?", "room"),
  T("newtown", "tell me about the Zone Suite", "room"),
  T("newtown", "what are the dining options?", "dining"),
  T("newtown", "tell me about Bazaar", "dining"),
  T("newtown", "what about Z-Bar?", "dining"),
  T("newtown", "is there a spa?", null),
  T("newtown", "is there a gym?", null),
  T("newtown", "how far is the airport?", null),
  T("newtown", "any hospitals nearby?", null),
  T("newtown", "what are the offers?", null),
  // --- switch to New Market
  T("newmarket", "what about the New Market one?", null),
  T("newmarket", "what rooms does it have?", "room"),
  T("newmarket", "tell me about Zone Luxe", "room"),
  T("newmarket", "what dining is there?", "dining"),
  T("newmarket", "tell me about Flurys", "dining"),
  T("newmarket", "is it near New Market?", null),
  T("newmarket", "does it have a pool?", null),
  T(null,        "compare both Kolkata hotels", null),
  // --- other cities: answers + latency only, images not scored
  T(null, "do you have hotels in Jaipur?", null),
  T(null, "tell me about the Jaipur one", null),
  T(null, "what rooms are there?", null),
  T(null, "what about Udaipur?", null),
  T(null, "how far is the lake?", null),
  T(null, "do you have anything in Chennai?", null),
  // --- back to Goa
  T("calangute", "lets go back to Calangute Goa", null),
  T("calangute", "what rooms do you have?", "room"),
  T("calangute", "I want to book it", null),
  T(null,        "can someone from the team call me?", null),   // LAST: renders a form
];

const wsFrames = [];
const browser = await chromium.launch({
  headless: false, slowMo: 40,
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
      const pd = ((md.entities || {}).pending_data) || {};
      wsFrames.push({
        images: (md.images || []).map((i) => ({ t: i.title, c: i.category })),
        reason: md.smart_image_selection_reason || "",
        scope: pd.active_property_names || [],
      });
    } catch { /* ignore */ }
  });
});

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

const bubbles = () => f.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => f.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 200000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2000); return true; } await sleep(800); }
  return false;
}

await f.locator('input[placeholder="Your full name"]').waitFor({ state: "visible", timeout: 90000 }).catch(() => {});
const fill = async (s, v) => { const el = await f.$(s); if (el) { await el.fill(v); return true; } return false; };
await fill('input[placeholder="Your full name"]', "Sana Shaaista");
await fill('input[placeholder="Phone number"]', "9845061230");
await fill('input[type="email"]', "sana@example.com");
for (let i = 0; i < 40; i++) {
  const ok = await f.evaluate(() => {
    const s = [...document.querySelectorAll("select")].pop(); if (!s) return false;
    const o = [...s.options].find((x) => /goa/i.test(x.textContent || ""));
    if (!o) return false;
    s.value = o.value; s.dispatchEvent(new Event("change", { bubbles: true })); return true;
  }).catch(() => false);
  if (ok) break; await sleep(1500);
}
await f.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => /continue/i.test(x.textContent || ""));
  if (b) b.click();
});
await wait(); await sleep(2500);
if (await f.locator('input[placeholder="Your full name"]').count().catch(() => 0)) {
  console.log("ABORT — intro form did not submit (harness problem, not a bot result)");
  await browser.close(); process.exit(2);
}
const choices = await f.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
const cal = choices.find((c) => /calangute/i.test(c)) || choices[0];
if (cal) {
  await f.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, cal);
  await wait(); await sleep(2000);
}
await page.screenshot({ path: path.join(SHOTS, "01_start.png") }).catch(() => {});
console.log(`started on: ${cal}\n`);
console.log(`${"#".padStart(3)} ${"secs".padStart(5)} ${"ch".padStart(5)}  ${"LANE".padEnd(26)}${"QUESTION".padEnd(34)}IMAGES`);
console.log("-".repeat(132));

const rows = [];
for (let i = 0; i < TURNS.length; i++) {
  const c = TURNS[i];
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
  const fr = wsFrames.slice(nf).pop() || { images: [], reason: "", scope: [] };
  const imgs = fr.images || [];

  const problems = [];
  if (!sent) problems.push("no reply");
  if (SIGNOFF.test(a)) problems.push("sign-off");
  if (c.scope) {
    // leak: an image that can only belong to a different property
    for (const im of imgs) {
      const owner = FINGERPRINT[String(im.t || "").toLowerCase()];
      if (owner && owner !== c.scope) problems.push(`IMAGE LEAK ${im.t} (${owner})`);
    }
    // text leak: the answer names the wrong property's exclusive room
    for (const [title, owner] of Object.entries(FINGERPRINT)) {
      if (owner !== c.scope && new RegExp(`\\b${title.replace(/[-.]/g, ".")}\\b`, "i").test(a))
        problems.push(`TEXT LEAK ${title} (${owner})`);
    }
    if (c.wantImg) {
      if (!imgs.length) problems.push("no images at all");
      else if (!imgs.some((im) => String(im.c || "").toLowerCase() === c.wantImg))
        problems.push(`wrong category (want ${c.wantImg})`);
    }
  }
  rows.push({ n: i + 1, ...c, sent, secs, chars: a.length, reason: fr.reason,
              images: imgs.map((im) => `${im.t}(${im.c})`), wsScope: fr.scope, problems, answer: a });
  const flag = problems.length ? "  <<< " + problems.join(" | ") : "";
  console.log(`${String(i + 1).padStart(3)} ${String(secs).padStart(5)} ${String(a.length).padStart(5)}  ${String(fr.reason || "-").padEnd(26)}${c.q.slice(0, 32).padEnd(34)}${JSON.stringify(imgs.map((x) => x.c)).slice(0, 34)}${flag}`);
  fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(rows, null, 1), "utf-8");
  if (i === 19 || i === 39) await page.screenshot({ path: path.join(SHOTS, `mid_${i + 1}.png`) }).catch(() => {});
}
await page.screenshot({ path: path.join(SHOTS, "99_end.png") }).catch(() => {});

// ---------------- report ----------------
const sent = rows.filter((r) => r.sent);
const t = sent.map((r) => r.secs).sort((a, b) => a - b);
const p = (x) => t[Math.min(t.length - 1, Math.floor(t.length * x))];
const scored = rows.filter((r) => r.scope);
const leaks = rows.filter((r) => r.problems.some((x) => x.includes("LEAK")));
const noImg = scored.filter((r) => r.wantImg && r.problems.includes("no images at all"));
const wrongCat = scored.filter((r) => r.problems.some((x) => x.startsWith("wrong category")));
const withImgs = rows.filter((r) => r.images.length);

console.log(`\n${"=".repeat(80)}`);
console.log(`turns answered      : ${sent.length} / ${rows.length}`);
console.log(`sign-offs           : ${rows.filter((r) => SIGNOFF.test(r.answer || "")).length}`);
console.log(`\nLATENCY  median ${p(0.5)}s | p90 ${p(0.9)}s | min ${t[0]}s | max ${t[t.length - 1]}s`);
console.log(`         over 20s: ${t.filter((x) => x > 20).length} | over 30s: ${t.filter((x) => x > 30).length}`);
console.log(`\nIMAGES   turns with images : ${withImgs.length} / ${rows.length}`);
console.log(`         image-scored turns: ${scored.length} (Goa + Kolkata only)`);
console.log(`         property LEAKS    : ${leaks.length}`);
console.log(`         expected but none : ${noImg.length}`);
console.log(`         wrong category    : ${wrongCat.length}`);
const byReason = {};
for (const r of rows) byReason[r.reason || "-"] = (byReason[r.reason || "-"] || 0) + 1;
console.log(`         selection reason  : ${JSON.stringify(byReason)}`);
console.log(`\nLENGTH   median ${sent.map((r) => r.chars).sort((a, b) => a - b)[Math.floor(sent.length / 2)]}ch | max ${Math.max(...sent.map((r) => r.chars))}ch`);
if (leaks.length) { console.log(`\nLEAKS:`); for (const l of leaks) console.log(`  #${l.n} [${l.scope}] ${l.q}\n      ${l.problems.join(" | ")}`); }
if (noImg.length) { console.log(`\nMISSING IMAGES:`); for (const m of noImg) console.log(`  #${m.n} [${m.scope}] ${m.q}`); }
if (wrongCat.length) { console.log(`\nWRONG CATEGORY:`); for (const m of wrongCat) console.log(`  #${m.n} [${m.scope}] ${m.q} -> ${JSON.stringify(m.images)}`); }
console.log(`\nfull transcript -> ${path.join(SHOTS, "results.json")}`);
await sleep(10000);
await browser.close();
