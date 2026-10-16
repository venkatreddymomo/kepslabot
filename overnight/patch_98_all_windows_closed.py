""""All the stated windows are shut" is not "the kitchen is shut".

At 10:34 breakfast has just ended and the tandoor sections do not open until noon,
so every entry in `menu_serving_windows` reads closed. The agent concluded the menu
itself was unavailable and refused a Chicken Burger - an all-day item in a section
with no stated timing. Nothing entered the cart, so the guest's "yes place it" had
nothing to place and no order ever reached the kitchen.

Most of the menu carries no timing at all; only four sections do. That has to be
said outright.
"""
import io
import json
import os
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

SERVICE_ID = 98

ANCHOR = (
    "- If the governing note has `open_now` false, DO NOT add the item."
)

ADDITION = (
    "- MOST OF THE MENU HAS NO STATED TIMING AND IS ALWAYS AVAILABLE. Only a few "
    "sections appear in `menu_serving_windows` at all. If EVERY listed section is "
    "closed right now, that means ONLY those sections are unavailable - it does NOT "
    "mean the kitchen is shut, the menu is unavailable, or the guest must wait. Never "
    "say the menu/kitchen is closed or \"not yet available\", and never refuse an item, "
    "because the listed windows happen to all be shut. An item no listed note covers "
    "is available NOW, whatever time it is.\n"
    "- The payload also states this as `menu_timing_note`; it and the list say the same "
    "thing.\n"
)


def main() -> int:
    url = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    engine = create_engine(url, connect_args={"connect_timeout": 10})

    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT name, generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=:i"), {"i": SERVICE_ID}).first()
        if row is None:
            print("FAIL: service {} not found".format(SERVICE_ID))
            return 1
        prompt = row._mapping["generated_system_prompt"]

        os.makedirs("backups", exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bpath = "backups/service_{}_prompt_{}.json".format(SERVICE_ID, stamp)
        with open(bpath, "w", encoding="utf-8") as f:
            json.dump(dict(row._mapping), f, ensure_ascii=False, indent=2, default=str)
        print("backup -> {}".format(bpath))

        if "MOST OF THE MENU HAS NO STATED TIMING" in prompt:
            print("already applied")
            return 0
        found = prompt.count(ANCHOR)
        if found != 1:
            print("FAIL: anchor found {} times (need exactly 1)".format(found))
            return 1

        before = len(prompt)
        prompt = prompt.replace(ANCHOR, ADDITION + ANCHOR)
        conn.execute(text(
            "UPDATE new_bot_services SET generated_system_prompt=:p, "
            "generated_system_prompt_override=1 WHERE id=:i"),
            {"p": prompt, "i": SERVICE_ID})
        print("prompt {} -> {} chars".format(before, len(prompt)))

    with engine.connect() as conn:
        saved = conn.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=:i"), {"i": SERVICE_ID}).first()._mapping
        ok = True
        for marker in ("MOST OF THE MENU HAS NO STATED TIMING",
                       "CART UNTIL IT IS PLACED", "ONE QUESTION, ONE YES",
                       "menu_serving_windows"):
            if marker not in saved["generated_system_prompt"]:
                print("VERIFY FAIL: {!r} missing".format(marker))
                ok = False
        if int(saved["generated_system_prompt_override"] or 0) != 1:
            print("VERIFY FAIL: override lock is not 1")
            ok = False
        print("VERIFIED IN DB (all rules present)" if ok else "VERIFY FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
