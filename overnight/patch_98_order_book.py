"""Teach service 98 to read the order book, without breaking the old path.

The payload carries EITHER `order_book` (the new table) or `order_lifecycle`
(the pending_data view), never both - handing the model two accounts of the same
order to reconcile is the bug the book exists to remove. So this adds a rule that
names whichever one arrived, rather than replacing the existing text: the same
prompt then works with the flag off and on, and flipping it needs no second
prompt change and no second restart.

Deliberately does NOT change what the model emits. `apply_write` accepts the
current flat `order_items` shape, so the book can be proven end-to-end against
today's output first. Moving the output contract as well would mean two changes
at once and no way to tell which one was responsible for anything.

    python overnight/patch_98_order_book.py --dry-run
    python overnight/patch_98_order_book.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
MARK = "THE ORDER BOOK IS THE ONLY RECORD"

# Anchored to the ORDER RECALL exception added earlier, so the new rule lands
# beside the other statements about what is and is not really the guest's.
ANCHOR = ("ONE EXCEPTION, AND IT IS NOT OPTIONAL: `order_lifecycle.cart` is server "
          "state, not chat history.")

ADDITION = (
    " THE ORDER BOOK IS THE ONLY RECORD. When the payload carries `order_book`, "
    "that is the guest's real order and `order_lifecycle` will not be there at "
    "all - read the book and nothing else. Each entry is ONE order with its own "
    "dishes, its own total and its own `status`. A `status` of `ongoing` means it "
    "has NOT gone to the kitchen and is the one you may offer to place; `placed` "
    "means it is already on its way, so never ask to place or confirm it again. "
    "`can_be_changed` is already worked out for you from the clock and the real "
    "ticket - trust it and never re-reason about how long ago something was "
    "ordered. `what_you_may_say` on each entry is the line to follow for that "
    "order. Never describe an order that is not in the book, and never tell the "
    "guest a dish was added unless you emit it in order_items on this same turn: "
    "if you do not write it down it does not exist, no ticket is raised and no "
    "food is made. If the guest gives an instruction about the order that is not "
    "a dish - no onions, ring twice, leave it at the door - put it in the `note` "
    "so the kitchen sees it."
)


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
        print("ABORT: the ORDER RECALL exception was not found verbatim - "
              "run patch_98_recall_includes_cart.py first")
        return 2

    new_prompt = prompt.replace(ANCHOR, ANCHOR + ADDITION, 1)
    assert MARK in new_prompt
    # The old path must survive untouched: with the flag off the model still
    # gets order_lifecycle and must still know what it means.
    assert "`order_lifecycle.cart` is server state" in new_prompt
    assert len(new_prompt) > len(prompt)

    if dry:
        i = new_prompt.find(MARK)
        print("\nWOULD INSERT:\n" + new_prompt[i - 90:i + len(ADDITION)])
        print("\n--dry-run: nothing was written")
        return 0

    bak = ROOT / "backups" / "svc98_before_order_book.txt"
    bak.parent.mkdir(exist_ok=True)
    bak.write_text(prompt, encoding="utf-8")
    print("backup: %s" % bak)

    with eng.begin() as c:
        c.execute(text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                       "generated_system_prompt_override=1 WHERE id=98"), {"p": new_prompt})
    with eng.connect() as c:
        row = c.execute(text("SELECT generated_system_prompt, generated_system_prompt_override "
                             "FROM new_bot_services WHERE id=98")).first()._mapping
    ap = str(row["generated_system_prompt"] or "")
    ok = (MARK in ap and "`order_lifecycle.cart` is server state" in ap
          and int(row["generated_system_prompt_override"] or 0) == 1)
    print("written: %d chars, LOCK=%s" % (len(ap), row["generated_system_prompt_override"]))
    print("VERIFIED OK" if ok else "VERIFY FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
