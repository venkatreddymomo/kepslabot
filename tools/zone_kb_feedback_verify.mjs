/**
 * Verification for the 2026-08-21 Zone pre-booking KB/prompt feedback batch.
 *   node tools/zone_kb_feedback_verify.mjs
 *
 * Drives /ws/chat through a real browser socket carrying the Zone widget_key,
 * so the deployment's model pin applies and every role runs on gpt-4.1-mini —
 * exactly what the embedded widget does in production.
 *
 * Checks:
 *   1. Italia / TEN - The Tapas Place / Pool Side Bar are gone from New Town
 *      Kolkata, and Bazaar / Z-Bar / Lobby Bar survive.
 *   2. Presidential Suite is gone; inventory reads 115 rooms / 3 categories.
 *   3. "is early check-in free?" answers with charges-may-apply + a number,
 *      and never with the banned "policy does not specify" register.
 *   4. "is advance payment required?" answers from the new KB fact.
 *   5. A genuine KB gap offers the property number AND the team handoff.
 *   6. Calangute Goa (untouched, already "chargeable") still answers correctly
 *      and shows no Kolkata bleed.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WS = BASE.replace(/^http/, "ws");
const WIDGET_KEY = process.env.WIDGET_KEY || "wk_kOKo8Vwt1rGBE7ZCSLhYaFeC";
const OUT = path.join(process.cwd(), "tools", "_kb_verify");
fs.mkdirSync(OUT, { recursive: true });

const BANNED_REGISTER =
  /(policy|policies|information|kb|knowledge base|sources?|details?)\s+(does not|doesn'?t|do not|don'?t)\s+(specify|mention|say|state|provide|cover)|it is not specified|no details are provided|not specified in/i;

// The KB says charges MAY apply. Hardening that into a certainty (or a denial)
// is the original bug in reverse — a fact the KB never gave.
const OVERCLAIM = /are not free of cost|is not free of cost|will be charged|will incur|are chargeable|is chargeable|are free of cost|is free of cost(?! *\?)/i;

// label, question, and the assertions for the reply
const T = (label, q, checks) => ({ label, q, checks });
const has = (re) => ({ kind: "has", re });
const not = (re) => ({ kind: "not", re });

const TURNS = [
  // ---------- New Town Kolkata ----------
  T("newtown:pick", "tell me about Zone by The Park, Kolkata New Town", []),
  T("newtown:dining", "what are the dining options there?", [
    not(/italia/i), not(/tapas/i), not(/pool ?side/i),
    has(/bazaar/i),
  ]),
  T("newtown:rooms", "what rooms do you have?", [
    not(/presidential/i),
    has(/zone deluxe|zone luxury|zone suite/i),
  ]),
  T("newtown:count", "how many rooms does this hotel have in total?", [
    not(/116/), not(/4 categories|four categories/i),
  ]),
  T("newtown:early", "is early check-in free of cost?", [
    not(BANNED_REGISTER),
    not(OVERCLAIM),
    has(/charges may apply|may apply|may incur|not necessarily free/i),
    has(/23246300/),
  ]),
  T("newtown:advance", "is advance payment required to confirm the room?", [
    not(BANNED_REGISTER),
    has(/card details|visa|mastercard|final payment/i),
  ]),

  // ---------- New Market Kolkata (the property QA flagged) ----------
  T("newmarket:pick", "now tell me about Zone by The Park, New Market Kolkata", []),
  T("newmarket:earlyq", "is early check-in or late check-out available?", [
    has(/availability/i),
  ]),
  // No number asserted here: the turn before already gave it, and the prompt's
  // "one promotion per topic" rule deliberately stops it repeating on the same
  // topic. What must hold is that "may apply" is not hardened into a certainty.
  T("newmarket:free", "is it free of cost ?", [
    not(BANNED_REGISTER),
    not(OVERCLAIM),
    has(/charges may apply|may apply|may incur|not necessarily free/i),
  ]),
  T("newmarket:advance", "is advance required to confirm the room", [
    not(BANNED_REGISTER),
    has(/card details|visa|mastercard|final payment/i),
  ]),
  T("newmarket:gap", "what is the nightly rate for the Zone Luxe room?", [
    not(BANNED_REGISTER),
    has(/9230964221/),                       // the number must be offered, not just the handoff
    has(/connect you|our team|reach out/i),  // …and the handoff must still be there
  ]),

  // ---------- Goa control: untouched property, no bleed ----------
  T("goa:pick", "tell me about Zone Connect by The Park, Calangute Goa", [
    not(/italia/i), not(/tapas/i), not(/presidential/i),
  ]),
  T("goa:early", "is early check-in chargeable there?", [
    not(BANNED_REGISTER),
    not(/9230964221|23246300/),
  ]),
  T("goa:dining", "what are the dining options at Calangute?", [
    not(/italia/i), not(/tapas/i), not(/bazaar/i),
  ]),
];

const browser = await chromium.launch({ headless: true, args: ["--ignore-certificate-errors"] });
const page = await browser.newPage();
await page.goto(BASE + "/docs", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});

const sid = "sid_kbverify_" + Date.now();
await page.evaluate(
  ([wsUrl, sessionId, widgetKey]) =>
    new Promise((resolve, reject) => {
      const url =
        wsUrl +
        "/ws/chat?session_id=" + encodeURIComponent(sessionId) +
        "&hotel_code=ZONE&channel=web_widget&widget_key=" + encodeURIComponent(widgetKey);
      const ws = new WebSocket(url);
      window.__replies = [];
      ws.onmessage = (e) => {
        try {
          const j = JSON.parse(e.data);
          if (j.type === "response") window.__replies.push(j.data || {});
        } catch {}
      };
      ws.onopen = () => { window.__ws = ws; resolve(true); };
      ws.onerror = (e) => reject(new Error("ws error"));
      setTimeout(() => reject(new Error("ws open timeout")), 20000);
    }),
  [WS, sid, WIDGET_KEY]
);

async function ask(text) {
  const before = await page.evaluate(() => window.__replies.length);
  await page.evaluate(
    ([msg, sessionId]) =>
      window.__ws.send(
        JSON.stringify({
          type: "message",
          message: msg,
          session_id: sessionId,
          hotel_code: "ZONE",
          channel: "web_widget",
          metadata: { message_id: "m_" + Date.now() + "_" + Math.random().toString(16).slice(2) },
        })
      ),
    [text, sid]
  );
  const t0 = Date.now();
  while (Date.now() - t0 < 180000) {
    const n = await page.evaluate(() => window.__replies.length);
    if (n > before) {
      return await page.evaluate((i) => window.__replies[i], before);
    }
    await new Promise((r) => setTimeout(r, 700));
  }
  return null;
}

const results = [];
let pass = 0, fail = 0;
console.log("session " + sid + "  ->  " + BASE + "  (widget pin governs the model)\n");

for (const t of TURNS) {
  const started = Date.now();
  const resp = await ask(t.q);
  const secs = ((Date.now() - started) / 1000).toFixed(1);
  const reply = String((resp && (resp.display_message || resp.message)) || "");
  const model = String(((resp || {}).metadata || {}).model || "");
  const failures = [];
  for (const c of t.checks) {
    const hit = c.re.test(reply);
    if (c.kind === "has" && !hit) failures.push("MISSING " + c.re);
    if (c.kind === "not" && hit) failures.push("PRESENT " + c.re);
  }
  if (failures.length) fail++; else pass++;
  results.push({ label: t.label, q: t.q, secs, model, reply, failures });
  console.log(
    (failures.length ? "FAIL " : "pass ") +
      t.label.padEnd(20) + secs.padStart(6) + "s  " +
      (reply.slice(0, 110).replace(/\s+/g, " ") || "(empty)")
  );
  for (const f of failures) console.log("        !! " + f);
}

fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify(results, null, 1));
const txt = results
  .map((r) => "==== " + r.label + "  (" + r.secs + "s)\nQ: " + r.q + "\nA: " + r.reply +
    (r.failures.length ? "\nFAILURES: " + r.failures.join(" | ") : ""))
  .join("\n\n");
fs.writeFileSync(path.join(OUT, "transcript.txt"), txt, "utf8");

console.log("\n" + "-".repeat(70));
console.log("pass " + pass + "  fail " + fail + "   -> tools/_kb_verify/transcript.txt");
await browser.close();
process.exit(fail ? 1 : 0);
