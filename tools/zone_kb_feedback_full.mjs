/**
 * Full regression + latency run for the 2026-08-21 Zone pre-booking feedback batch.
 *   node tools/zone_kb_feedback_full.mjs
 *
 * Drives /ws/chat through real browser sockets carrying the Zone widget_key, so
 * the deployment pin applies and every role runs on gpt-4.1-mini — same as the
 * embed in production.
 *
 * Runs FOUR separate sessions so property scope starts clean per group, which is
 * also what a real guest does:
 *   A  New Town Kolkata — every angle on the four removed items
 *   B  New Market Kolkata — the two answers QA flagged, plus a true KB gap
 *   C  estate-wide — early check-in + advance payment across 6 more properties,
 *      switching property mid-conversation to prove no cross-property bleed
 *   D  controls — untouched properties must be unchanged
 *
 * Wall time per turn is recorded here; the per-LLM-call breakdown comes from
 * tools/zone_latency_report.mjs reading new_bot_log_llm for these sessions.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WS = BASE.replace(/^http/, "ws");
const WIDGET_KEY = process.env.WIDGET_KEY || "wk_kOKo8Vwt1rGBE7ZCSLhYaFeC";
const OUT = path.join(process.cwd(), "tools", "_kb_verify");
fs.mkdirSync(OUT, { recursive: true });

// ── shared assertions ───────────────────────────────────────────────────────
const BANNED_REGISTER =
  /(polic(y|ies)|information|kb|knowledge base|sources?|details?)\s+(does not|doesn'?t|do not|don'?t)\s+(specify|mention|say|state|provide|cover)|it is not specified|no details are provided|not specified in/i;
// The KB says "charges may apply", so "not free of cost" is a correct reading
// and the preferred direct answer. What is still wrong is claiming it IS free.
const CLAIMS_FREE = /\bis free of charge|are free of charge|is complimentary|are complimentary|yes,? (it|they) (is|are) free/i;
// The four things removed from New Town Kolkata. Word boundaries matter here:
// "Vitalia Spa" (the brand at every Zone property) contains "italia", and
// Dimapur has a legitimate "Presidential Villa" — neither is a regression.
const REMOVED = /\bitalia\b|\btapas\b|pool ?side bar|presidential suite/i;
// For CROSS-PROPERTY bleed checks, drop "poolside bar" — Calangute's own bar
// (Plunge) is legitimately described as one. Only the named Kolkata items count.
const KOLKATA_ONLY = /\bitalia\b|\btapas\b|presidential suite|bazaar/i;

const has = (re, why) => ({ kind: "has", re, why });
const not = (re, why) => ({ kind: "not", re, why });
const T = (label, q, checks = []) => ({ label, q, checks });

// ── the four sessions ───────────────────────────────────────────────────────
const GROUPS = [
  {
    name: "A · New Town Kolkata — removals",
    turns: [
      T("A1 city intro (curated, no LLM)", "I'm exploring Kolkata — tell me about the hotels there.", [
        not(REMOVED, "removed items must not appear in the curated city intro"),
        not(/four room types|four places to eat/i, "derived counts had to change too"),
      ]),
      // The chip label the widget sends after the city intro's "Tap a name below".
      T("A2 property pick", "Zone by The Park, Kolkata (New Town)", [
        not(REMOVED, "removed items must not appear when the property is picked"),
      ]),
      T("A3 dining list", "what are the dining options?", [
        not(REMOVED, "deleted outlets must not be listed"),
        has(/bazaar/i, "Bazaar survives"),
        has(/z-?bar/i, "Z-Bar survives"),
      ]),
      // Must stay inside ONE sentence: "…named Italia at X. Our main option is
      // Bazaar, which offers…" is a correct denial, not a description of Italia.
      T("A4 direct probe: Italia", "tell me about Italia, your Italian restaurant", [
        not(/\bitalia\b[^.!?\n]{0,60}\b(is|serves|offers|features|located)\b/i,
            "must not describe Italia as existing"),
        has(/\bno\b|do(n'|n)?o?t have|does not have/i, "must explicitly deny it"),
      ]),
      T("A5 direct probe: tapas", "do you have a Spanish tapas place here?", [
        not(/\bten\b.{0,30}tapas|tapas place is|yes,? we (have|do)/i, "must not confirm the tapas outlet"),
      ]),
      T("A6 direct probe: poolside bar", "is there a poolside bar?", [
        not(/pool ?side bar (is|offers|serves)/i, "must not confirm the poolside bar"),
      ]),
      T("A7 rooms list", "what rooms do you have?", [
        not(/presidential/i, "Presidential Suite must be gone"),
        has(/zone deluxe|zone luxury|zone suite/i, "the three real rooms survive"),
      ]),
      T("A8 direct probe: Presidential", "tell me about the Presidential Suite", [
        not(/presidential suite (is|offers|features|has|measures)/i, "must not describe it as existing"),
      ]),
      T("A9 room count", "how many rooms does this hotel have in total?", [
        not(/\b116\b/, "old total"),
        not(/4 categories|four categories/i, "old category count"),
        has(/\b115\b/, "new total"),
      ]),
      T("A10 overview", "what makes this hotel special?", [
        not(REMOVED, "removed items must not resurface in the overview"),
      ]),
    ],
  },
  {
    name: "B · New Market Kolkata — the flagged answers",
    turns: [
      T("B1 scope", "tell me about Zone by The Park, New Market Kolkata"),
      T("B2 early available?", "is early check-in or late check-out available?", [
        not(BANNED_REGISTER, "banned register"),
        has(/availability/i, "availability stated"),
      ]),
      T("B3 is it free? (QA case 1)", "is it free of cost ?", [
        not(BANNED_REGISTER, "banned register"),
        not(CLAIMS_FREE, "must not claim it is free"),
        has(/charges may apply|may apply|may incur|not free/i, "the charge answer"),
      ]),
      T("B4 advance? (QA case 2)", "is advance required to confirm the room", [
        not(BANNED_REGISTER, "banned register"),
        has(/card details|visa|mastercard/i, "the new advance fact"),
        has(/final payment|confirm the booking/i, "the follow-up half of the fact"),
      ]),
      T("B5 true KB gap", "what is the nightly rate for the Zone Luxe room?", [
        not(BANNED_REGISTER, "banned register"),
        has(/9230964221/, "the property number must be offered"),
        has(/connect you|our team|reach out/i, "…and the handoff kept"),
      ]),
    ],
  },
  {
    name: "C · estate-wide — 6 more properties, switching mid-chat",
    turns: [
      T("C1 Jaipur early", "for Zone by The Park, Jaipur — is early check-in chargeable?", [
        not(BANNED_REGISTER, "banned register"),
        has(/0141-?4231000|charges may apply|may apply|may incur/i, "Jaipur's own line"),
        not(/9230964221|23246300/, "no Kolkata number bleed"),
      ]),
      T("C2 Jaipur advance", "and is advance payment needed to confirm?", [
        has(/card details|visa|mastercard/i, "advance fact reached Jaipur"),
      ]),
      T("C3 Chennai ORR early", "what about Zone by The Park, Chennai ORR — early check-in?", [
        not(BANNED_REGISTER, "banned register"),
        has(/11 ?am|charges may apply|may apply|may incur/i, "the 11 AM fact was preserved"),
      ]),
      T("C4 Parra Goa early", "and Zone Connect by The Park Parra Goa — is early check-in free?", [
        not(BANNED_REGISTER, "banned register"),
        not(CLAIMS_FREE, "must not claim it is free"),
        has(/18001239663|charges may apply|may apply|may incur|not free/i, "toll-free fallback for a property with no direct line"),
      ]),
      T("C5 Port Blair advance", "for Zone Connect by The Park, Port Blair — is advance payment required?", [
        not(BANNED_REGISTER, "banned register"),
        has(/100%|advance/i, "Port Blair keeps its stricter 100% rule"),
      ]),
      T("C6 Dimapur early", "what about Zone Niathu by The Park, Dimapur — early check-in charges?", [
        not(BANNED_REGISTER, "banned register"),
        has(/84159 ?21118|charges may apply|may apply|may incur/i, "Dimapur's own line"),
      ]),
    ],
  },
  {
    name: "D · controls — untouched properties must be unchanged",
    turns: [
      T("D1 Calangute early (already 'chargeable')", "at Zone Connect by The Park, Calangute Goa is early check-in chargeable?", [
        not(BANNED_REGISTER, "banned register"),
        has(/chargeable/i, "its stronger existing wording was preserved, not overwritten"),
      ]),
      T("D2 Calangute advance", "is advance payment required there?", [
        has(/card details|visa|mastercard/i, "advance fact reached Goa too"),
      ]),
      T("D3 Calangute dining", "what are the dining options there?", [
        not(KOLKATA_ONLY, "no Kolkata outlet bleed"),
        has(/cafe c/i, "its own outlets intact"),
      ]),
      T("D4 New Market rooms", "and at New Market Kolkata, what rooms are there?", [
        not(/presidential/i, "no Presidential anywhere"),
        has(/zone deluxe|zone luxe/i, "its own rooms intact"),
      ]),
    ],
  },
];

// ── driver ──────────────────────────────────────────────────────────────────
const browser = await chromium.launch({ headless: true, args: ["--ignore-certificate-errors"] });
const page = await browser.newPage();
await page.goto(BASE + "/docs", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});

async function openSocket(sid) {
  await page.evaluate(
    ([wsUrl, sessionId, widgetKey]) =>
      new Promise((resolve, reject) => {
        try { if (window.__ws) window.__ws.close(); } catch {}
        const url =
          wsUrl + "/ws/chat?session_id=" + encodeURIComponent(sessionId) +
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
        ws.onerror = () => reject(new Error("ws error"));
        setTimeout(() => reject(new Error("ws open timeout")), 20000);
      }),
    [WS, sid, WIDGET_KEY]
  );
}

async function ask(sid, text) {
  const before = await page.evaluate(() => window.__replies.length);
  await page.evaluate(
    ([msg, sessionId]) =>
      window.__ws.send(JSON.stringify({
        type: "message", message: msg, session_id: sessionId,
        hotel_code: "ZONE", channel: "web_widget",
        metadata: { message_id: "m_" + Date.now() + "_" + Math.random().toString(16).slice(2) },
      })),
    [text, sid]
  );
  const t0 = Date.now();
  while (Date.now() - t0 < 200000) {
    const n = await page.evaluate(() => window.__replies.length);
    if (n > before) return { resp: await page.evaluate((i) => window.__replies[i], before), ms: Date.now() - t0 };
    await new Promise((r) => setTimeout(r, 500));
  }
  return { resp: null, ms: Date.now() - t0 };
}

const results = [];
const sessions = [];
let pass = 0, fail = 0;
console.log("target " + BASE + "   model: gpt-4.1-mini (deployment pin)\n");

for (const g of GROUPS) {
  const sid = "sid_kbfull_" + g.name[0] + "_" + Date.now();
  sessions.push(sid);
  await openSocket(sid);
  console.log("\n" + "=".repeat(96));
  console.log(g.name + "    [" + sid + "]");
  console.log("=".repeat(96));
  for (const t of g.turns) {
    const { resp, ms } = await ask(sid, t.q);
    const reply = String((resp && (resp.display_message || resp.message)) || "");
    const failures = [];
    for (const c of t.checks) {
      const hit = c.re.test(reply);
      if (c.kind === "has" && !hit) failures.push("MISSING (" + c.why + ") " + c.re);
      if (c.kind === "not" && hit) failures.push("PRESENT (" + c.why + ") " + c.re);
    }
    if (t.checks.length) { failures.length ? fail++ : pass++; }
    results.push({ group: g.name, sid, label: t.label, q: t.q, ms, reply, failures });
    console.log(
      (t.checks.length === 0 ? "  -  " : failures.length ? "FAIL " : "pass ") +
      t.label.padEnd(34) + (ms / 1000).toFixed(1).padStart(6) + "s  " +
      (reply.slice(0, 78).replace(/\s+/g, " ") || "(empty)")
    );
    for (const f of failures) console.log("        !! " + f);
  }
}

// ── latency summary (wall time per turn, as the guest experiences it) ───────
const times = results.map((r) => r.ms).sort((a, b) => a - b);
const pct = (p) => times[Math.min(times.length - 1, Math.floor((times.length - 1) * p))] / 1000;
const mean = times.reduce((a, b) => a + b, 0) / times.length / 1000;
const slowest = [...results].sort((a, b) => b.ms - a.ms).slice(0, 5);

console.log("\n" + "=".repeat(96));
console.log("RESULT   pass " + pass + "   fail " + fail + "   (" + results.length + " turns)");
console.log("LATENCY  end-to-end wall time, guest-perceived");
console.log("  turns " + times.length +
  "   min " + (times[0] / 1000).toFixed(1) + "s" +
  "   p50 " + pct(0.5).toFixed(1) + "s" +
  "   p90 " + pct(0.9).toFixed(1) + "s" +
  "   p95 " + pct(0.95).toFixed(1) + "s" +
  "   max " + (times[times.length - 1] / 1000).toFixed(1) + "s" +
  "   mean " + mean.toFixed(1) + "s");
console.log("  slowest:");
for (const s of slowest) console.log("    " + (s.ms / 1000).toFixed(1).padStart(6) + "s  " + s.label);

fs.writeFileSync(path.join(OUT, "full_results.json"), JSON.stringify({ sessions, results }, null, 1));
fs.writeFileSync(
  path.join(OUT, "full_transcript.txt"),
  results.map((r) => "==== [" + r.group + "] " + r.label + "  (" + (r.ms / 1000).toFixed(1) + "s)\nQ: " + r.q +
    "\nA: " + r.reply + (r.failures.length ? "\nFAILURES: " + r.failures.join(" | ") : "")).join("\n\n"),
  "utf8"
);
console.log("\nsessions: " + sessions.join(" "));
console.log("-> tools/_kb_verify/full_transcript.txt");
await browser.close();
process.exit(fail ? 1 : 0);
