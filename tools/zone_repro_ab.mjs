/**
 * Focused, REPEATABLE repro of the 7 issues found in the 50-turn sweep, so we
 * can tell which are real and which are run-to-run noise — and whether the
 * ticket-edit / support-gate changes caused any of them.
 *
 *   LABEL=with_fix REPS=3 node tools/zone_repro_ab.mjs
 *   LABEL=baseline REPS=3 node tools/zone_repro_ab.mjs      (after git stash)
 *
 * Writes tools/_repro_ab/<LABEL>.json — one record per repetition, so the same
 * scenario run N times can be compared directly.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const LABEL = process.env.LABEL || "run";
const REPS = parseInt(process.env.REPS || "3", 10);
const OUT = path.join(process.cwd(), "tools", "_repro_ab");
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Property/city fingerprints, used to tell "one property in focus" from a dump.
const CITY_WORDS = [
  "calangute", "parra", "new town", "new market", "kolkata", "jaipur", "chennai",
  "coimbatore", "darjeeling", "digha", "gopalpur", "jammu", "katra", "pathankot",
  "patna", "mussoorie", "saket", "udaipur", "jaisalmer", "gangtok", "port blair",
  "dimapur", "phalodi", "lansdowne",
];
const HINDI = /[ऀ-ॿ]|aap|kripya|hotel ke|kamre|jankari|humari|sampark|madad|bharen/i;

const results = [];
const save = () =>
  fs.writeFileSync(path.join(OUT, `${LABEL}.json`), JSON.stringify(results, null, 1), "utf-8");

function citiesIn(text) {
  const t = (text || "").toLowerCase();
  return CITY_WORDS.filter((c) => t.includes(c));
}

async function runOnce(rep) {
  const browser = await chromium.launch({
    headless: true,
    args: ["--window-size=1400,950"],
  });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 950 } });
  const page = await ctx.newPage();
  const session = `ab_${LABEL}_${rep}_${Date.now()}`;
  const rec = { rep, session, checks: {}, turns: [] };

  const bubbles = () =>
    page
      .$$eval(".space-y-2 > div.justify-start", (e) => e.map((x) => (x.textContent || "").trim()))
      .catch(() => []);
  const typing = () => page.$$eval(".animate-bounce", (e) => e.length).catch(() => 0);
  const submitBtn = () =>
    page
      .evaluate(() =>
        [...document.querySelectorAll("button")].some((b) =>
          /^(submit|send request|confirm)$/i.test((b.textContent || "").trim())
        )
      )
      .catch(() => false);

  async function wait(ms = 200000) {
    const end = Date.now() + ms;
    for (let i = 0; i < 40 && Date.now() < end; i++) {
      if ((await typing()) > 0) break;
      await sleep(500);
    }
    while (Date.now() < end) {
      if ((await typing()) === 0) {
        await sleep(1800);
        return true;
      }
      await sleep(800);
    }
    return false;
  }

  async function ask(q) {
    const before = (await bubbles()).length;
    let blocked = false;
    const composer = page.locator('textarea[placeholder="Type a message..."]').first();
    try {
      await composer.fill(q, { timeout: 20000 });
      await composer.press("Enter");
      await wait();
    } catch {
      blocked = true;
    }
    const all = await bubbles();
    const a = all.length > before ? all[all.length - 1] || "" : "";
    rec.turns.push({ q, blocked, answer: a });
    return { a, blocked };
  }

  await page.goto(`${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`, {
    waitUntil: "domcontentloaded",
    timeout: 90000,
  });
  await sleep(2500);
  await page.locator("button svg").last().click().catch(() => {});

  // intro form
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
  await sleep(2000);
  const picks = await page.evaluate(() =>
    [...document.querySelectorAll("button")]
      .map((b) => (b.textContent || "").trim())
      .filter((t) => /zone/i.test(t) && t.length < 70)
  );
  if (picks.length) {
    await page.evaluate((l) => {
      const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === l);
      if (b) b.click();
    }, picks[0]);
    await wait();
    await sleep(1800);
  }
  rec.picked = picks[0] || "";

  // ── A: does a generic "what rooms" after a broaden dump every property? ──
  await ask("which cities do you have hotels in?");
  await ask("how many hotels do you have in total?");
  await ask("do you have anything in bangalore?");
  const rooms = await ask("what rooms do you have?");
  rec.checks.A_rooms_property_count = citiesIn(rooms.a).length;
  rec.checks.A_cross_property_dump = citiesIn(rooms.a).length >= 3;

  // ── B/C: city switch, then a Hinglish turn ──────────────────────────────
  await ask("ok show me your hotels in kolkata");
  await ask("tell me more about the first one");
  const hinglish = await ask("mujhe kamre ki jankari chahiye");
  const hCities = citiesIn(hinglish.a);
  rec.checks.B_scope_left_kolkata =
    hCities.some((c) => ["calangute", "parra"].includes(c)) &&
    !hCities.some((c) => ["kolkata", "new town", "new market"].includes(c));
  rec.checks.C_hinglish_answered_in_hindi = HINDI.test(hinglish.a);

  // ── D: does the language stay switched on a later ENGLISH turn? ─────────
  const wifi = await ask("what is the wifi password?");
  rec.checks.D_english_turn_got_hindi = HINDI.test(wifi.a);

  // ── E: complaint form language + F: composer frozen by an open form ─────
  const comp = await ask("i want to raise a complaint about the booking process");
  rec.checks.E_complaint_reply_in_hindi = HINDI.test(comp.a);
  rec.checks.E_form_opened = await submitBtn();
  const probe = await ask("hello are you there");
  rec.checks.F_composer_blocked_by_open_form = probe.blocked;

  // ── G: recall names only what we actually discussed ─────────────────────
  if (await submitBtn()) {
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
        .forEach((t) => setVal(t, "The booking page kept failing."));
      document.querySelectorAll("input").forEach((i) => {
        if ((i.value || "").trim()) return;
        if (i.type === "email") setVal(i, "sana@example.com");
        else if (i.type === "tel" || /phone/i.test(i.placeholder || "")) setVal(i, "9845061230");
        else if (i.type === "text" || !i.type) setVal(i, "Sana Shaaista");
      });
    });
    await sleep(700);
    await page.evaluate(() => {
      const b = [...document.querySelectorAll("button")].find((x) =>
        /^(submit|send request|confirm)$/i.test((x.textContent || "").trim())
      );
      if (b) b.click();
    });
    await wait();
    await sleep(1500);
  }
  const recall = await ask("remind me which hotels we discussed");
  const rCities = citiesIn(recall.a);
  const DISCUSSED = ["calangute", "parra", "kolkata", "new town", "new market"];
  rec.checks.G_recall_named_undiscussed = rCities.filter((c) => !DISCUSSED.includes(c));
  rec.checks.G_recall_hallucinated = rec.checks.G_recall_named_undiscussed.length > 0;

  results.push(rec);
  save();
  console.log(`\n--- ${LABEL} rep ${rep} (${session}) ---`);
  for (const [k, v] of Object.entries(rec.checks)) console.log(`   ${k}: ${JSON.stringify(v)}`);
  await browser.close();
}

for (let r = 1; r <= REPS; r++) {
  try {
    await runOnce(r);
  } catch (e) {
    console.log(`rep ${r} crashed: ${e.message}`);
    results.push({ rep: r, crashed: String(e.message) });
    save();
  }
}

// ── summary ─────────────────────────────────────────────────────────────────
const keys = [
  "A_cross_property_dump",
  "B_scope_left_kolkata",
  "C_hinglish_answered_in_hindi",
  "D_english_turn_got_hindi",
  "E_complaint_reply_in_hindi",
  "E_form_opened",
  "F_composer_blocked_by_open_form",
  "G_recall_hallucinated",
];
console.log(`\n${"=".repeat(64)}\n${LABEL}: ${results.filter((r) => r.checks).length} reps`);
for (const k of keys) {
  const vals = results.filter((r) => r.checks).map((r) => r.checks[k]);
  const hits = vals.filter(Boolean).length;
  console.log(`  ${k.padEnd(36)} ${hits}/${vals.length}`);
}
console.log(`-> ${path.join(OUT, LABEL + ".json")}`);
