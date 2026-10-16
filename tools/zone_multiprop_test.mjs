/**
 * The four multi-property enhancements, end to end.
 *
 *   WIDGET_KEY=wk_kOKo8Vwt1rGBE7ZCSLhYaFeC node tools/zone_multiprop_test.mjs
 *
 * A  reply covering 2 hotels shows ONE exterior each, not keyword-matched pool shots
 * B  buttons appear on that mid-chat reply, not only on the welcome
 * C  multi-property welcome ends on the "Still deciding?" chooser
 * D  tapping a hotel answers with rooms/dining/facilities, not the city again
 *
 * Image facts are read from the WEBSOCKET FRAMES, never from the carousel: the
 * DOM only renders the visible slide, and scraping it has twice reported counts
 * and captions that were not what the backend actually sent.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WK = process.env.WIDGET_KEY ? `&embed=1&widget_key=${process.env.WIDGET_KEY}` : "";
const OUT = path.join(process.cwd(), "tools", "_multiprop");
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CITY = process.env.CITY || "Kolkata";
const results = [];
const rec = (name, pass, detail) => {
  results.push({ name, pass, detail });
  console.log(`[${pass ? "PASS" : "FAIL"}] ${name}`);
  if (detail) console.log(`        ${String(detail).replace(/\n/g, "\n        ").slice(0, 700)}`);
};

const browser = await chromium.launch({ headless: false, slowMo: 40,
  args: ["--window-size=1500,980", "--window-position=0,0"] });
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();

// backend truth, straight off the wire
const frames = [];
const sent = [];
page.on("websocket", (ws) => {
  ws.on("framereceived", (d) => {
    try { frames.push(JSON.parse(typeof d.payload === "string" ? d.payload : d.payload.toString())); }
    catch { /* non-JSON keepalive */ }
  });
  // what the WIDGET sends: the only way to see which source_type a click produced
  ws.on("framesent", (d) => {
    try { sent.push(JSON.parse(typeof d.payload === "string" ? d.payload : d.payload.toString())); }
    catch { /* non-JSON keepalive */ }
  });
});
const lastMeta = () => {
  for (let i = frames.length - 1; i >= 0; i--) {
    const m = frames[i]?.metadata || frames[i]?.data?.metadata;
    if (m && (m.images || m.property_choices || m.smart_image_selection_reason)) return m;
  }
  return null;
};

