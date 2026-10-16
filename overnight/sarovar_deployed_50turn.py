"""Production Playwright runner for Sarovar pre_checkin + during_stay sweeps.

Runs against https://bot.kepsla.ai/chat using the production test harness page,
the mapped test profiles, and the real widget composer flow.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "overnight"
RUN_TAG = os.environ.get("RUN_TAG", datetime.datetime.now().strftime("deployed_%Y%m%d_%H%M%S"))
BASE_URL = os.environ.get("BASE_URL", "https://bot.kepsla.ai")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
HEADLESS = os.environ.get("UI_HEADLESS", "1") == "1"
SCENARIO_FILTER = {
    s.strip().lower() for s in (os.environ.get("SCENARIOS", "pre_checkin,during_stay").split(","))
    if s.strip()
}
MAX_TURNS = int(os.environ.get("MAX_TURNS", "0") or 0)

TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"
BUBBLE = "div.flex.justify-start"

PRECHECKIN_TURNS = [
    ("Hi, I have a booking next week. What time is check-in?", None, "info: check-in time"),
    ("And what time is check-out?", None, "info: check-out time"),
    ("Do you offer free WiFi in the rooms and lobby?", None, "info: wifi coverage"),
    ("What's the WiFi password?", None, "info: wifi password"),
    ("Can you share the hotel address and directions from Chennai airport?", None, "info: directions"),
    ("How far is Chennai airport from the hotel?", None, "info: airport distance"),
    ("Is parking available if I drive down?", None, "info: parking"),
    ("Do you have a pool and gym?", None, "info: amenities"),
    ("Do you have a spa? What treatments are available?", None, "info: spa overview"),
    ("What are the restaurant timings for breakfast and dinner?", None, "info: dining timings"),
    ("Can you arrange an airport pickup from Chennai airport on July 1, 2027 at 2 pm?", "submit", "ticket create: airport pickup"),
    ("Actually my flight now arrives at 4:30 pm, please update the pickup time.", "submit", "ticket update: pickup time"),
    ("How much does the airport pickup cost?", None, "policy gap: pickup charges"),
    ("Yes, please connect me to your team to confirm the pickup charges.", "submit", "handoff accept: pickup charges"),
    ("Can I get early check-in at 9 am? I arrive in the morning.", None, "policy: early check-in"),
    ("Yes please, note my early check-in request.", "submit", "handoff accept: early check-in note"),
    ("It's our wedding anniversary. Can you arrange flowers and a cake in the room on arrival?", "submit", "ticket create: anniversary setup"),
    ("Please change the cake to chocolate and add balloons as well.", "submit", "ticket update: anniversary setup"),
    ("Actually cancel the anniversary decoration request.", None, "ticket cancel: anniversary setup"),
    ("We need an extra bed for our child in the room.", "submit", "ticket create: extra bed"),
    ("Please note that I need Jain meals during the stay.", "submit", "ticket create: dietary note"),
    ("I'm vegetarian and allergic to peanuts. Please note that too.", "submit", "ticket update: dietary note"),
    ("Can someone from the front office call me?", "submit", "ticket create: callback"),
    ("What is the cancellation policy for my booking?", None, "policy gap: cancellation"),
    ("Yes, connect me to reservations to confirm the cancellation policy.", "submit", "handoff accept: cancellation policy"),
    ("What documents do I need at check-in? Is a passport required for locals?", None, "policy gap: check-in docs"),
    ("No, that's fine for now.", None, "decline handoff"),
    ("I'll be arriving late around 11 pm. Please hold my room.", "submit", "ticket create: late arrival note"),
    ("Please arrange a railway-station pickup from Tiruvallur instead.", "submit", "ticket create: railway pickup"),
    ("The train will arrive at 8:15 pm instead. Please update the pickup.", "submit", "ticket update: railway pickup"),
    ("Can you arrange a drop to Chennai airport after checkout?", None, "phase decline: departure transfer"),
    ("Can I book a room at your Delhi hotel instead?", None, "phase decline: other property booking"),
    ("Are pets allowed? I want to bring my dog.", None, "info: pets"),
    ("Can I get a smoking room?", None, "info: smoking room"),
    ("Do you have EV charging at the hotel?", None, "info: EV charging"),
    ("Can you arrange rose petals and a baby crib in the room?", "submit", "ticket create: bespoke setup"),
    ("Actually keep only the baby crib, no rose petals.", "submit", "ticket update: bespoke setup"),
    ("Can I request a room upgrade to a suite?", None, "booking change: room upgrade"),
    ("Yes, please connect me to your team about the upgrade.", "submit", "handoff accept: room upgrade"),
    ("What restaurants do you have at the hotel?", None, "info: dining outlets"),
    ("Do you serve Jain food or pure-veg meals?", None, "info then offer dietary note"),
    ("Yes, please note that preference for my stay.", "submit", "handoff accept: Jain meals"),
    ("Tell me a joke to pass the time.", None, "out of scope: joke"),
    ("What's 45782 multiplied by 991?", None, "out of scope: math"),
    ("How do I buy a car in Chennai?", None, "out of scope: external topic"),
    ("Is there valet parking as well?", None, "info: valet"),
    ("Can you confirm my name and room type from the booking?", None, "identity/booking recall"),
    ("What's my phone number on file?", None, "identity recall"),
    ("I'd like to speak to a human right away.", "submit", "ticket create: direct contact"),
    ("Thanks for the help. See you next week.", None, "closing"),
]

DURING_STAY_TURNS = [
    ("hi", None, "greeting names Venkat, room 1400"),
    ("Order food", None, "menu shows, veg/non-veg split, PDF attached, correct IST"),
    ("whats good for dinner", None, "browsing only, nothing added"),
    ("no thanks", None, "decline path, no order"),
    ("order food - 2 chicken burger and one filter coffee", None, "items + prices added"),
    ("yes place it", None, "ticket placed on this turn"),
    ("what did i order", None, "prices and a total"),
    ("can i get a continental breakfast", None, "out of window breakfast timing"),
    ("is the chocolate walnut brownie available right now", None, "untimed item availability"),
    ("whats available right now", None, "only in-window items"),
    ("oh i forgot, also add a filter coffee to that", None, "same ticket add-on"),
    ("i want to place a new order - 2 arrabbiata pasta", None, "new order split"),
    ("yes place it", None, "second food order"),
    ("what did i order", None, "orders shown separately"),
    ("i want to place a new order", None, "two-turn phrasing part 1"),
    ("2 vegetable burger", None, "two-turn phrasing part 2"),
    ("can i get a red wine as well?", None, "off-menu decline"),
    ("yes", None, "alternatives listed, not ordered"),
    ("ok what desserts do you have", None, "dessert browse"),
    ("add tiramisu", None, "dessert added"),
    ("actually cancel the tiramisu", None, "cancel one item"),
    ("what did i order", None, "order recall after item cancel"),
    ("make it 3 vegetable burgers instead of 2", None, "quantity change"),
    ("the AC in my room is not cooling properly", None, "maintenance handoff start"),
    ("yes", None, "maintenance ticket confirm"),
    ("yes lets continue", None, "return to food lane"),
    ("what did i order", None, "food order still intact"),
    ("cancel my second order", None, "cancel one order"),
    ("actually cancel my whole order", None, "cancel remaining food order"),
    ("book a table at kadak for 4 at 8pm", None, "unknown outlet decline"),
    ("spr kitchen, 4 people at 8pm tonight", "submit", "table form submit"),
    ("change the venue to chola lounge", None, "update same reservation"),
    ("change it back to spr", None, "update same reservation again"),
    ("change kadak hotel timings to 9pm", None, "refuse fake outlet edit"),
    ("actually make it 9pm", None, "update reservation time"),
    ("can you arrange a cab to the airport tomorrow at 6am", "cancel", "open then cancel transport request"),
    ("i need extra towels", None, "housekeeping request"),
    ("yes", None, "housekeeping confirm"),
    ("book me a spa session tomorrow at 5pm", "submit", "spa booking form or handoff"),
    ("actually cancel the spa booking", None, "spa cancel"),
    ("whats my name and room number", None, "identity recall"),
    ("whats my phone number", None, "phone recall"),
    ("book me a room at your delhi hotel", None, "phase decline"),
    ("can you book me a flight", None, "out of scope"),
    ("whats the wifi password", None, "wifi password"),
    ("do you allow pets", None, "pets policy"),
    ("mujhe apna order dubara batao", None, "Hindi order recall"),
    ("ok english please", None, "language switch back"),
    ("did my ac get fixed", None, "maintenance recall"),
    ("whats the checkout time", None, "checkout time"),
    ("thanks thats all", None, "closing"),
]

SCENARIOS = {
    "pre_checkin": {"phase": "pre_checkin", "turns": PRECHECKIN_TURNS},
    "during_stay": {"phase": "during_stay", "turns": DURING_STAY_TURNS},
}


def ensure_out_dir() -> Path:
    out = OUT_ROOT / RUN_TAG
    out.mkdir(parents=True, exist_ok=True)
    return out


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def log(message: str) -> None:
    line = "[{:%H:%M:%S}] {}".format(datetime.datetime.now(), message)
    print(line, flush=True)
    with (ensure_out_dir() / "progress.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def msg_count(page) -> int:
    try:
        body = page.inner_text("body")
    except Exception:
        return 0
    m = re.search(r"Messages:\s*(\d+)", body)
    return int(m.group(1)) if m else 0


def expand_all(page) -> None:
    try:
        page.evaluate(
            "() => {[...document.querySelectorAll('button')]"
            ".filter(b => /read more/i.test(b.innerText||'')).forEach(b => b.click());}"
        )
    except Exception:
        pass


def bot_bubbles(page) -> list[str]:
    try:
        raw = page.evaluate(
            "() => [...document.querySelectorAll('%s')]"
            ".map(d => (d.innerText||'').trim()).filter(Boolean)" % BUBBLE
        )
    except Exception:
        return []
    out: list[str] = []
    for text in raw:
        cleaned = re.sub(r"\s*(Read more|Show less)\s*$", "", text, flags=re.I).strip()
        if cleaned:
            out.append(cleaned)
    return out


def wait_for_reply(page, before_count: int, timeout_s: int = 180) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        if msg_count(page) >= before_count + 2:
            break
    last = None
    stable = 0
    while time.time() < deadline:
        expand_all(page)
        bubbles = bot_bubbles(page)
        current = bubbles[-1] if bubbles else ""
        if current and current == last:
            stable += 1
            if stable >= 3:
                return current
        else:
            stable = 0
        last = current
        page.wait_for_timeout(1000)
    return last or ""


def find_form(page):
    try:
        forms = page.locator("form").all()
    except Exception:
        return None
    for form in forms:
        try:
            if form.locator("input, select, textarea").count():
                return form
        except Exception:
            continue
    return None


def value_for(label: str, kind: str, phase: str) -> str:
    lab = (label or "").lower()
    if kind == "date":
        if phase == "pre_checkin":
            return "2027-07-02"
        return (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    if kind == "time":
        return "17:00"
    if kind == "number":
        return "4"
    if re.search(r"outlet|restaurant|venue", lab):
        return "SPR Kitchen"
    if re.search(r"treatment|session|service|massage", lab):
        return "Massage"
    if re.search(r"guest|people|party|headcount|pax", lab):
        return "4"
    if re.search(r"location|pickup|drop|airport", lab):
        return "Chennai International Airport"
    if re.search(r"trip", lab):
        return "Drop"
    if re.search(r"email", lab):
        return "venkat@example.com"
    if re.search(r"flight", lab):
        return "AI440"
    if re.search(r"train", lab):
        return "12622"
    if re.search(r"occasion", lab):
        return "Anniversary setup with cake"
    if re.search(r"special request|details|request|notes|concern|issue", lab):
        return "Please assist with this request."
    return "Please assist, thank you."


def submit_form(page, phase: str, note: str, scenario_dir: Path):
    form = find_form(page)
    if form is None:
        return None
    seen = []
    try:
        for el in form.locator("input, select, textarea").all():
            try:
                tag = el.evaluate("e => e.tagName")
                kind = el.evaluate("e => e.type || ''")
                label = el.evaluate(
                    "e => (e.labels && e.labels[0] && e.labels[0].innerText) || e.placeholder || e.name || ''"
                )
                if tag == "SELECT":
                    opts = el.evaluate("e => [...e.options].map(o => o.value).filter(Boolean)")
                    labels = el.evaluate("e => [...e.options].map(o => o.label).filter(Boolean)")
                    seen.append({"label": label, "type": "select", "options": opts, "labels": labels})
                    if opts:
                        el.select_option(opts[0])
                else:
                    seen.append({"label": label, "type": kind})
                    val = value_for(label, kind, phase)
                    if kind == "checkbox":
                        el.check()
                    else:
                        el.fill(val)
            except Exception as ex:
                seen.append({"label": "<error>", "type": str(ex)[:120]})
        submitted = False
        for selector in (
            "button[type=submit]",
            "button:has-text('Submit')",
            "button:has-text('Continue')",
            "button:has-text('Confirm')",
            "button:has-text('Send')",
        ):
            try:
                btn = form.locator(selector).first
                if btn.count():
                    btn.click()
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            page.keyboard.press("Enter")
        log(f"  {note}: form submitted fields={[s.get('label') for s in seen]}")
    except Exception as ex:
        log(f"  {note}: form submit failed {ex!r}")
    append_jsonl(scenario_dir / "forms.jsonl", {"at": note, "action": "submit", "fields": seen})
    return seen


def cancel_form(page, note: str, scenario_dir: Path) -> bool:
    try:
        btn = page.locator("button", has_text=re.compile(r"cancel request|cancel", re.I)).first
        if not btn.count():
            log(f"  {note}: no cancel button found")
            return False
        btn.click()
        append_jsonl(scenario_dir / "forms.jsonl", {"at": note, "action": "cancel"})
        log(f"  {note}: form cancelled")
        return True
    except Exception as ex:
        log(f"  {note}: cancel failed {ex!r}")
        return False


def open_chat(page) -> None:
    page.click("button:has-text('New Session')")
    page.wait_for_timeout(2500)
    try:
        page.click("button[aria-label='Open Chat']")
    except Exception:
        try:
            page.click("text='Need help? Chat with Kai'")
        except Exception:
            page.evaluate(
                "() => {"
                "const byLabel=[...document.querySelectorAll('button')]"
                ".find(x=>/open chat/i.test(x.getAttribute('aria-label')||''));"
                "if (byLabel) { byLabel.click(); return; }"
                "const byText=[...document.querySelectorAll('button')]"
                ".find(x=>/need help\\? chat with kai/i.test((x.innerText||'').trim()));"
                "if (byText) byText.click();"
                "}"
            )
    page.wait_for_selector(TA, timeout=45000)
    page.wait_for_timeout(2000)


def wait_for_profile(page, phase: str) -> bool:
    want = f"Mapped test profile active for {phase}"
    for _ in range(50):
        page.wait_for_timeout(1000)
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        if want in body:
            return True
    return False


def snapshot_panel(page, button_text: str, scenario_dir: Path, stem: str) -> None:
    try:
        dismiss_dialog(page)
        btn = page.locator("button", has_text=button_text).first
        if not btn.count():
            log(f"  snapshot {stem} skipped: button not found")
            return
        btn.click(timeout=5000)
        page.wait_for_timeout(1200)
        text = ""
        try:
            dialog = page.locator("[role=dialog]").last
            if dialog.count():
                text = dialog.inner_text(timeout=3000)
            else:
                text = page.inner_text("body")
        except Exception:
            text = page.inner_text("body")
        (scenario_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
        page.screenshot(path=str(scenario_dir / f"{stem}.png"))
        dismiss_dialog(page)
    except Exception as ex:
        log(f"  snapshot {stem} failed: {ex!r}")


def dismiss_dialog(page) -> None:
    try:
        dlg = page.locator("[role=dialog]").last
        if dlg.count():
            try:
                close_btn = dlg.locator("button").last
                if close_btn.count():
                    close_btn.click(timeout=2000, force=True)
            except Exception:
                pass
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    page.wait_for_timeout(600)


def run_scenario(browser, name: str, phase: str, turns: list[tuple[str, str | None, str]], run_dir: Path) -> dict:
    scenario_dir = run_dir / name
    scenario_dir.mkdir(parents=True, exist_ok=True)
    console_path = scenario_dir / "console.jsonl"
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    page.set_default_timeout(45000)

    def on_console(msg) -> None:
        append_jsonl(
            console_path,
            {
                "type": "console",
                "level": msg.type,
                "text": msg.text,
                "ts": datetime.datetime.now(datetime.UTC).isoformat(),
            },
        )

    def on_page_error(err) -> None:
        append_jsonl(
            console_path,
            {"type": "pageerror", "text": str(err), "ts": datetime.datetime.now(datetime.UTC).isoformat()},
        )

    page.on("console", on_console)
    page.on("pageerror", on_page_error)

    url = f"{BASE_URL}/chat?hotel_code={HOTEL}&phase={phase}"
    log(f"=== {name} start ===")
    page.goto(url, wait_until="domcontentloaded")
    prof = wait_for_profile(page, phase)
    log(f"{name}: mapped profile {'active' if prof else 'MISSING'}")
    page.screenshot(path=str(scenario_dir / "01_loaded.png"), full_page=True)
    open_chat(page)
    page.screenshot(path=str(scenario_dir / "02_chat_open.png"), full_page=True)

    session_id = ""
    try:
        m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
        session_id = m.group(1) if m else ""
    except Exception:
        session_id = ""

    greeting = (bot_bubbles(page) or [""])[0]
    rows = []
    latencies = []
    turns_to_run = turns[:MAX_TURNS] if MAX_TURNS > 0 else turns

    for i, (msg, form_action, intent) in enumerate(turns_to_run, 1):
        if not (page.locator(TA).count() and page.locator(TA).is_enabled()):
            for _ in range(5):
                submit_form(page, phase, f"unblocking before turn {i}", scenario_dir)
                page.wait_for_timeout(2500)
                if page.locator(TA).count() and page.locator(TA).is_enabled():
                    break
        if not (page.locator(TA).count() and page.locator(TA).is_enabled()):
            log(f"{name}: turn {i:02d} composer wedged, stopping")
            break

        before = msg_count(page)
        t0 = time.time()
        try:
            page.click(TA)
            page.fill(TA, "")
            page.type(TA, msg, delay=20)
            page.wait_for_timeout(200)
            try:
                page.click(SEND)
            except Exception:
                page.keyboard.press("Enter")
        except Exception as ex:
            log(f"{name}: turn {i:02d} send failed {ex!r}")

        reply = wait_for_reply(page, before)
        form_note = None
        if form_action == "submit":
            page.wait_for_timeout(2000)
            if submit_form(page, phase, f"turn {i}", scenario_dir) is not None:
                form_note = "submitted"
                reply = wait_for_reply(page, msg_count(page) - 2) or reply
        elif form_action == "cancel":
            page.wait_for_timeout(2000)
            if cancel_form(page, f"turn {i}", scenario_dir):
                form_note = "cancelled"
                page.wait_for_timeout(3000)
                expand_all(page)
                bubbles = bot_bubbles(page)
                reply = bubbles[-1] if bubbles else reply

        secs = round(time.time() - t0, 1)
        latencies.append(secs)
        row = {
            "turn": i,
            "guest": msg,
            "intent": intent,
            "secs": secs,
            "form": form_note,
            "bot": reply,
        }
        rows.append(row)
        append_jsonl(scenario_dir / "turns.jsonl", row)
        log(f"{name}: {i:02d}/{len(turns_to_run)} ({secs:5.1f}s) > {msg[:60]}")
        log(f"{name}:       bot: {reply[:220].replace(chr(10), ' ')}")
        if i in {1, 10, 20, 30, 40, len(turns_to_run)}:
            try:
                page.screenshot(path=str(scenario_dir / f"turn_{i:02d}.png"), full_page=True)
            except Exception:
                pass
        page.wait_for_timeout(500)

    snapshot_panel(page, "View History", scenario_dir, "history")
    snapshot_panel(page, "View Tickets", scenario_dir, "tickets")
    try:
        page.screenshot(path=str(scenario_dir / "final.png"), full_page=True)
    except Exception:
        pass
    page.close()

    result = {
        "scenario": name,
        "phase": phase,
        "session_id": session_id,
        "greeting": greeting,
        "turn_count": len(rows),
        "latency": {
            "min": min(latencies) if latencies else 0,
            "median": statistics.median(latencies) if latencies else 0,
            "max": max(latencies) if latencies else 0,
        },
        "rows": rows,
    }
    (scenario_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log(f"=== {name} done session={session_id} turns={len(rows)} ===")
    return result


def main() -> int:
    run_dir = ensure_out_dir()
    selected = {name: cfg for name, cfg in SCENARIOS.items() if name in SCENARIO_FILTER}
    if not selected:
        print(f"No scenarios matched {sorted(SCENARIO_FILTER)}", file=sys.stderr)
        return 2

    summary = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=0 if HEADLESS else 80,
            args=["--window-size=1500,1000", "--ignore-certificate-errors"],
        )
        for name, cfg in selected.items():
            summary.append(run_scenario(browser, name, cfg["phase"], cfg["turns"], run_dir))
        browser.close()

    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log(f"RESULT -> {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
