/**
 * Verification for the 2026-08-26 Zone estate-wide fact batch:
 *   1. Wheelchair access  — all 24 properties
 *   2. Welcome drink on arrival — all 24
 *   3. Baby food (chef's choice) — all 24
 *
 *   node tools/zone_three_facts_verify.mjs
 *
 * Drives /ws/chat through a real browser socket carrying the Zone widget_key,
 * so the deployment's model pin applies — exactly what the embedded widget does.
 *
 * The properties chosen are the hard cases:
 *   - Lansdowne    had NO wheelchair line anywhere (the original "bot not sure" case)
 *   - Darjeeling   said "Accessible / wheelchair rooms: No"  -> flipped to Yes
 *   - Parra Goa    said "Accessible / wheelchair rooms: no"  -> flipped to Yes
 *   - Chennai ORR  had no rooms_suites line (only a policies_rules one)
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WS = BASE.replace(/^http/, "ws");
const WIDGET_KEY = process.env.WIDGET_KEY || "wk_kOKo8Vwt1rGBE7ZCSLhYaFeC";
const OUT = path.join(process.cwd(), "tools", "_three_facts_verify");
fs.mkdirSync(OUT, { recursive: true });

// The register the bot must never fall back into for a fact that is now in the KB.
const UNSURE =
  /(policy|policies|information|kb|knowledge base|sources?|details?)\s+(does not|doesn'?t|do not|don'?t)\s+(specify|mention|say|state|provide|cover)|it is not specified|no details are provided|not specified in|i'?m not sure|i am not sure|unable to confirm|cannot confirm/i;

const YES_ACCESS = /wheelchair|accessible/i;
const NO_ACCESS = /not wheelchair|no wheelchair|not accessible|do not have (an )?accessible|don'?t have (an )?accessible|unfortunately[^.]*accessib/i;

const T = (label, q, checks) => ({ label, q, checks });
const has = (re) => ({ kind: "has", re });
const not = (re) => ({ kind: "not", re });

const TURNS = [
  // ---------- Lansdowne: had no wheelchair fact at all ----------
  T("lansdowne:pick", "tell me about Zone Connect by The Park, Lansdowne", []),
  T("lansdowne:wheelchair", "is the hotel wheelchair accessible?", [
    not(UNSURE), not(NO_ACCESS), has(YES_ACCESS),
  ]),
  T("lansdowne:welcome", "do you provide a welcome drink on arrival?", [
    not(UNSURE), has(/welcome drink/i),
  ]),
  T("lansdowne:baby", "is baby food available in the hotel?", [
    not(UNSURE), has(/baby food/i), has(/chef/i),
  ]),

  // ---------- Darjeeling: was an explicit "No" ----------
  T("darjeeling:pick", "now tell me about Zone by The Park, Darjeeling", []),
  T("darjeeling:wheelchair", "do you have wheelchair accessible rooms there?", [
    not(UNSURE), not(NO_ACCESS), has(YES_ACCESS),
  ]),
  T("darjeeling:baby", "we are travelling with an infant, do you have baby food?", [
    not(UNSURE), has(/baby food/i),
  ]),

  // ---------- Parra Goa: was an explicit "no" ----------
  T("parra:pick", "tell me about Zone Connect by The Park Parra Goa", []),
  T("parra:wheelchair", "is wheelchair access available at this hotel?", [
    not(UNSURE), not(NO_ACCESS), has(YES_ACCESS),
  ]),
  T("parra:welcome", "do guests get a welcome drink?", [
    not(UNSURE), has(/welcome drink/i),
  ]),

  // ---------- Chennai ORR: had no rooms_suites accessibility line ----------
  T("chennai:pick", "tell me about Zone by The Park, Chennai ORR", []),
  T("chennai:wheelchair", "my father uses a wheelchair, can he access the hotel?", [
    not(UNSURE), not(NO_ACCESS), has(YES_ACCESS),
  ]),
  T("chennai:baby", "do you serve baby food?", [
    not(UNSURE), has(/baby food/i),
  ]),
];

const browser = await chromium.launch({ headless: true, args: ["--ignore-certificate-errors"] });
const page = await browser.newPage();
await page.goto(BASE + "/docs", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});

const sid = "sid_3facts_" + Date.now();
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
      ws.onerror = () => reject(new Error("ws error"));
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
    if (n > before) return await page.evaluate((i) => window.__replies[i], before);
    await new Promise((r) => setTimeout(r, 700));
  }
  return null;
}

const results = [];
let pass = 0, fail = 0;
console.log("session " + sid + "  ->  " + BASE + "\n");

for (const t of TURNS) {
  const started = Date.now();
  const resp = await ask(t.q);
  const secs = ((Date.now() - started) / 1000).toFixed(1);
  const reply = String((resp && (resp.display_message || resp.message)) || "");
  const failures = [];
  for (const c of t.checks) {
    const hit = c.re.test(reply);
    if (c.kind === "has" && !hit) failures.push("MISSING " + c.re);
    if (c.kind === "not" && hit) failures.push("PRESENT " + c.re);
  }
  if (t.checks.length) { if (failures.length) fail++; else pass++; }
  results.push({ label: t.label, q: t.q, secs, reply, failures });
  console.log(
    (t.checks.length === 0 ? "---- " : failures.length ? "FAIL " : "pass ") +
      t.label.padEnd(24) + secs.padStart(6) + "s  " +
      (reply.slice(0, 100).replace(/\s+/g, " ") || "(empty)")
  );
  for (const f of failures) console.log("        !! " + f);
}

fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify(results, null, 1));
fs.writeFileSync(
  path.join(OUT, "transcript.txt"),
  results.map((r) => "==== " + r.label + "  (" + r.secs + "s)\nQ: " + r.q + "\nA: " + r.reply +
    (r.failures.length ? "\nFAILURES: " + r.failures.join(" | ") : "")).join("\n\n"),
  "utf8"
);

console.log("\n" + "-".repeat(70));
console.log("pass " + pass + "  fail " + fail + "   -> tools/_three_facts_verify/transcript.txt");
await browser.close();
process.exit(fail ? 1 : 0);
