/**
 * Probe WHICH image-selection path runs for a given turn, and why it returned
 * nothing. Dumps the smart_image_* metadata the pipeline records:
 *   smart_image_books_seen      book labels available this turn (consumed)
 *   smart_image_books           labels the winning branch reported
 *   smart_image_selection_reason which branch produced the images
 *   smart_image_topic_error     exception inside a branch, if any
 *
 *   node tools/zone_image_path_probe.mjs
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
  "Zone Connect by The Park, Calangute Goa",
  "What are the poolside dining options?",
  "do you have a swimming pool?",
  "show me the pool",
];

const browser = await chromium.launch({ headless: true, args: ["--ignore-certificate-errors"] });
const page = await browser.newPage();
await page.goto(BASE + "/docs", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});

const sid = "sid_probe_" + Date.now();
await page.evaluate(
  ([wsUrl, sessionId, widgetKey]) =>
    new Promise((resolve, reject) => {
      const url = wsUrl + "/ws/chat?session_id=" + encodeURIComponent(sessionId) +
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
      window.__ws.send(JSON.stringify({
        type: "message", message: msg, session_id: sessionId,
        hotel_code: "ZONE", channel: "web_widget",
        metadata: { message_id: "m_" + Date.now() + "_" + Math.random().toString(16).slice(2) },
      })),
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

const out = [];
for (const q of TURNS) {
  const r = await ask(q);
  const m = (r && r.metadata) || {};
  const imgs = Array.isArray(m.images) ? m.images : [];
  const row = {
    q,
    reply: String((r && (r.display_message || r.message)) || "").slice(0, 220),
    images: imgs.map((i) => `${i.title} [${i.category}]`),
    books_seen: m.smart_image_books_seen,
    books: m.smart_image_books,
    reason: m.smart_image_selection_reason,
    error: m.smart_image_topic_error,
    tool_used: m.smart_image_tool_used,
  };
  out.push(row);
  console.log("\n================================================================");
  console.log("Q: " + q);
  console.log("A: " + row.reply.replace(/\s+/g, " ").slice(0, 160));
  console.log("  images        :", row.images.length ? row.images.join(", ") : "(none)");
  console.log("  books_seen    :", JSON.stringify(row.books_seen));
  console.log("  books         :", JSON.stringify(row.books));
  console.log("  reason        :", row.reason);
  console.log("  tool_used     :", row.tool_used);
  if (row.error) console.log("  ERROR         :", row.error);
}

fs.writeFileSync(path.join(OUT, "path_probe.json"), JSON.stringify(out, null, 1));
console.log("\n-> tools/_image_check/path_probe.json");
await browser.close();
