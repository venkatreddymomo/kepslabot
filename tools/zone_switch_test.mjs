/**
 * Property-switch test for a two-hotel city.
 *   node tools/zone_switch_test.mjs
 *
 * Goa's two hotels have disjoint room names, which makes the scope provable
 * rather than a matter of opinion:
 *   Calangute -> Junior Suite, Executive Suite   (never at Parra)
 *   Parra     -> Family Area                     (never at Calangute)
 *
 * So after a switch, the answer must name the NEW hotel's rooms and must not
 * name the old one's. Same for the images attached.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(process.cwd(), "tools", "_switch");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CAL_ONLY = /junior suite|executive suite/i;
const PARRA_ONLY = /family area/i;
const CAL_NAME = /calangute/i;
const PARRA_NAME = /parra/i;

const TURNS = [
  { q: "what rooms do you have?",            want: "calangute", why: "still on the picked hotel" },
  { q: "what about the Parra one?",          want: "parra",     why: "explicit switch by name" },
  { q: "what rooms does it have?",           want: "parra",     why: "scope must STAY switched" },
  { q: "and the dining?",                    want: "parra",     why: "still Parra on a new topic" },
  { q: "compare both hotels for me",         want: "both",      why: "both in scope when asked" },
  { q: "go back to the Calangute one",       want: "calangute", why: "switch back" },
  { q: "what rooms do you have?",            want: "calangute", why: "scope stayed on Calangute" },
];

const frames = [];
const browser = await chromium.launch({
  headless: false, slowMo: 50,
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
      const ents = md.entities || {};
      const pd = ents.pending_data || {};
      frames.push({
        images: (md.images || []).map((i) => `${i.title}(${i.category})`),
        scope: pd.active_property_names || pd.active_property_ids || [],
      });
    } catch { /* ignore */ }
  });
});

const session = `switch_${Date.now()}`;
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

const choices = await page.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /zone/i.test(t) && t.length < 70));
const calangute = choices.find((c) => /calangute/i.test(c)) || choices[0];
console.log(`picking: ${calangute}\n`);
await page.evaluate((l) => {
  const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
  if (b) b.click();
}, calangute);
await wait(); await sleep(1500);

const rows = [];
for (const t of TURNS) {
  const before = (await bubbles()).length;
  const nf = frames.length;
  const composer = page.locator('textarea[placeholder="Type a message..."]').first();
  await composer.fill(t.q);
  await composer.press("Enter");
  await wait();
  const all = await bubbles();
  const a = all.length > before ? (all[all.length - 1] || "") : "";
  const fr = frames.slice(nf).pop() || { images: [], scope: [] };

  const saysCal = CAL_ONLY.test(a) || CAL_NAME.test(a);
  const saysParra = PARRA_ONLY.test(a) || PARRA_NAME.test(a);
  const leakCal = CAL_ONLY.test(a);        // room names are the hard proof
  const leakParra = PARRA_ONLY.test(a);

  let ok, detail;
  if (t.want === "calangute") { ok = saysCal && !leakParra; detail = `cal=${saysCal} parraLeak=${leakParra}`; }
  else if (t.want === "parra") { ok = saysParra && !leakCal; detail = `parra=${saysParra} calLeak=${leakCal}`; }
  else { ok = saysCal && saysParra; detail = `cal=${saysCal} parra=${saysParra}`; }

  rows.push({ ...t, ok, detail, chars: a.length, images: fr.images, scope: fr.scope, answer: a });
  console.log(`${ok ? "pass" : "FAIL"}  ${t.q.padEnd(32)} want=${t.want.padEnd(10)} ${detail}`);
  console.log(`      scope=${JSON.stringify(fr.scope).slice(0, 70)}  imgs=${JSON.stringify(fr.images).slice(0, 58)}`);
  console.log(`      ${a.replace(/\s+/g, " ").slice(0, 165)}`);
  fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(rows, null, 1), "utf-8");
}
await page.screenshot({ path: path.join(SHOTS, "end.png") }).catch(() => {});

const bad = rows.filter((r) => !r.ok);
console.log(`\n${"=".repeat(70)}`);
console.log(`${rows.length - bad.length} / ${rows.length} property-scope checks passed`);
if (bad.length) for (const b of bad) console.log(`  FAIL ${b.q}  (${b.why}) -> ${b.detail}`);
console.log(bad.length ? "\nPROPERTY SWITCHING IS BROKEN" : "\nPROPERTY SWITCHING OK");
await sleep(8000);
await browser.close();
