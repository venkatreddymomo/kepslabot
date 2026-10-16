import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const base = process.env.BASE || "http://127.0.0.1:8013";
const runLabel = process.env.RUN_LABEL || "run";
const session = `language_widget_e2e_${Date.now()}`;
const outputDir = path.join(process.cwd(), "tools", "_language_widget_e2e", runLabel);
fs.mkdirSync(outputDir, { recursive: true });

const turns = process.env.TURNS
  ? JSON.parse(process.env.TURNS)
  : [
      "kya main jaldi check in kar sakta hoon?",
      "nenu hotel ki munduga vaste early check-in cheyyavachaa??",
      "nanu hotel-ge bega bandre early check-in madabahuda?",
    ];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 430, height: 900 } });
const page = await context.newPage();
const browserErrors = [];
const failedRequests = [];

page.on("console", (m) => { if (m.type() === "error") browserErrors.push(m.text()); });
page.on("pageerror", (e) => browserErrors.push(String(e)));
page.on("requestfailed", (r) => failedRequests.push(`${r.method()} ${r.url()}`));

const url = `${base}/chat?hotel_code=SAROVAR_PORTICO_SRIPERUMBUDUR&phase=pre_booking&session_id=${session}`;
async function waitForBotIdle(timeoutMs = 180000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if ((await page.locator(".animate-bounce").count()) === 0) return;
    await page.waitForTimeout(500);
  }
  throw new Error("Timed out waiting for the widget to finish typing");
}

await page.goto(url, { waitUntil: "domcontentloaded", timeout: 90000 });

const launcherIcons = page.locator("button svg");
const n = await launcherIcons.count();
if (n > 0) { await launcherIcons.nth(n - 1).click(); await page.waitForTimeout(2000); }

const nameInput = page.locator('input[placeholder="Your full name"]');
if ((await nameInput.count()) === 1 && await nameInput.isVisible()) {
  await nameInput.fill("Language Test Guest");
  const phoneInput = page.locator('input[placeholder="Phone number"]');
  if ((await phoneInput.count()) === 1) await phoneInput.fill("9000000001");
  const emailInput = page.locator('input[type="email"]');
  if ((await emailInput.count()) === 1) await emailInput.fill("language-test@example.com");
  const selects = page.locator("form select");
  const sc = await selects.count();
  for (let i = 0; i < sc; i += 1) {
    const sel = selects.nth(i);
    const opts = await sel.locator("option").allTextContents();
    const pref = opts.find((l) => /sriperumbudur|chennai/i.test(l));
    const fb = opts.find((l) => l.trim() && !/select|choose/i.test(l));
    const choice = pref || fb;
    if (choice) await sel.selectOption({ label: choice });
  }
  const cont = page.getByRole("button", { name: "Continue", exact: false });
  if ((await cont.count()) === 1) { await cont.click(); await page.waitForTimeout(1000); await waitForBotIdle(); }
}

const propBtn = page.getByRole("button").filter({ hasText: "Sarovar Portico Sriperumbudur" });
if ((await propBtn.count()) === 1 && await propBtn.isVisible()) {
  await propBtn.click(); await page.waitForTimeout(1000); await waitForBotIdle();
}

const composer = page.locator('textarea[placeholder="Type a message..."]');
await composer.waitFor({ state: "visible", timeout: 90000 });
const assistantBubbles = page.locator(".space-y-2 > div.justify-start");
const results = [];

for (let i = 0; i < turns.length; i += 1) {
  await waitForBotIdle();
  const before = await assistantBubbles.count();
  const started = Date.now();
  await composer.fill(turns[i]);
  await composer.press("Enter");
  let after = before;
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(500);
    after = await assistantBubbles.count();
    if (after > before && (await page.locator(".animate-bounce").count()) === 0) break;
  }
  if (after <= before) throw new Error(`Timed out on turn ${i + 1}`);
  await page.waitForTimeout(1200);
  const reply = (await assistantBubbles.nth(after - 1).innerText()).trim();
  results.push({ turn: i + 1, user: turns[i], reply, elapsedMs: Date.now() - started });
  await page.screenshot({ path: path.join(outputDir, `turn-${i + 1}.png`), fullPage: true });
}

const report = { runLabel, session, url, results, browserErrors, failedRequests };
fs.writeFileSync(path.join(outputDir, "report.json"), JSON.stringify(report, null, 2), "utf8");
console.log(`REPORT_${runLabel}=${JSON.stringify(report)}`);
await browser.close();