await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=mp_${Date.now()}${WK}`,
  { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () => page.$$eval(".space-y-2 > div.justify-start",
  (e) => e.map((x) => (x.textContent || "").trim())).catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
async function wait(ms = 240000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) { if (await typing() > 0) break; await sleep(500); }
  while (Date.now() < end) { if (await typing() === 0) { await sleep(2500); return true; } await sleep(800); }
  return false;
}
const clean = (t) => String(t || "")
  .replace(/Loading image\.\.\.[\s\S]*$/, "")
  .replace(/Prev[\s\S]*?\d+\s*\/\s*\d+\s*\|\s*\w+\s*Next/g, "")
  .replace(/(Book on website|Get help from our team|Show less|Read more)/g, "").trim();

// intro form
const nameBox = page.locator('input[placeholder="Your full name"]').first();
await nameBox.waitFor({ state: "visible", timeout: 90000 });
await nameBox.fill("Sana Shaaista");
await page.locator('input[placeholder="Phone number"]').first().fill("9845061230");
await page.locator('input[type="email"]').first().fill("sana@example.com");
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
await wait(); await sleep(2500);

// ---- C: the welcome ends on the chooser -------------------------------------
const welcome = clean((await bubbles()).pop());
fs.writeFileSync(path.join(OUT, "welcome.txt"), welcome, "utf-8");
rec(`C  ${CITY} welcome ends on the chooser`,
  /still deciding\?/i.test(welcome) && /tap a name below/i.test(welcome),
  welcome.slice(-320));
rec("C  chooser replaced the old bland ending",
  !/which one would you like to explore\?/i.test(welcome),
  /which one would you like to explore\?/i.test(welcome) ? "old ending still present" : "old ending gone");

// ---- B(welcome): buttons -----------------------------------------------------
let choices = await page.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /^zone/i.test(t) && t.length < 70));
rec("B  welcome renders one button per hotel", choices.length >= 2, choices.join("  |  "));

// ---- D: tapping a hotel ------------------------------------------------------
const picked = choices[1] || choices[0];
// Click the REAL choice button. Matching on text alone is not enough: the image
// carousel prints the same hotel name as its caption, so an exact-text search
// can land on the caption element and the message goes out as typed_input.
const clickInfo = await page.evaluate((l) => {
  const all = [...document.querySelectorAll("button")].filter(
    (x) => (x.textContent || "").trim() === l,
  );
  // the choice button is the pill: full-width-ish, rounded, inside the bubble
  const target = all.find((x) => /rounded-full/.test(x.className || "")) || all[0];
  if (target) target.click();
  return { matches: all.length, cls: String(target?.className || "").slice(0, 90) };
}, picked);
console.log(`        (clicked "${picked}" — ${clickInfo.matches} elements matched that text)`);
await wait(); await sleep(2500);
// NOT a pass/fail. The widget passes "property_choice" on this click, but the WS
// transport rewrites ui_source_type to "typed_input" before the backend sees it
// — a known quirk, already documented above _INTRO_PHRASE_RE in chat.py, and the
// reason the curated pick is matched on the property NAME rather than on the
// click type. Printed so a future reader knows this is expected, not a fault.
const pickFrame = [...sent].reverse().find((f) => String(f?.message || "").trim() === picked);
console.log(`        (transport note: ui_source_type arrived as `
  + `"${pickFrame?.metadata?.ui_source_type}" — WS rewrites it; matching is by name)`);
const afterPick = clean((await bubbles()).pop());
fs.writeFileSync(path.join(OUT, "after_pick.txt"), `PICKED: ${picked}\n\n${afterPick}`, "utf-8");

// positive evidence: it must talk about the HOTEL
const hasRooms = /\b(deluxe|luxe|luxury|suite|presidential|connect room|premium room|zone room)\b/i.test(afterPick);
const hasDining = /\b(bazaar|flurys|italia|tapas|z-?bar|cafe c|co-?bar|orange zone|restaurant|dining|breakfast)\b/i.test(afterPick);
const hasFacil = /\b(pool|spa|gym|fitness|funzone|banquet|business centre|business center|meeting)\b/i.test(afterPick);
// and NOT re-describe the city it just described
const cityBlurb = /\b(located in|in the heart of|vibrant heart|close to (?:cultural|landmarks)|near cultural landmarks)\b/i.test(afterPick);
// Facilities is NOT required: Zone Connect Coimbatore has none in the KB, so its
// curated message deliberately omits the line rather than padding it. Demanding
// all three sections reported that correct behaviour as a failure.
rec(`D  pick answers with rooms + dining (facilities present: ${hasFacil})`,
  hasRooms && hasDining, afterPick.slice(0, 620));
rec("D  pick does NOT re-describe the city", !cityBlurb,
  cityBlurb ? "still opens with a location blurb" : "no location blurb");
// served from the DB, so it must be the curated copy verbatim and FAST
const pickMeta = lastMeta() || {};
rec("D  served from curated copy, not the model",
  String(pickMeta.response_source || "") === "location_intro"
  && String(pickMeta.location_id || "").startsWith("prop::"),
  `response_source=${pickMeta.response_source} location_id=${pickMeta.location_id}`);
rec("D  pick still shows the hotel's gallery",
  Array.isArray(pickMeta.images) && pickMeta.images.length >= 2,
  `${(pickMeta.images || []).length} images`);

// ---- A + B mid-chat: the hotel-shift question --------------------------------
const Q = "do u have some family frindly hotel in goa";
frames.length = 0;
const comp = page.locator('textarea[placeholder="Type a message..."]').first();
await comp.fill(Q); await comp.press("Enter");
await wait(); await sleep(3000);
const shiftReply = clean((await bubbles()).pop());
const meta = lastMeta() || {};
const imgs = Array.isArray(meta.images) ? meta.images : [];
const props = new Set(imgs.map((i) => String(i.property_name || "")));
const cats = imgs.map((i) => String(i.category || i.title || ""));
fs.writeFileSync(path.join(OUT, "shift.json"),
  JSON.stringify({ q: Q, reply: shiftReply, meta }, null, 1), "utf-8");

rec("A  multi-property reply uses the one-each rule",
  meta.smart_image_selection_reason === "multi_property_one_each",
  `reason=${meta.smart_image_selection_reason}  images=${imgs.length}`);
rec("A  one picture per hotel, not two of the same",
  imgs.length >= 2 && props.size >= 2,
  `${imgs.length} images across ${props.size} properties: ${[...props].join(" | ")}  cats=${cats.join(",")}`);
rec("A  no keyword-matched pool photos standing in for a hotel",
  !cats.some((c) => /pool/i.test(c)), `categories/titles: ${cats.join(", ")}`);

const midChoices = Array.isArray(meta.property_choices) ? meta.property_choices : [];
rec("B  mid-chat reply carries choice buttons", midChoices.length >= 2, midChoices.join("  |  "));
const domBtns = await page.evaluate(() => [...document.querySelectorAll("button")]
  .map((b) => (b.textContent || "").trim()).filter((t) => /^zone/i.test(t) && t.length < 70));
rec("B  those buttons actually render", domBtns.length >= 2, domBtns.join("  |  "));

// ---- D on the MID-CHAT buttons ------------------------------------------------
// The welcome buttons are not the only way in. When a guest asks about a city
// with two hotels mid-conversation, buttons appear on THAT reply too, and
// tapping one must serve the same curated message — not fall back to the model
// because it arrived by a different route.
if (midChoices.length >= 2) {
  frames.length = 0;
  const midPick = midChoices[0];
  await page.evaluate((l) => {
    const all = [...document.querySelectorAll("button")].filter(
      (x) => (x.textContent || "").trim() === l,
    );
    const t = all.find((x) => /rounded-full/.test(x.className || "")) || all[0];
    if (t) t.click();
  }, midPick);
  await wait(); await sleep(2500);
  const midReply = clean((await bubbles()).pop());
  const midMeta = lastMeta() || {};
  fs.writeFileSync(path.join(OUT, "after_midchat_pick.txt"),
    `PICKED (mid-chat): ${midPick}\n\n${midReply}`, "utf-8");
  rec("D  mid-chat button ALSO serves the curated message",
    String(midMeta.response_source || "") === "location_intro"
    && String(midMeta.location_id || "").startsWith("prop::"),
    `picked=${midPick}  response_source=${midMeta.response_source} location_id=${midMeta.location_id}`);
  rec("D  mid-chat pick has the same four sections",
    /🛏️|Rooms —/.test(midReply) && /🍽️|Eat & drink/.test(midReply)
    && /⭐|Offers/.test(midReply),
    midReply.slice(0, 420));
} else {
  rec("D  mid-chat button ALSO serves the curated message", false,
    "no mid-chat buttons appeared — could not test this path");
}

await page.screenshot({ path: path.join(OUT, "end.png"), fullPage: false }).catch(() => {});
console.log("\n" + "=".repeat(90));
const pass = results.filter((r) => r.pass).length;
console.log(`${pass}/${results.length} checks passed`);
for (const r of results.filter((x) => !x.pass)) console.log(`  FAILED: ${r.name}`);
fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify(results, null, 1), "utf-8");
await sleep(4000);
await browser.close();
