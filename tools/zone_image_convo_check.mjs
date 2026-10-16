/**
 * Drives a real multi-turn conversation per Zone property over the widget WS and
 * records which images the bot attaches at each turn — so image behaviour can be
 * reviewed across a whole conversation, not just the property pick.
 *
 *   node tools/zone_image_convo_check.mjs
 *
 * Extends tools/zone_image_gallery_check.mjs (which covers 6 properties / 3 turns).
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WS = BASE.replace(/^http/, "ws");
const WIDGET_KEY = process.env.WIDGET_KEY || "wk_kOKo8Vwt1rGBE7ZCSLhYaFeC";
const CONCURRENCY = Number(process.env.CONCURRENCY || 3);
const OUT = path.join(process.cwd(), "tools", "_image_check");
fs.mkdirSync(OUT, { recursive: true });

const PROPERTIES = JSON.parse(fs.readFileSync(path.join(OUT, "_properties.json"), "utf8"));
const TURNS = [
  { key: "pick",    text: (n) => n },
  { key: "rooms",   text: () => "what rooms do you have?" },
  { key: "dining",  text: () => "what are the dining options?" },
  { key: "pool",    text: () => "do you have a pool?" },
  { key: "outside", text: () => "what does the hotel look like from outside?" },
];

const imgs = (resp) => {
  const m = (resp && (resp.metadata || {})) || {};
  const list = m.images || (resp || {}).images || [];
  return Array.isArray(list) ? list : [];
};
const reason = (resp) => ((resp && resp.metadata) || {}).smart_image_selection_reason || "";

async function makeWorker(browser) {
  const page = await browser.newPage();
  await page.goto(BASE + "/docs", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
  return page;
}

async function openSocket(page, sid) {
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
}

async function ask(page, sid, text) {
  const before = await page.evaluate(() => window.__replies.length);
  await page.evaluate(
    ([msg, sessionId]) => window.__ws.send(JSON.stringify({
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

const browser = await chromium.launch({ headless: true, args: ["--ignore-certificate-errors"] });
const queue = [...PROPERTIES];
const results = [];
let done = 0;

async function runOne(page, prop) {
  const sid = "sid_convo_" + Date.now() + "_" + Math.random().toString(16).slice(2);
  await openSocket(page, sid);
  const row = { property: prop.name, property_id: prop.id, turns: {} };
  for (const t of TURNS) {
    const resp = await ask(page, sid, t.text(prop.name));
    const list = imgs(resp);
    row.turns[t.key] = {
      count: list.length,
      reason: reason(resp),
      images: list.map((i) => ({ title: i.title || i.name || "?", category: i.category || "", id: i.id })),
      reply: String((resp && (resp.message || resp.display_message)) || "").slice(0, 400),
    };
  }
  results.push(row);
  done++;
  const s = TURNS.map((t) => `${t.key}:${row.turns[t.key].count}`).join(" ");
  console.log(`[${String(done).padStart(2)}/${PROPERTIES.length}] ${prop.name.padEnd(50)} ${s}`);
}

await Promise.all(
  Array.from({ length: CONCURRENCY }, async () => {
    const page = await makeWorker(browser);
    while (queue.length) {
      const prop = queue.shift();
      try { await runOne(page, prop); }
      catch (e) { console.log("FAILED " + prop.name + " :: " + e.message); results.push({ property: prop.name, error: e.message }); }
    }
  })
);

results.sort((a, b) => (a.property_id || 0) - (b.property_id || 0));
fs.writeFileSync(path.join(OUT, "convo.json"), JSON.stringify(results, null, 1));
console.log("\n-> tools/_image_check/convo.json");
await browser.close();
