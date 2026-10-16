/**
 * Probe the deployed Zone embed so the chat test knows what it is driving.
 *   node d:/tmp/zone_probe.mjs
 */
import { chromium } from "playwright";

const URL = process.env.URL || "https://dev-reviews.kepsla.com:18080/zone/bot.html";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  headless: false,
  args: ["--window-size=1500,980", "--window-position=0,0", "--ignore-certificate-errors"],
});
const ctx = await browser.newContext({ viewport: null, ignoreHTTPSErrors: true });
const page = await ctx.newPage();
page.on("console", (m) => { if (m.type() === "error") console.log("  [console error]", m.text().slice(0, 140)); });

console.log("navigating to", URL);
await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 90000 });
await sleep(6000);

console.log("\ntitle:", await page.title());
console.log("frames:", page.frames().length);
for (const f of page.frames()) console.log("   -", f.url().slice(0, 130));

const shape = await page.evaluate(() => {
  const out = { iframes: [], launcher: [], inputs: [], scripts: [] };
  document.querySelectorAll("iframe").forEach((f) =>
    out.iframes.push({ id: f.id, cls: f.className, src: (f.src || "").slice(0, 120) }));
  document.querySelectorAll("button, [role=button], .kebo-launcher, [class*=launch]").forEach((b) => {
    const t = (b.textContent || "").trim().slice(0, 30);
    out.launcher.push({ tag: b.tagName, id: b.id, cls: String(b.className).slice(0, 60), text: t });
  });
  document.querySelectorAll("input, textarea, select").forEach((i) =>
    out.inputs.push({ tag: i.tagName, type: i.type, ph: i.placeholder || "", id: i.id }));
  document.querySelectorAll("script[src]").forEach((s) => out.scripts.push(s.src.slice(0, 120)));
  return out;
});
console.log("\n-- top document --");
console.log(JSON.stringify(shape, null, 1).slice(0, 2200));

// If the widget lives in an iframe, look inside it too.
for (const f of page.frames()) {
  if (f === page.mainFrame()) continue;
  try {
    const inner = await f.evaluate(() => ({
      inputs: [...document.querySelectorAll("input, textarea, select")].map((i) => ({
        tag: i.tagName, type: i.type, ph: i.placeholder || "",
      })),
      buttons: [...document.querySelectorAll("button")].map((b) => (b.textContent || "").trim().slice(0, 28)).slice(0, 18),
      bodyText: (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 320),
    }));
    console.log(`\n-- frame ${f.url().slice(0, 90)} --`);
    console.log(JSON.stringify(inner, null, 1).slice(0, 1800));
  } catch (e) {
    console.log("  (frame not readable:", String(e).slice(0, 80), ")");
  }
}

await page.screenshot({ path: "d:/tmp/zone_probe.png" }).catch(() => {});
console.log("\nscreenshot -> d:/tmp/zone_probe.png");
console.log("leaving the browser open for 20s so you can look…");
await sleep(20000);
await browser.close();
