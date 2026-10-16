"""Make the cart field mandatory whenever the reply claims the order changed.

Measured on the 72-turn run: 6 replies said "I've added X"; 3 of them carried no
`order_items` at all. On those turns the whole cart post-process is skipped
(llm_orchestration_grounding_mixin.py:3991 is gated on the field being a list),
so the cart keeps its old contents while the guest is told otherwise. Ticket
11135 went to the kitchen as Filter Coffee + Tandoori Murgh after the guest had
been shown "You've added 2 x Chicken Burger... Order total 3,375".

The rule already existed. It was written as an afterthought - "also emit the full
current order" - in the middle of a bullet list, so it reads as extra homework
rather than the thing that decides whether the food is real. Rewritten as a hard
precondition on the sentence itself, tied to the consequence.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
OLD = ("- REPORT THE ORDER AS DATA. In EVERY reply where the order changes or you "
       "recap/summarise it, also emit the full current order as structured JSON in "
       "pending_data_updates.order_items")
NEW = ("- THE ORDER IS REAL ONLY IF YOU EMIT IT. `pending_data_updates.order_items` "
       "is not a summary of your reply - it IS the order. The kitchen receives that "
       "field and nothing else. Your sentence is read by the guest; this field is "
       "read by the kitchen, and when they disagree the guest waits for food nobody "
       "is cooking. THE RULE: if your reply says ANYTHING was added, ordered, "
       "removed, changed or is in the cart - or you list or total the order at all - "
       "then that same reply MUST carry order_items, holding the COMPLETE order, "
       "every line, not just the new dish. No exceptions, no matter how short the "
       "reply. If you are not emitting the field, do not write a sentence that says "
       "food was added, because it was not. A guest was told 'You've added 2 x "
       "Chicken Burger' on a turn with no field; the ticket went to the kitchen "
       "without them. Emit the full current order as structured JSON in "
       "pending_data_updates.order_items")
MARK = "THE ORDER IS REAL ONLY IF YOU EMIT IT"


def main() -> int:
    url = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 15})
    with eng.connect() as c:
        prompt = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98")).scalar() or "")
    print(f"current: {len(prompt)} chars")
    if MARK in prompt:
        print("already applied")
        return 0
    if OLD not in prompt:
        print("ABORT: the old REPORT THE ORDER AS DATA rule was not found verbatim")
        return 2

    bak = ROOT / "backups" / "svc98_before_order_items_mandatory.txt"
    bak.parent.mkdir(exist_ok=True)
    bak.write_text(prompt, encoding="utf-8")
    print(f"backup: {bak}")

    new_prompt = prompt.replace(OLD, NEW)
    assert MARK in new_prompt
    # the example payload and the "exact menu item name" rule follow OLD - keep them
    assert "exact menu item name" in new_prompt
    assert '{"name":"Butter Chicken"' in new_prompt

    with eng.begin() as c:
        c.execute(text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                       "generated_system_prompt_override=1 WHERE id=98"), {"p": new_prompt})
    with eng.connect() as c:
        row = c.execute(text("SELECT generated_system_prompt, generated_system_prompt_override "
                             "FROM new_bot_services WHERE id=98")).first()._mapping
    ap = str(row["generated_system_prompt"] or "")
    ok = MARK in ap and int(row["generated_system_prompt_override"] or 0) == 1
    print(f"written: {len(ap)} chars, LOCK={row['generated_system_prompt_override']}")
    print("VERIFIED OK" if ok else "VERIFY FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
