/**
 * 20-turn guest conversation across Goa, Kolkata and Udaipur, printing every
 * reply next to the images attached so relevance can be judged.
 *
 *   node tools/zone_20turn_image_test.mjs
 *
 * `expect` is a coarse auto-check only:
 *   "visual" — a topic we hold photos of; no images is suspicious
 *   "none"   — policy/logistics; ANY image is suspicious
 *   "any"    — judgement call, printed but never flagged
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WS = BASE.replace(/^http/, "ws");
const WIDGET_KEY = process.env.WIDGET_KEY || "wk_kOKo8Vwt1rGBE7ZCSLhYaFeC";
const OUT = path.join(process.cwd(), "tools", "_image_check");
fs.mkdirSync(OUT, { recursive: true });

const TURNS = [
  // ---------- GOA: two properties, then one ----------
  ["show me your hotels in Goa", "visual"],
  ["do both the Goa properties have swimming pools?", "visual"],
  ["Zone Connect by The Park, Calangute Goa", "visual"],
  ["what rooms do you have?", "visual"],
  ["what are the dining options?", "visual"],
  ["is there a spa?", "any"],
  ["what is the check-in time?", "none"],
  // ---------- KOLKATA ----------
  ["what about your hotels in Kolkata?", "visual"],
  ["Zone by The Park, Kolkata (New Town)", "visual"],
  ["show me the rooms there", "visual"],
  ["do you have a gym?", "visual"],
  ["what restaurants do you have?", "visual"],
  ["is there a swimming pool?", "visual"],
  ["how far is the airport?", "none"],
  // ---------- UDAIPUR: a property we have not touched today ----------
  ["now tell me about Zone Connect by The Park Udaipur", "visual"],
  ["what rooms are available there?", "visual"],
  ["does it have a pool?", "visual"],
  ["what are the dining options at Udaipur?", "visual"],
  ["do you have a banquet hall for events?", "visual"],
  ["what is the cancellation policy?", "none"],
];

const browser = await chromium.launch({ headless: true, args: ["--ignore-certificate-errors"] });
const page = await browser.newPage();
await page.goto(BASE + "/docs", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});

const sid = "sid_20turn_" + Date.now();
await page.evaluate(
  ([wsUrl, sessionId, widgetKey]) =>
    new Promise((resolve, reject) => {
      const url = wsUrl + "/ws/chat?session_id=" + encodeURIComponent(sessionId) +
        "&hotel_code=ZONE&channel=web_widget&widget_key=" + encodeURIComponent(widgetKey);
      const ws = new WebSocket(url);
      window.__replies = [];
      ws.onmessage = (e) => {
        try { const j = JSON.parse(e.data); if (j.type === "response") window.__replies.push(j.data || {}); } catch {}
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
    ([msg, sessionId]) => window.__ws.send(JSON.stringify({
      type: "message", message: msg, session_id: sessionId,
      hotel_code: "ZONE", channel: "web_widget",
      metadata: { message_id: "m_" + Date.now() + "_" + Math.random().toString(16).slice(2) },
    })), [text, sid]);
  const t0 = Date.now();
  while (Date.now() - t0 < 180000) {
    const n = await page.evaluate(() => window.__replies.length);
    if (n > before) return await page.evaluate((i) => window.__replies[i], before);
    await new Promise((r) => setTimeout(r, 700));
  }
  return null;
}

const log = [];
let flags = 0;
for (let i = 0; i < TURNS.length; i++) {
  const [q, expect] = TURNS[i];
  const t0 = Date.now();
  const r = await ask(q);
  const secs = ((Date.now() - t0) / 1000).toFixed(1);
  const m = (r && r.metadata) || {};
  const imgs = Array.isArray(m.images) ? m.images : [];
  const reply = String((r && (r.display_message || r.message)) || "").replace(/\s+/g, " ");
  const denial = /\b(does not have|do not have|doesn'?t have|no (spa|gym|pool)|not available|unfortunately)\b/i.test(reply);

  let flag = "";
  if (expect === "none" && imgs.length) flag = "  <<< UNEXPECTED IMAGES";
  if (expect === "visual" && !imgs.length && !denial) flag = "  <<< NO IMAGES";
  if (flag) flags++;

  log.push({ n: i + 1, q, expect, secs, reply, denial, images: imgs.map((x) => `${x.title} [${x.category}]`), flag: flag.trim() });

  console.log("\n" + "─".repeat(78));
  console.log(`${String(i + 1).padStart(2)}. GUEST: ${q}   (${secs}s)${flag}`);
  console.log("    BOT  : " + reply.slice(0, 170) + (reply.length > 170 ? "…" : ""));
  if (!imgs.length) console.log("    IMAGES: (none)" + (denial ? "   [bot said the facility does not exist — correct]" : ""));
  else for (const x of imgs) console.log("    IMAGE : " + x.title + "  [" + x.category + "]");
}

fs.writeFileSync(path.join(OUT, "twenty_turn.json"), JSON.stringify(log, null, 1));
console.log("\n" + "=".repeat(78));
console.log(`20 turns complete — ${flags} flagged for review`);
console.log("-> tools/_image_check/twenty_turn.json");
await browser.close();
