/**
 * A NORMAL guest conversation, printing each reply next to the images attached,
 * so image relevance can be judged by eye.
 *   node tools/zone_normal_chat_images.mjs                       (Calangute Goa)
 *   PROP="Zone by The Park, Jaipur" node tools/zone_normal_chat_images.mjs
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WS = BASE.replace(/^http/, "ws");
const WIDGET_KEY = process.env.WIDGET_KEY || "wk_kOKo8Vwt1rGBE7ZCSLhYaFeC";
const PROP = process.env.PROP || "Zone Connect by The Park, Calangute Goa";
const OUT = path.join(process.cwd(), "tools", "_image_check");
fs.mkdirSync(OUT, { recursive: true });

const TURNS = [
  PROP,
  "what rooms do you have?",
  "tell me more about the junior suite",
  "what are the dining options?",
  "do you have a swimming pool?",
  "is there a spa?",
  "do you have a gym?",
  "what time is check-in and check-out?",
  "how far is the airport?",
  "is parking available?",
  "what is your cancellation policy?",
];

const browser = await chromium.launch({ headless: true, args: ["--ignore-certificate-errors"] });
const page = await browser.newPage();
await page.goto(BASE + "/docs", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});

const sid = "sid_normal_" + Date.now();
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
console.log("PROPERTY: " + PROP + "\n");
for (const q of TURNS) {
  const r = await ask(q);
  const m = (r && r.metadata) || {};
  const imgs = Array.isArray(m.images) ? m.images : [];
  const reply = String((r && (r.display_message || r.message)) || "").replace(/\s+/g, " ");
  log.push({ q, reply, images: imgs.map((i) => ({ title: i.title, category: i.category })) });
  console.log("──────────────────────────────────────────────────────────────");
  console.log("GUEST: " + q);
  console.log("BOT  : " + reply.slice(0, 210) + (reply.length > 210 ? "…" : ""));
  if (!imgs.length) console.log("IMAGES: (none)");
  else for (const i of imgs) console.log("IMAGE : " + i.title + "  [" + i.category + "]");
}

fs.writeFileSync(path.join(OUT, "normal_chat.json"), JSON.stringify(log, null, 1));
console.log("\n-> tools/_image_check/normal_chat.json");
await browser.close();
