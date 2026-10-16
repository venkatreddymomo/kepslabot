/**
 * Focused re-check of the three regressions reported after the first round:
 *
 *   A. new visitor  -> intro form ONLY (no greeting bubble)
 *      returning    -> greeting bubble "Hi <name>, welcome back..." + location
 *                      picker, and NO intro form
 *   B. chips wrap on a laptop even INSIDE the embed iframe (the case that was
 *      still scrolling), and still scroll on a phone
 *
 * B is tested through a real iframe with a laptop-sized `screen`, because that
 * is exactly what the previous detection got wrong.
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const HEADLESS = process.env.HEADLESS === "1";
const SHOTS = path.join(process.cwd(), "tools", "_verify_shots2");
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
const check = (n, p, d = "") => {
  results.push({ n, p, d });
  console.log(`  ${p ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const panelText = (scope) =>
  scope.locator("div.min-h-0.flex-1.overflow-y-auto").first()
    .evaluate((el) => el.textContent || "").catch(() => "");

async function openWidget(scope) {
  await scope.locator("button svg").last().click().catch(() => {});
}

// Click by textContent inside a given frame/page context.
async function clickText(ctxFrame, selector, source) {
  return await ctxFrame.evaluate(
    ([sel, src]) => {
      const hit = Array.from(document.querySelectorAll(sel)).find((e) =>
        new RegExp(src, "i").test(e.textContent || ""),
      );
      if (!hit) return false;
      hit.click();
      return true;
    },
    [selector, source],
  );
}

// The lead is "frozen" server-side; poll until it exists so we never test the
// returning-visitor path before the visitor actually became one.
async function waitForLead(page, sessionId, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const name = await page.evaluate(async (sid) => {
      try {
        const r = await fetch(`/api/chat/session/${encodeURIComponent(sid)}/profile`);
        if (!r.ok) return "";
        const d = await r.json();
        return String(d?.guest_name || "");
      } catch { return ""; }
    }, sessionId);
    if (name) return name;
    await sleep(1500);
  }
  return "";
}


// Fill + submit the intro form. The city <select> is populated by a slow
// (~10s) manifest call, so we WAIT for the option to exist — selecting too
// early silently failed validation and the lead was never created.
async function fillIntroForm(scope, frameForClick, { name, phone, email, city }) {
  const nameField = scope.locator('input[placeholder="Your full name"]').first();
  await nameField.waitFor({ state: "attached", timeout: 60000 });
  await nameField.fill(name);
  await scope.locator('input[placeholder="Phone number"]').first().fill(phone);
  await scope.locator('input[type="email"]').first().fill(email);

  const select = scope.locator("form select").last();
  const deadline = Date.now() + 60000;
  let picked = false;
  while (Date.now() < deadline) {
    const labels = await select.locator("option").allTextContents().catch(() => []);
    if (labels.some((l) => new RegExp(city, "i").test(l))) {
      const match = labels.find((l) => new RegExp(city, "i").test(l));
      await select.selectOption({ label: match });
      picked = true;
      break;
    }
    await sleep(1500);
  }
  if (!picked) return false;
  return await clickText(frameForClick, "button", "Continue");
}

// ---------------------------------------------------------------- A ---------
async function testGreeting(browser) {
  console.log("\n=== A. new visitor vs returning visitor ===");
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const session = `greet_${Date.now()}`;
  const url = `${BASE}/chat?hotel_code=ZONE&phase=pre_booking&session_id=${session}`;

  await page.goto(url, { waitUntil: "domcontentloaded" });
  await sleep(3000);
  await openWidget(page);
  const form = page.locator('input[placeholder="Your full name"]').first();
  await form.waitFor({ state: "attached", timeout: 60000 }).catch(() => {});

  let text = await panelText(page);
  check("new visitor: intro form shown", (await form.count()) > 0);
  check("new visitor: no greeting bubble", !/welcome back|choose the location you/i.test(text));
  await page.screenshot({ path: path.join(SHOTS, "A1_new_visitor.png") });

  // Become a RETURNING visitor: submit the lead, then reload the same session.
  const clicked = await fillIntroForm(page, page, {
    name: "Sana Shaaista", phone: "9845061230",
    email: "sana@example.com", city: "Kolkata",
  });
  check("setup: intro form submitted", clicked);
  const leadName = await waitForLead(page, session);
  check("setup: lead frozen server-side", Boolean(leadName), leadName || "no lead");

  // A plain reload restores the SAME conversation (history intact) — that is
  // not the case being fixed. The reported scenario is a NEW conversation id
  // for a visitor we already know: the dustbin / "Clear chat" control.
  await sleep(9000);
  await page.locator('button[title="Clear chat"]').first().click().catch(() => {});
  await sleep(9000);

  text = await panelText(page);
  const formBack = await page.locator('input[placeholder="Your full name"]').count();
  check("returning: greeted by name + welcome back", /welcome back/i.test(text),
    text.slice(0, 110).replace(/\s+/g, " "));
  check("returning: asked to choose location", /choose the location/i.test(text));
  check("returning: intro form NOT shown again", formBack === 0);
  await page.screenshot({ path: path.join(SHOTS, "A2_returning_visitor.png") });
  await ctx.close();
}

// ---------------------------------------------------------------- B ---------
async function testChips(browser, label, viewport, screen, expectScroll) {
  console.log(`\n=== B. chips in iframe — ${label} ===`);
  const ctx = await browser.newContext({ viewport, screen });
  const page = await ctx.newPage();
  const session = `chip_${label}_${Date.now()}`;
  const frameUrl = `${BASE}/chat?hotel_code=ZONE&phase=pre_booking&embed=1&session_id=${session}`;

  // Real iframe, exactly like the embedded widget on a customer site.
  await page.setContent(
    `<html><body style="margin:0">
       <iframe id="w" src="${frameUrl}" style="width:380px;height:620px;border:0"></iframe>
     </body></html>`,
    { waitUntil: "domcontentloaded" },
  );
  const frame = page.frameLocator("#w");
  await sleep(4000);

  let child = null;
  const fdl = Date.now() + 30000;
  while (Date.now() < fdl && !child) {
    child = page.frames().find((f) => f.url().includes("embed=1"));
    if (!child) await sleep(1000);
  }
  const submitted = await fillIntroForm(frame, child, {
    name: "Chip Tester", phone: "9845061230",
    email: "chip@example.com", city: "Kolkata",
  });
  check(`${label}: intro form submitted in iframe`, submitted);

  // Wait for suggestion chips to arrive.
  const chipRow = frame.locator("div.shrink-0.border-t:not(:has(textarea)):has(button)").first();
  await chipRow.waitFor({ state: "attached", timeout: 90000 }).catch(() => {});
  await sleep(2500);

  if ((await chipRow.count()) === 0) {
    check(`${label}: chip row present`, false, "no chips rendered");
    await ctx.close();
    return;
  }
  const info = await chipRow.evaluate((el) => ({
    cls: el.className,
    scrollW: el.scrollWidth,
    clientW: el.clientWidth,
  }));
  const wraps = /flex-wrap/.test(info.cls);
  const scrolls = /overflow-x-auto/.test(info.cls);
  const hidden = info.scrollW > info.clientW + 4;

  if (expectScroll) {
    check(`${label}: keeps horizontal scroller`, scrolls, info.cls.slice(0, 60));
  } else {
    check(`${label}: chips WRAP, nothing hidden`, wraps && !hidden,
      `wrap=${wraps} scroll=${scrolls} scrollW=${info.scrollW} clientW=${info.clientW}`);
  }
  await page.screenshot({ path: path.join(SHOTS, `B_${label}.png`) });
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch({ headless: HEADLESS, slowMo: HEADLESS ? 0 : 250 });
  await testGreeting(browser);
  // Laptop: small iframe but a laptop-sized SCREEN. This is the exact case that
  // was still scrolling, because innerWidth/pointer both said "phone".
  await testChips(browser, "laptop-embed", { width: 1440, height: 900 },
    { width: 1920, height: 1080 }, false);
  // Phone: small iframe AND a phone-sized screen -> scroller is correct here.
  await testChips(browser, "phone-embed", { width: 390, height: 844 },
    { width: 390, height: 844 }, true);
  await browser.close();

  const failed = results.filter((r) => !r.p);
  console.log(`\n======== ${results.length - failed.length}/${results.length} passed ========`);
  failed.forEach((f) => console.log(`  FAILED: ${f.n} ${f.d}`));
  console.log(`screenshots: ${SHOTS}`);
  process.exit(failed.length ? 1 : 0);
})();
