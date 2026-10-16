"""Drive the real widget with a guest who reads the bot's replies.

Same browser path as the scripted runner - it reuses its helpers - but the guest's
words are written each turn AFTER reading what the bot actually said, and a goal
ends when the bot's reply shows it is done rather than after a fixed number of
turns. This is the only way a run can tell a bot bug from a script bug.

    RUN_TAG=g1 UI_GOALS_JSON=overnight/goals_core.json python overnight/guest_run.py
"""
import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

import overnight.sarovar_50_turn as H
from overnight.guest_brain import Guest, goal_is_done

OUT = H.OUT
TAG = H.TAG
MAX_PER_GOAL = int(os.environ.get("MAX_TURNS_PER_GOAL", "6") or 6)

_goals_file = os.environ.get("UI_GOALS_JSON", "overnight/goals_core.json")
GOALS = json.load(open(_goals_file, encoding="utf-8"))


def run():
    guest = Guest()
    rows, history = [], []
    sid = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=os.environ.get("UI_HEADLESS", "0") == "1", slow_mo=120)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(45000)
        page.goto(H.CHAT_URL, wait_until="domcontentloaded")

        for _ in range(40):
            page.wait_for_timeout(1000)
            if "Mapped test profile active" in page.inner_text("body"):
                break
        H.log("mapped test profile active")

        page.click("button:has-text('New Session')")
        page.wait_for_timeout(3000)
        try:
            page.click("button[aria-label='Open Chat']")
        except Exception:
            page.evaluate(
                "() => {const b=[...document.querySelectorAll('button')]"
                ".find(x=>/open chat/i.test(x.getAttribute('aria-label')||'')); if(b) b.click();}")
        page.wait_for_selector(H.TA, timeout=45000)
        page.wait_for_timeout(3000)

        m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
        sid = m.group(1) if m else ""
        last_bot = (H.bot_bubbles(page) or [""])[0]
        H.log("chat open session={}".format(sid))
        H.log("greeting: {}".format(last_bot[:140].replace("\n", " ")))
        history.append({"role": "assistant", "content": last_bot})

        seen = {}
        turn_no = 0
        for gi, goal in enumerate(GOALS, 1):
            text = goal if isinstance(goal, str) else str(goal.get("goal") or "")
            form = "" if isinstance(goal, str) else str(goal.get("form") or "")
            H.log("\n=== GOAL {}/{}: {}".format(gi, len(GOALS), text))

            for step in range(MAX_PER_GOAL):
                msg, _ = guest.say(goal=text, last_bot=last_bot, history=history)
                if not msg:
                    H.log("   guest produced nothing, moving on")
                    break

                if not (page.locator(H.TA).count() and page.locator(H.TA).is_enabled()):
                    H.submit_form(page, "unblocking")
                    page.wait_for_timeout(3000)

                before = H.msg_count(page)
                t0 = time.time()
                turn_no += 1
                try:
                    page.click(H.TA)
                    page.fill(H.TA, "")
                    page.type(H.TA, msg, delay=30)
                    page.wait_for_timeout(300)
                    page.click(H.SEND)
                except Exception as ex:
                    H.log("   send failed: {!r}".format(ex))
                    break

                reply = H.wait_for_reply(page, before)
                if form == "submit":
                    page.wait_for_timeout(2500)
                    if H.submit_form(page, "goal {}".format(gi)) is not None:
                        b2 = H.msg_count(page)
                        reply = H.wait_for_reply(page, b2 - 2) or reply
                elif form == "cancel":
                    page.wait_for_timeout(2500)
                    if H.cancel_form(page, "goal {}".format(gi)):
                        page.wait_for_timeout(4000)
                        H.expand_all(page)
                        reply = (H.bot_bubbles(page) or [reply])[-1]

                secs = round(time.time() - t0, 1)
                history.append({"role": "user", "content": msg})
                history.append({"role": "assistant", "content": reply})
                last_bot = reply

                tk = H.tickets_for(sid)
                changed = []
                for t in tk:
                    sig = (t.get("ticket_ref"), t.get("status"), str(t.get("issue"))[:200])
                    if seen.get(t["id"]) != sig:
                        changed.append(t)
                        seen[t["id"]] = sig

                row = {"turn": turn_no, "goal": text, "guest": msg, "bot": reply,
                       "secs": secs, "tickets_total": len(tk),
                       "tickets_changed": [
                           {"ref": t.get("ticket_ref"), "status": t.get("status"),
                            "dept": t.get("department"), "issue": str(t.get("issue"))[:200]}
                           for t in changed]}
                rows.append(row)
                with open(os.path.join(OUT, TAG + "_turns.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

                H.log("   {:02d} ({:4.1f}s) guest: {}".format(turn_no, secs, msg[:70]))
                H.log("        bot: {}".format(reply[:170].replace("\n", " ")))
                for t in changed:
                    H.log("        TICKET {} [{}] {} :: {}".format(
                        t.get("ticket_ref"), t.get("status"), t.get("department"),
                        str(t.get("issue"))[:80]))

                # Judged from the bot's reply, by the guest, after seeing it.
                met = goal_is_done(guest._client, goal=text, last_bot=reply)
                if met:
                    H.log("   -> goal met in {} turn(s)".format(step + 1))
                    break
            else:
                H.log("   -> GOAL NOT MET after {} turns".format(MAX_PER_GOAL))

        try:
            page.screenshot(path=os.path.join(OUT, TAG + "_final.png"), full_page=True)
        except Exception:
            pass
        browser.close()
    return sid, rows


if __name__ == "__main__":
    H.log("=== goal-driven guest run start ({}) ===".format(TAG))
    sid, rows = run()
    tickets = H.tickets_for(sid)
    local = [t for t in tickets if str(t.get("ticket_ref") or "").upper().startswith("LOCAL-")]
    lat = [r["secs"] for r in rows] or [0]
    json.dump({"session": sid, "turns": rows, "tickets": tickets,
               "local_only": len(local),
               "latency": {"min": min(lat), "median": statistics.median(lat), "max": max(lat)}},
              open(os.path.join(OUT, TAG + "_result.json"), "w", encoding="utf-8"),
              indent=1, ensure_ascii=False, default=str)
    H.log("\n===== TICKETS ({}) =====".format(len(tickets)))
    for t in tickets:
        H.log("  {} [{}] {} :: {}".format(t.get("ticket_ref"), t.get("status"),
                                          t.get("department"), str(t.get("issue"))[:90]))
    H.log("turns={} tickets={} local-only={}".format(len(rows), len(tickets), len(local)))
