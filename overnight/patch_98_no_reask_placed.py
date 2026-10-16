"""Never ask to place an order that is already placed.

Recall on a PLACED order was ending with the checkout question:

    "Your current placed order includes: - Chicken Burger - ₹750 ...
     Is this correct and shall I place this order for your room?"

It says the order is placed and then asks to place it, in one message. A guest who
reads that reasonably answers "yes" - and a yes to that question is how a second,
duplicate order gets raised for food already cooking. The server-rendered summary
already handles this correctly (it switches to "This is on its way to your room");
this is the model writing its own recap and appending the question out of habit.
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
    "- When there is nothing placed and nothing in the cart, say so plainly rather "
    "than implying an order exists.\n"
)

ADDITION = (
    "- NEVER ASK TO PLACE SOMETHING THAT IS ALREADY PLACED. When you recap an order "
    "that appears in `food_order_status.orders`, it is with the kitchen: end by saying "
    "so (\"this is on its way\") and NEVER close with \"shall I place this order?\", "
    "\"is this correct and shall I place it\", or any other placement question about "
    "it. Saying it is placed and asking to place it in the same message contradicts "
    "itself, and a guest who answers \"yes\" to that has just asked for a SECOND order "
    "of food already cooking.\n"
    "- The only order you may ever offer to place is one that is NOT yet with the "
    "kitchen — the cart. If the cart is empty and everything is placed, close with an "
    "offer to add something new instead, never with a placement question.\n"
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

        if "NEVER ASK TO PLACE SOMETHING THAT IS ALREADY PLACED" in prompt:
            print("already applied")
            return 0
        found = prompt.count(ANCHOR)
        if found != 1:
            print("FAIL: anchor found {} times (need exactly 1)".format(found))
            return 1

        before = len(prompt)
        prompt = prompt.replace(ANCHOR, ANCHOR + ADDITION)
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
        for marker in ("NEVER ASK TO PLACE SOMETHING THAT IS ALREADY PLACED",
                       "NAMES THE ACTION", "MOST OF THE MENU HAS NO STATED TIMING",
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
