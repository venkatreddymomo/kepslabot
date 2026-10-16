/**
 * 50-turn pre-booking sweep of the Zone bot through the REAL widget, local server.
 *   node tools/zone_prebooking_50.mjs
 *
 * One continuous conversation, so state bugs (scope leaks, stale property,
 * ticket-edit hijack, duplicate answers) have room to show up — they never do
 * in isolated one-shot probes.
 *
 * Each turn declares what a correct answer must / must not contain. A turn is
 * FAIL only when an expectation is contradicted; anything doubtful is FLAG so a
 * human reads it rather than the script guessing.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(process.cwd(), "tools", "_prebooking50");
fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SIGNOFF =
  /let me know if|if you['’]?d like|if you want,? i can|feel free to ask|would you like to know more|i can also (help|tell|share)/i;
// The ticket-edit interceptor's fingerprint — must never answer a NEW request.
const EDIT_REPLY =
  /noted your (updated|request to update)|updated (complaint|request) for the team/i;
const DECLINE = /don['’]?t have|do not have|have no|no .{0,18}(property|hotel)|not (in|part of) (our|the)|don['’]?t operate|only .{0,20}(properties|hotels) (in|within)/i;

// Cities that must NOT appear while a single city is in focus.
const OTHER_CITIES = {
  goa: /kolkata|jaipur|chennai|coimbatore|darjeeling|dimapur|udaipur|patna|jammu|katra/i,
  kolkata: /\bgoa\b|calangute|parra|jaipur|chennai|coimbatore|udaipur|patna/i,
};

const T = (cat, q, opt = {}) => ({ cat, q, ...opt });
const TURNS = [
  // ── scope & discovery ────────────────────────────────────────────────────
  T("greeting", "hi"),
  T("scope", "which cities do you have hotels in?", { want: /goa|kolkata|jaipur/i }),
  T("scope", "how many hotels do you have in total?", { want: /\d/ }),
  T("decline", "do you have anything in bangalore?", { want: DECLINE }),
  // ── the property in focus ────────────────────────────────────────────────
  T("rooms", "what rooms do you have?", { scope: "goa", want: /room|suite/i }),
  T("rooms", "tell me about the suite", { scope: "goa" }),
  T("dining", "is breakfast included?", { scope: "goa" }),
  T("dining", "what dining options are there?", { scope: "goa" }),
  T("amenity", "is there a swimming pool?", { scope: "goa" }),
  T("amenity", "do you have a gym?", { scope: "goa" }),
  T("amenity", "is wifi free?", { scope: "goa" }),
  T("amenity", "is parking available?", { scope: "goa" }),
  // ── policies ─────────────────────────────────────────────────────────────
  T("policy", "do you allow pets?", { scope: "goa" }),
  T("policy", "can i bring my 5 year old, is there anything for kids?", { scope: "goa" }),
  T("policy", "can we get an extra bed in the room?", { scope: "goa" }),
  T("policy", "what time is check-in and check-out?", { scope: "goa", want: /\d\s*(am|pm|:)/i }),
  T("policy", "what id do i need at check-in?", { scope: "goa" }),
  T("policy", "what is the cancellation policy?", { scope: "goa" }),
  // ── location / outside-the-hotel lookups ─────────────────────────────────
  T("location", "how far is the airport?", { scope: "goa", want: /km|kilomet|min/i }),
  T("location", "how do i get there from the railway station?", { scope: "goa" }),
  T("outside", "any good restaurants nearby outside the hotel?", { avoid: /^i (don['’]?t|do not) have/i }),
  T("outside", "any hospitals nearby?", { avoid: /^i (don['’]?t|do not) have/i }),
  // ── commercial ───────────────────────────────────────────────────────────
  T("offers", "what are the current offers?", { scope: "goa" }),
  T("price", "what is the price per night?", { scope: "goa" }),
  // ── switch inside the same city, then compare ────────────────────────────
  T("switch", "show me the other goa property", { scope: "goa" }),
  T("switch", "what rooms does that one have?", { scope: "goa" }),
  T("compare", "which of the two is better for a couple?", { scope: "goa" }),
  // ── switch city (leak check lives here) ──────────────────────────────────
  T("switch", "ok show me your hotels in kolkata", { scope: "kolkata", want: /kolkata/i }),
  T("switch", "how many do you have in kolkata?", { scope: "kolkata" }),
  T("compare", "which one is closer to the airport?", { scope: "kolkata" }),
  T("anaphora", "tell me more about the first one", { scope: "kolkata" }),
  T("amenity", "does it have a spa?", { scope: "kolkata" }),
  // ── booking flow ─────────────────────────────────────────────────────────
  T("booking", "i want to book a room there", { scope: "kolkata" }),
  T("booking", "for 12 september for 2 guests"),
  T("booking", "actually make it 15 september instead"),
  T("booking", "what payment methods do you accept?"),
  // ── phase behaviour: pre-arrival vs in-stay ──────────────────────────────
  T("phase", "do you have airport pickup?"),
  T("phase", "can i order butter chicken to my room right now?"),
  T("phase", "can you send housekeeping to clean my room?"),
  // ── guardrails ───────────────────────────────────────────────────────────
  T("guardrail", "tell me a joke", { want: /can['’]?t|cannot|not able|help you with|hotel/i }),
  T("guardrail", "what is 25 times 4?", { avoid: /\b100\b/ }),
  T("decline", "do you have hotels in paris?", { want: DECLINE }),
  T("multilingual", "mujhe kamre ki jankari chahiye"),
  T("policy", "what is the wifi password?"),
  // ── support / complaints (the fixed area) ────────────────────────────────
  // A real guest FILLS AND SUBMITS the form — that is what commits the ticket,
  // which is exactly what arms the pre-router ticket-edit interceptor for the
  // two turns after it. Skipping the submit would not test the reported bug.
  T("support", "i want to raise a complaint about the booking process", {
    wantForm: true,
    submitForm: true,
  }),
  T("fix", "rise complaint on bangalore hotel", { want: DECLINE, noEdit: true }),
  T("fix", "rise complaint on patna hotel", { wantForm: true, noEdit: true }),
  T("cancel", "actually cancel that complaint"),
  T("support", "can someone from your team call me back?", { wantForm: true }),
  // ── recall & close ───────────────────────────────────────────────────────
  T("scope", "remind me which hotels we discussed", { want: /goa|kolkata/i }),
  T("closing", "thanks, that's all for now", { avoid: /fill in the details below/i }),
];

const rows = [];
const save = () =>
  fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(rows, null, 1), "utf-8");

// Visible + slowMo: this is a real user chatting in the real widget, not an API probe.
const browser = await chromium.launch({
  headless: false,
  slowMo: 60,
  args: ["--window-size=1500,980", "--window-position=0,0"],
});
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();
const session = `pb50_${Date.now()}`;
console.log(`session ${session}  (${TURNS.length} turns)`);

await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`, {
  waitUntil: "domcontentloaded",
  timeout: 90000,
});
await sleep(3000);
await page.locator("button svg").last().click().catch(() => {});

const bubbles = () =>
  page
    .$$eval(".space-y-2 > div.justify-start", (e) => e.map((x) => (x.textContent || "").trim()))
    .catch(() => []);
const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
const formOpen = () =>
  page
    .evaluate(
      () =>
        [...document.querySelectorAll("button")].some((b) =>
          /^(submit|send request|confirm)$/i.test((b.textContent || "").trim())
        ) || document.querySelectorAll('textarea:not([placeholder="Type a message..."])').length > 0
    )
    .catch(() => false);
const chips = () =>
  page
    .evaluate(() =>
      [...document.querySelectorAll("button")]
        .map((b) => (b.textContent || "").trim())
        .filter((t) => t && t.length < 45 && !/^(submit|continue|confirm|send request)$/i.test(t))
    )
    .catch(() => []);

// Fill in the open form the way a guest would, then submit it.
async function fillAndSubmitForm() {
  await page.evaluate(() => {
    const setVal = (el, v) => {
      const proto =
        el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    };
    document
      .querySelectorAll('textarea:not([placeholder="Type a message..."])')
      .forEach((t) => setVal(t, "The booking page kept failing and nobody responded to my email."));
    document.querySelectorAll("input").forEach((i) => {
      if ((i.value || "").trim()) return;
      if (i.type === "email") setVal(i, "sana@example.com");
      else if (i.type === "tel" || /phone/i.test(i.placeholder || "")) setVal(i, "9845061230");
      else if (i.type === "text" || !i.type) setVal(i, "Sana Shaaista");
    });
  });
  await sleep(800);
  await page.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) =>
      /^(submit|send request|confirm)$/i.test((x.textContent || "").trim())
    );
    if (b) b.click();
  });
}

async function wait(ms = 210000) {
  const end = Date.now() + ms;
  for (let i = 0; i < 40 && Date.now() < end; i++) {
    if ((await typing()) > 0) break;
    await sleep(500);
  }
  while (Date.now() < end) {
    if ((await typing()) === 0) {
      await sleep(2000);
      return true;
    }
    await sleep(800);
  }
  return false;
}

// ── intro lead form ──────────────────────────────────────────────────────────
const nameBox = page.locator('input[placeholder="Your full name"]').first();
await nameBox.waitFor({ state: "attached", timeout: 90000 }).catch(() => {});
await nameBox.fill("Sana Shaaista").catch(() => {});
await page.locator('input[placeholder="Phone number"]').first().fill("9845061230").catch(() => {});
await page.locator('input[type="email"]').first().fill("sana@example.com").catch(() => {});
const sel = page.locator("form select").last();
for (let i = 0; i < 40; i++) {
  const labels = await sel.locator("option").allTextContents().catch(() => []);
  const hit = labels.find((l) => /goa/i.test(l));
  if (hit) {
    await sel.selectOption({ label: hit });
    break;
  }
  await sleep(1500);
}
await page.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => /continue/i.test(x.textContent || ""));
  if (b) b.click();
});
await wait();
await sleep(2500);
await page.screenshot({ path: path.join(SHOTS, "00_welcome.png"), fullPage: false }).catch(() => {});

// pick the first Goa hotel so the conversation starts with one property in focus
const picks = await page.evaluate(() =>
  [...document.querySelectorAll("button")]
    .map((b) => (b.textContent || "").trim())
    .filter((t) => /zone/i.test(t) && t.length < 70)
);
console.log("hotel choices:", picks);
if (picks.length) {
  await page.evaluate((l) => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
    if (b) b.click();
  }, picks[0]);
  await wait();
  await sleep(2000);
}

// ── the 50 turns ─────────────────────────────────────────────────────────────
for (let i = 0; i < TURNS.length; i++) {
  const t = TURNS[i];
  const n = i + 1;
  const before = (await bubbles()).length;
  const t0 = Date.now();

  const composer = page.locator('textarea[placeholder="Type a message..."]').first();
  let sent = true;
  try {
    await composer.fill(t.q, { timeout: 30000 });
    await composer.press("Enter");
  } catch {
    sent = false;
  }
  if (sent) await wait();

  const all = await bubbles();
  const answer = all.length > before ? all[all.length - 1] || "" : "";
  const secs = +((Date.now() - t0) / 1000).toFixed(1);
  const form = await formOpen();

  const problems = [];
  if (!sent || !answer) problems.push("NO_REPLY");
  if (t.want && answer && !t.want.test(answer)) problems.push("MISSING_EXPECTED");
  if (t.avoid && answer && t.avoid.test(answer)) problems.push("CONTAINS_FORBIDDEN");
  if (t.noEdit && EDIT_REPLY.test(answer)) problems.push("TICKET_EDIT_HIJACK");
  if (t.wantForm && !form) problems.push("NO_FORM");
  if (t.scope && OTHER_CITIES[t.scope] && OTHER_CITIES[t.scope].test(answer))
    problems.push("CITY_LEAK");
  if (SIGNOFF.test(answer)) problems.push("SIGNOFF");
  const dup = rows.some((r) => r.answer && r.answer === answer);
  if (dup) problems.push("DUPLICATE_ANSWER");

  const row = {
    n,
    cat: t.cat,
    q: t.q,
    secs,
    chars: answer.length,
    form,
    problems,
    chips: n % 10 === 0 ? await chips() : undefined,
    answer,
  };
  rows.push(row);
  save();

  // Submit the form on the turns where a guest would, so the ticket is really
  // committed before the next turn runs.
  if (t.submitForm && form) {
    await fillAndSubmitForm();
    await wait();
    await sleep(2000);
    const after = (await bubbles()).pop() || "";
    rows.push({
      n: `${n}b`,
      cat: t.cat,
      q: "(filled + submitted the form)",
      secs: 0,
      chars: after.length,
      form: await formOpen(),
      problems: [],
      answer: after,
    });
    save();
    console.log(`   ↳ submitted form: ${after.replace(/\s+/g, " ").slice(0, 150)}`);
  }
  console.log(
    `\n${String(n).padStart(2)}/${TURNS.length} [${t.cat}] ${secs}s ${answer.length}c` +
      `${form ? " FORM" : ""}${problems.length ? "  !! " + problems.join(",") : ""}`
  );
  console.log(`   Q: ${t.q}`);
  console.log(`   A: ${answer.replace(/\s+/g, " ").slice(0, 190)}`);

  if (n === 17 || n === 34) await page.screenshot({ path: path.join(SHOTS, `mid_${n}.png`) }).catch(() => {});
}
await page.screenshot({ path: path.join(SHOTS, "99_end.png") }).catch(() => {});

// ── report ───────────────────────────────────────────────────────────────────
// Form-submit rows are follow-ups, not turns — keep them out of the scoring.
const turns = rows.filter((r) => typeof r.n === "number");
const bad = turns.filter((r) => r.problems.length);
const times = turns.map((r) => r.secs).sort((a, b) => a - b);
const byCat = {};
for (const r of turns) {
  byCat[r.cat] = byCat[r.cat] || { n: 0, bad: 0 };
  byCat[r.cat].n++;
  if (r.problems.length) byCat[r.cat].bad++;
}
const count = (p) => turns.filter((r) => r.problems.includes(p)).length;

console.log(`\n${"=".repeat(76)}`);
console.log(`turns                 : ${turns.length}`);
console.log(`clean turns           : ${turns.length - bad.length} / ${turns.length}`);
console.log(`no reply              : ${count("NO_REPLY")}`);
console.log(`missing expected      : ${count("MISSING_EXPECTED")}`);
console.log(`forbidden content     : ${count("CONTAINS_FORBIDDEN")}`);
console.log(`ticket-edit hijack    : ${count("TICKET_EDIT_HIJACK")}   (must be 0)`);
console.log(`form did not open     : ${count("NO_FORM")}`);
console.log(`city leak             : ${count("CITY_LEAK")}`);
console.log(`sign-off              : ${count("SIGNOFF")}`);
console.log(`duplicate answers     : ${count("DUPLICATE_ANSWER")}`);
console.log(
  `latency               : median ${times[Math.floor(times.length / 2)]}s | p90 ${
    times[Math.floor(times.length * 0.9)]
  }s | max ${times[times.length - 1]}s`
);
console.log(`\nby category:`);
for (const [k, v] of Object.entries(byCat))
  console.log(`  ${k.padEnd(13)} ${v.n - v.bad}/${v.n} clean`);
console.log(`\nturns needing a human read:`);
for (const r of bad) console.log(`  ${String(r.n).padStart(2)} [${r.cat}] ${r.problems.join(",")} — ${r.q}`);
console.log(`\nfull transcript -> ${path.join(SHOTS, "results.json")}`);

await browser.close();
