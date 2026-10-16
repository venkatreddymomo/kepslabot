"""Cart until it is placed, order after.

The agent said "You've added Idli at ₹400 to your order" while the guest was still
choosing, so an unplaced cart read as food already on its way. The distinction is a
judgement the model has all the information to make - `food_order_status.orders`
holds what the kitchen actually has - so it is given as a rule, not enforced by
string matching.

Backs the row up first; `backups/` is gitignored so this is the only copy.
"""
import io
import json
import os
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

SERVICE_ID = 98

ANCHOR = "## HOLD THE ORDER IN MEMORY — NEVER LOSE IT"

ADDITION = (
    "## CART UNTIL IT IS PLACED, ORDER AFTER\n"
    "- While the guest is still choosing, what they have is a **cart** — say \"added "
    "to your cart\", \"in your cart\", \"your cart\", \"cart total\". It becomes \"your "
    "order\" ONLY once it has actually gone to the kitchen, which is exactly when it "
    "appears in `food_order_status.orders`.\n"
    "- NEVER call an unplaced cart \"your order\". The guest reads that as food already "
    "on its way, stops adding, and later asks why it never arrived. Equally, never call "
    "a placed order \"your cart\" — it is with the kitchen and can no longer be edited "
    "freely.\n"
    "- Answer in the guest's own frame: \"what's in my cart\" is about the unplaced "
    "cart, \"what did I order\" is about what is with the kitchen. If they ask about one "
    "and the other also exists, answer the one they asked about FIRST, then mention the "
    "other in one short line — never merge the two or add their totals together.\n"
    "- When there is nothing placed and nothing in the cart, say so plainly rather than "
    "implying an order exists.\n"
    "\n"
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

        if "CART UNTIL IT IS PLACED" in prompt:
            print("already applied, nothing to do")
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
        for marker in ("CART UNTIL IT IS PLACED", "ONE QUESTION, ONE YES",
                       "menu_serving_windows"):
            if marker not in saved["generated_system_prompt"]:
                print("VERIFY FAIL: {!r} missing".format(marker))
                ok = False
        if int(saved["generated_system_prompt_override"] or 0) != 1:
            print("VERIFY FAIL: override lock is not 1")
            ok = False
        print("VERIFIED IN DB (cart rule + earlier fixes all present)" if ok
              else "VERIFY FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
