"""ORDER RECALL forbade naming a dish that is real but not yet placed.

Turn 61 of the 72-turn sweep: the guest said "order me a tiramisu", said "yes",
the bot put it in a new cart - and one turn later answered "what did i order"
with only the two orders already with the kitchen. The Tiramisu vanished.

Not model variance. The rule reads "answer STRICTLY from food_profile.latest_order
(and past_orders) ... NEVER include an item that is not in the profile", and
food_profile is built from PLACED orders only. A cart line is by definition not in
it, so the prompt required the omission.

The rule exists to stop the model rebuilding an order out of chat history, and
that part stays. `order_lifecycle.cart` is not chat history - it is the same
server-side state that produces the ticket - so it is exempt, and must be named
as still-to-be-placed so the guest can see the difference.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
OLD = ("answer STRICTLY from food_profile.latest_order (and past_orders) — name "
       "exactly those items and nothing else. NEVER rebuild an order from earlier "
       "chat, and NEVER include an item that is not in the profile (an item you "
       "declined as off-menu was NOT ordered).")
NEW = ("answer from food_profile.latest_order (and past_orders) for everything "
       "ALREADY WITH THE KITCHEN — name exactly those items and nothing else. "
       "NEVER rebuild an order from earlier chat, and NEVER include an item that "
       "is not in the profile (an item you declined as off-menu was NOT ordered). "
       "ONE EXCEPTION, AND IT IS NOT OPTIONAL: `order_lifecycle.cart` is server "
       "state, not chat history. If it holds any line, that dish is really the "
       "guest's and you MUST name it in the same answer, clearly marked as not "
       "yet sent — e.g. \"and 1 Tiramisu still waiting to be placed\" — and then "
       "offer to place it. Leaving a cart line out of a recall answer makes a "
       "dish the guest just asked for disappear, which is never acceptable.")
MARK = "ONE EXCEPTION, AND IT IS NOT OPTIONAL"


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
        print("ABORT: the old ORDER RECALL rule was not found verbatim")
        return 2

    bak = ROOT / "backups" / "svc98_before_recall_cart.txt"
    bak.parent.mkdir(exist_ok=True)
    bak.write_text(prompt, encoding="utf-8")
    print(f"backup: {bak}")

    new_prompt = prompt.replace(OLD, NEW)
    assert MARK in new_prompt and new_prompt != prompt
    assert "NEVER rebuild an order from earlier chat" in new_prompt, "anti-invention core lost"

    with eng.begin() as c:
        c.execute(text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                       "generated_system_prompt_override=1 WHERE id=98"), {"p": new_prompt})
    with eng.connect() as c:
        row = c.execute(text("SELECT generated_system_prompt, generated_system_prompt_override "
                             "FROM new_bot_services WHERE id=98")).first()._mapping
    ap = str(row["generated_system_prompt"] or "")
    ok = (MARK in ap and "NEVER rebuild an order from earlier chat" in ap
          and int(row["generated_system_prompt_override"] or 0) == 1)
    print(f"written: {len(ap)} chars, LOCK={row['generated_system_prompt_override']}")
    print("VERIFIED OK" if ok else "VERIFY FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
