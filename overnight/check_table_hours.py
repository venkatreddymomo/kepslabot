"""Walk the real widget to the Table Reservation form and read what the TIME
field actually offers, per outlet.

The hours came off the hotel's own KB pages and were written onto
new_bot_services.form_config (service 86):

    SPR Kitchen            06:30 - 22:30   ("Overall operating hours")
    Chola Lounge - The Bar 11:00 - 23:00   ("Hours")

Nothing here is invented. This proves the values survive BOTH server-side
whitelists and reach the guest's picker, which is the only place a 23:30
booking is actually stopped.
"""
import io
import os
import sys

from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"


def options(el) -> list[str]:
    return [t.strip() for t in el.locator("option").all_inner_texts() if t.strip()]


def find_outlet_select(page):
    for el in page.locator("select").all():
        opts = options(el)
        if "SPR Kitchen" in opts and "Chola Lounge - The Bar" in opts:
            return el
    return None


def find_time_slots(page) -> list[str]:
    """The time picker's option VALUES, which are 24-hour "HH:MM".

    Read the values, never the labels: the labels are 12-hour and unpadded
    ("6:30 AM"), so any test that filters them by shape silently drops every
    single-digit hour and the picker looks like it starts at 10:00.
    """
    for el in page.locator("select").all():
        vals = el.evaluate(
            "s => Array.from(s.options).map(o => o.value).filter(Boolean)")
        clock = [v for v in vals if len(v) == 5 and v[2] == ":"]
        if len(clock) >= 2:
            return clock
    return []


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=os.environ.get("UI_HEADLESS", "1") == "1")
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
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

        page.fill(TA, "i want to reserve a table at SPR Kitchen tomorrow at 8pm")
        page.click(SEND)

        outlet = None
        for _ in range(40):
            page.wait_for_timeout(3000)
            outlet = find_outlet_select(page)
            if outlet is not None:
                break
        if outlet is None:
            print("FAIL: the reservation form never appeared")
            browser.close()
            return 1
        print("form is up; time field before an outlet is chosen: %r"
              % (find_time_slots(page) or "(locked - 'Select Outlet first')"))

        failures = 0
        for name, lo, hi in (("SPR Kitchen", "06:30", "22:30"),
                             ("Chola Lounge - The Bar", "11:00", "23:00")):
            outlet.select_option(label=name)
            page.wait_for_timeout(2500)
            slots = find_time_slots(page)
            if not slots:
                print("[%s] FAIL - no slots offered" % name)
                failures += 1
                continue
            print("      %s" % (", ".join(slots[:4]) + " ... " + ", ".join(slots[-3:])))
            late = [s for s in slots if s >= "23:30"]
            ok = slots[0] == lo and slots[-1] <= hi and not late
            print("[%s] %d slots  %s .. %s  (expected %s .. %s)  %s"
                  % (name, len(slots), slots[0], slots[-1], lo, hi,
                     "PASS" if ok else "FAIL"))
            print("      23:30 bookable? %s" % ("YES - " + str(late) if late else "NO"))
            failures += 0 if ok else 1

        page.screenshot(path="overnight/_chat/table_form_hours.png")
        browser.close()
        print("\n%s" % ("ALL PASS" if failures == 0 else "%d FAILED" % failures))
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
