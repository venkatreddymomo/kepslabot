/**
 * Counts the images the bot actually attaches when a guest PICKS a property,
 * and on a rooms / dining question, for the 4 properties whose images were
 * re-uploaded on 2026-08-26 vs. control properties that still have categories.
 *
 *   node tools/zone_image_gallery_check.mjs
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const WS = BASE.replace(/^http/, "ws");
const WIDGET_KEY = process.env.WIDGET_KEY || "wk_kOKo8Vwt1rGBE7ZCSLhYaFeC";
const OUT = path.join(process.cwd(), "tools", "_image_check");
fs.mkdirSync(OUT, { recursive: true });

const CASES = [
  { group: "FOCUS  ", name: "Zone Connect by The Park, Calangute Goa" },
  { group: "FOCUS  ", name: "Zone Connect by The Park Parra Goa" },
  { group: "FOCUS  ", name: "Zone by The Park, Kolkata (New Town)" },
  { group: "FOCUS  ", name: "Zone by The Park, New Market Kolkata" },
  { group: "CONTROL", name: "Zone by The Park, Jaipur" },
  { group: "CONTROL", name: "Zone by The Park, Digha" },
];

const browser = await chromium.launch({ headless: true, args: ["--ignore-certificate-errors"] });
const page = await browser.newPage();
await page.goto(BASE + "/docs", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});

async function openSocket(sid) {
  await page.evaluate(
    ([wsUrl, sessionId, widgetKey]) =>
      new Promise((resolve, reject) => {
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
  while (Date.now() - t0 < 180000) {
    const n = await page.evaluate(() => window.__replies.length);
    if (n > before) return await page.evaluate((i) => window.__replies[i], before);
    await new Promise((r) => setTimeout(r, 700));
  }
  return null;
}

const imgs = (resp) => {
  const m = (resp && (resp.metadata || {})) || {};
  const list = m.images || (resp || {}).images || [];
  return Array.isArray(list) ? list : [];
};

const results = [];
console.log("images attached per turn  ->  " + BASE + "\n");
console.log("group   property                                  pick  rooms  dining");

for (const c of CASES) {
  const sid = "sid_img_" + Date.now() + "_" + Math.random().toString(16).slice(2);
  await openSocket(sid);
  const pick = await ask(sid, c.name);
  const rooms = await ask(sid, "what rooms do you have?");
  const dining = await ask(sid, "what are the dining options?");
  const row = {
    group: c.group, property: c.name,
    pick: imgs(pick).length, rooms: imgs(rooms).length, dining: imgs(dining).length,
    pickTitles: imgs(pick).map((i) => i.title || i.name || "?"),
    roomTitles: imgs(rooms).map((i) => i.title || i.name || "?"),
    diningTitles: imgs(dining).map((i) => i.title || i.name || "?"),
  };
  results.push(row);
  console.log(
    c.group + " " + c.name.padEnd(41) +
    String(row.pick).padStart(4) + String(row.rooms).padStart(7) + String(row.dining).padStart(7)
  );
}

fs.writeFileSync(path.join(OUT, "gallery.json"), JSON.stringify(results, null, 1));
console.log("\n-> tools/_image_check/gallery.json");
await browser.close();
