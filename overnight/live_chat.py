"""A chat session I can actually hold a conversation in, one turn at a time.

Scripted sweeps write every message up front, so they cannot follow up on
something odd, cannot rephrase, and cannot chase a wrong answer - which is most
of what real testing is. This keeps a browser open in the background and takes
messages one at a time from a file, so each message can be decided AFTER reading
the reply to the last one.

    python overnight/live_chat.py            # opens the session, then waits
    echo "hi" >> overnight/_chat/in.txt      # send a message
    cat overnight/_chat/out.txt              # read the reply

Every turn is appended to overnight/_chat/log.md as it happens.
Write "__QUIT__" to end the session.
"""
from __future__ import annotations

import datetime
import io
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
PHASE = os.environ.get("PHASE", "during_stay")
HEADLESS = os.environ.get("UI_HEADLESS", "0") == "1"
DIR = ROOT / "overnight" / "_chat"
DIR.mkdir(parents=True, exist_ok=True)
IN, OUT, LOG = DIR / "in.txt", DIR / "out.txt", DIR / "log.md"

TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"


def msg_count(page) -> int:
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else -1


def bubbles(page):
    return [t.strip() for t in page.locator("div.flex.justify-start").all_inner_texts() if t.strip()]


def clear_form(page) -> bool:
    for label in ("Cancel request", "Cancel"):
        try:
            b = page.locator(f"button:has-text('{label}')").first
            if b.count():
                b.click()
                page.wait_for_timeout(2000)
                if page.locator(TA).count() and page.locator(TA).is_enabled():
                    return True
        except Exception:
            continue
    return False


def send(page, text: str) -> str:
    if not (page.locator(TA).count() and page.locator(TA).is_enabled()):
        clear_form(page)
    before = msg_count(page)
    page.fill(TA, text)
    page.click(SEND)
    for _ in range(110):
        page.wait_for_timeout(1000)
        if msg_count(page) > before + 1:
            break
    page.wait_for_timeout(2200)
    b = bubbles(page)
    return b[-1] if b else "(no reply captured)"


def main() -> int:
    IN.write_text("", encoding="utf-8")
    OUT.write_text("", encoding="utf-8")
    LOG.write_text(f"# live chat — {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n", encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS, slow_mo=80)
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        page.set_default_timeout(60000)
        page.goto(f"{BASE_URL}/chat?hotel_code={HOTEL}&phase={PHASE}", wait_until="domcontentloaded")
        for _ in range(50):
            page.wait_for_timeout(1000)
            try:
                if f"Mapped test profile active for {PHASE}" in page.inner_text("body"):
                    break
            except Exception:
                pass
        page.click("button:has-text('New Session')")
        page.wait_for_timeout(2500)
        try:
            page.click("button[aria-label='Open Chat']")
        except Exception:
            page.click("text='Need help? Chat with Kai'")
        page.wait_for_selector(TA, timeout=45000)
        page.wait_for_timeout(2000)
        sid = ""
        try:
            m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
            sid = m.group(1) if m else ""
        except Exception:
            pass
        OUT.write_text(f"READY session={sid}\n", encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"session `{sid}`\n\n")
        print(f"READY session={sid}", flush=True)

        seen = 0
        idle = 0
        while idle < 3600:
            try:
                lines = [l for l in IN.read_text(encoding="utf-8").split("\n") if l.strip()]
            except OSError:
                lines = []
            if len(lines) <= seen:
                time.sleep(1)
                idle += 1
                continue
            idle = 0
            for text in lines[seen:]:
                seen += 1
                if text.strip() == "__QUIT__":
                    browser.close()
                    print("session ended", flush=True)
                    return 0
                reply = send(page, text)
                OUT.write_text(reply, encoding="utf-8")
                with LOG.open("a", encoding="utf-8") as fh:
                    fh.write(f"**guest:** {text}\n\n**bot:** {reply}\n\n---\n\n")
                print(f"\n> {text}\n< {reply}\n", flush=True)
        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
