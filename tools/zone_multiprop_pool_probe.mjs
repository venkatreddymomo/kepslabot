/**
 * Reproduce the multi-property turn: a pool question that names BOTH Goa
 * hotels. Shows which branch fired and what images came back.
 *   node tools/zone_multiprop_pool_probe.mjs
 */
import { chromium } from "playwright";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WS = BASE.replace(/^http/, "ws");
const WIDGET_KEY = process.env.WIDGET_KEY || "wk_kOKo8Vwt1rGBE7ZCSLhYaFeC";

const TURNS = [
  "show me your hotels in Goa",
  "do both the Goa properties have swimming pools?",
  "tell me about the pools at both Goa hotels",
];

const browser = await chromium.launch({ headless: true, args: ["--ignore-certificate-errors"] });
const page = await browser.newPage();
await page.goto(BASE + "/docs", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});

const sid = "sid_mp_" + Date.now();
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

for (const q of TURNS) {
  const r = await ask(q);
  const m = (r && r.metadata) || {};
  const imgs = Array.isArray(m.images) ? m.images : [];
  console.log("\n================================================================");
  console.log("Q: " + q);
  console.log("A: " + String((r && (r.display_message || r.message)) || "").replace(/\s+/g, " ").slice(0, 200));
  console.log("  images (" + imgs.length + "): " +
    (imgs.map((i) => `${i.title} [${i.category}]`).join(", ") || "(none)"));
  console.log("  reason        : " + m.smart_image_selection_reason);
  console.log("  property_choices: " + JSON.stringify(m.property_choices));
}

await browser.close();
