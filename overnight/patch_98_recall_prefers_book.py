"""ORDER RECALL and the order book were telling the model two different things.

ORDER RECALL says "answer STRICTLY from food_profile.latest_order". The book rule
says "never describe an order that is not in the book". Both are in the prompt at
once, and food_profile carries the SAME current order under a different name - so
on a dining turn the model was reconciling the book, food_profile.latest_order,
past_orders, food_order_status and current_confirmed_order. Five accounts of one
order. It stitched them together and told a guest a Filter Coffee was already on
its way when the book held only burgers.

The payload now drops food_order_status and current_confirmed_order when the book
is on. food_profile stays, because past_orders is genuinely about EARLIER stays
and drives recommendations - so this makes the split explicit instead: the book
is this stay, food_profile is history.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
MARK = "IF `order_book` IS PRESENT IT ANSWERS THIS"
ANCHOR = ("ORDER RECALL: when the guest asks about their food status or what they "
          "ordered")
ADDITION = (" - IF `order_book` IS PRESENT IT ANSWERS THIS, on its own: read the "
            "orders in the book and nothing else, and do not consult food_profile, "
            "past_orders or any other field for what they have ordered on this "
            "stay. Those describe EARLIER stays and are for recommendations only. "
            "Naming an order that is not in the book means promising food nobody "
            "is making -")


def main() -> int:
    dry = "--dry-run" in sys.argv
    url = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 15})
    with eng.connect() as c:
        prompt = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98")).scalar() or "")
    print("current: %d chars" % len(prompt))
    if MARK in prompt:
        print("already applied")
        return 0
    if ANCHOR not in prompt:
        print("ABORT: the ORDER RECALL rule was not found verbatim")
        return 2
    new_prompt = prompt.replace(ANCHOR, ANCHOR + ADDITION, 1)
    assert MARK in new_prompt and len(new_prompt) > len(prompt)
    if dry:
        i = new_prompt.find(MARK)
        print("\nWOULD INSERT:\n" + new_prompt[i - 120:i + len(ADDITION)])
        print("\n--dry-run: nothing written")
        return 0
    (ROOT / "backups" / "svc98_before_recall_prefers_book.txt").write_text(
        prompt, encoding="utf-8")
    with eng.begin() as c:
        c.execute(text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                       "generated_system_prompt_override=1 WHERE id=98"), {"p": new_prompt})
    with eng.connect() as c:
        ap = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98")).scalar() or "")
    print("written: %d chars" % len(ap))
    print("VERIFIED OK" if MARK in ap else "VERIFY FAILED")
    return 0 if MARK in ap else 1


if __name__ == "__main__":
    sys.exit(main())
