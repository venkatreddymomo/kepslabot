import re, sys
sys.path.insert(0, "overnight")
from playwright.sync_api import sync_playwright
CHAT="http://127.0.0.1:8011/chat?hotel_code=sarovar_portico_sriperumbudur&phase=during_stay"
TA='textarea[placeholder="Type a message..."]'
with sync_playwright() as pw:
    b=pw.chromium.launch(headless=True)
    p=b.new_page(viewport={"width":1400,"height":1000})
    p.set_default_timeout(30000)
    p.goto(CHAT, wait_until="domcontentloaded"); p.wait_for_timeout(4000)
    try:
        p.click("button:has-text('New Session')"); p.wait_for_timeout(2500)
    except Exception as e: print("new session:", repr(e)[:60])
    try: p.click("button[aria-label='Open Chat']")
    except Exception:
        p.evaluate("()=>{const b=[...document.querySelectorAll('button')].find(x=>/open chat/i.test(x.getAttribute('aria-label')||'')); if(b)b.click();}")
    p.wait_for_selector(TA, timeout=30000)
    m=re.search(r"Session:\s*(\S+)", p.inner_text("body"))
    print("composer found :", p.locator(TA).count()==1)
    print("send btn found :", p.locator("button[aria-label='Send message']").count()==1)
    print("session        :", m.group(1) if m else "?")
    print("profile room   :", "1400" in p.inner_text("body"))
    p.screenshot(path="overnight/smoke.png", full_page=True)
    b.close()
print("SMOKE OK")
