"""Submit a real shuttle form through the widget and print EVERY bubble after it.

The guest reported that submitting a form gets only "In the meantime, want to
explore more about the hotel..." with nothing about their booking. The server
builds the reply as TWO bubbles - an acknowledgement and a closer - so the
question is whether the acknowledgement is missing, empty, or simply above the
fold. Only a real submission answers that, so this fills the form and reads back
what actually lands in the panel.
"""
import io
import os
import re
import sys

from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"


def bubbles(page):
    return [t.strip() for t in
            page.locator("div.flex.justify-start").all_inner_texts() if t.strip()]


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=os.environ.get("UI_HEADLESS", "1") == "1")
    page = b.new_page(viewport={"width": 1500, "height": 1000})
    page.set_default_timeout(60000)
    page.goto("%s/chat?hotel_code=%s&phase=during_stay" % (BASE, HOTEL),
              wait_until="domcontentloaded")
    for _ in range(50):
        page.wait_for_timeout(1000)
        if "Mapped test profile active for during_stay" in page.inner_text("body"):
            break
    page.click("button:has-text('New Session')")
    page.wait_for_timeout(2500)
    try:
        page.click("button[aria-label='Open Chat']")
    except Exception:
        page.click("text='Need help? Chat with Kai'")
    page.wait_for_selector(TA, timeout=45000)
    page.wait_for_timeout(2000)
    sid = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
    print("session:", sid.group(1) if sid else "?")

    page.fill(TA, "i need an airport drop tomorrow")
    page.click(SEND)
    for _ in range(40):
        page.wait_for_timeout(2000)
        if page.locator("select").count() > 4 or page.locator('input[type="date"]').count():
            break
    if not page.locator('input[type="date"]').count():
        page.fill(TA, "yes please")
        page.click(SEND)
        for _ in range(40):
            page.wait_for_timeout(2000)
            if page.locator('input[type="date"]').count():
                break

    print("form is up:", page.locator('input[type="date"]').count() > 0)
    before = len(bubbles(page))

    # Fill whatever the form asks for, then submit.
    for sel in page.locator("select").all():
        opts = [o for o in sel.locator("option").all_inner_texts() if o.strip()]
        real = [o for o in opts if not o.lower().startswith(("select", "-- "))]
        if real and len(opts) > 1:
            try:
                sel.select_option(label=real[0])
            except Exception:
                pass
    for inp in page.locator('input[type="date"]').all():
        try:
            inp.fill("2026-09-02")
        except Exception:
            pass
    for inp in page.locator('input[type="time"]').all():
        try:
            inp.fill("09:00")
        except Exception:
            pass
    for inp in page.locator('input[type="number"]').all():
        try:
            inp.fill("2")
        except Exception:
            pass
    for inp in page.locator('input[type="text"]').all():
        try:
            if not (inp.input_value() or "").strip():
                inp.fill("Chennai Airport")
        except Exception:
            pass
    page.wait_for_timeout(800)
    try:
        page.locator("button:has-text('Submit')").first.click()
    except Exception as exc:
        print("could not submit:", exc)
    page.wait_for_timeout(25000)

    after = bubbles(page)
    print("\n=== EVERY BOT BUBBLE AFTER SUBMIT (%d new) ===" % max(0, len(after) - before))
    for t in after[before:]:
        print("---")
        print(t[:600])
    page.screenshot(path="overnight/_chat/form_ack.png")
    b.close()
