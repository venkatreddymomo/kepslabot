"""Stop the prompt calling `current_confirmed_order` "not yet placed".

It is not. The field is deliberately the WHOLE cart, placed lines included, so a
dish added inside the edit window has something to join and so the off-menu
whitelist has something to guard. Narrowing its contents was tried and reverted:
with the field emptied, "also add a filter coffee" had nothing to append to and
the coffee never reached the kitchen.

So the contents are right and the DEFINITION is wrong, and the definition is what
the model believes. Dumped on a live turn where the order was already being
prepared, the field held [Tiramisu, Chicken Burger, Filter Coffee] while the
prompt swore it was unplaced - so the bot showed a combined ₹1,575 including food
already on its way, asked to place it, and in the same breath said the order
could no longer be changed.

`order_lifecycle` and `food_order_status.not_yet_placed` are the fields that
actually answer "does any of this still need placing". Point the model at those.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
OLD = ("current_confirmed_order is the IN-PROGRESS cart not yet placed; "
       "food_profile.latest_order / past_orders are what is already on its way / "
       "was ordered before.")
NEW = ("current_confirmed_order is EVERY line of the guest's order - including "
       "lines already sent to the kitchen. IT DOES NOT TELL YOU WHETHER ANYTHING "
       "STILL NEEDS PLACING, and you must never read it as an unplaced order or "
       "total it up as one bill to be confirmed. What still needs placing is "
       "answered ONLY by `order_lifecycle.cart` and "
       "`food_order_status.not_yet_placed`: if those are empty, everything in "
       "current_confirmed_order is already with the kitchen, so do not offer to "
       "place it and do not present its total as something to approve. Use this "
       "field to name dishes correctly and to keep an item you already declined "
       "from reappearing - nothing else. food_profile.latest_order / past_orders "
       "are what is already on its way / was ordered before.")


def main() -> int:
    url = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 10})
    with eng.connect() as c:
        prompt = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98")).scalar() or "")
    print(f"current: {len(prompt)} chars")

    if "IT DOES NOT TELL YOU WHETHER ANYTHING STILL NEEDS PLACING" in prompt:
        print("already applied")
        return 0
    if OLD not in prompt:
        print("ABORT: the old definition was not found verbatim")
        return 2

    new_prompt = prompt.replace(OLD, NEW)
    assert "IN-PROGRESS cart not yet placed" not in new_prompt, "the old claim survived"
    assert "IT DOES NOT TELL YOU WHETHER ANYTHING STILL NEEDS PLACING" in new_prompt
    assert len(new_prompt) > len(prompt)

    with eng.begin() as c:
        c.execute(text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                       "generated_system_prompt_override=1 WHERE id=98"), {"p": new_prompt})
    with eng.connect() as c:
        row = dict(c.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=98")).first()._mapping)
    ap = str(row["generated_system_prompt"] or "")
    ok = ("IN-PROGRESS cart not yet placed" not in ap
          and "IT DOES NOT TELL YOU WHETHER ANYTHING STILL NEEDS PLACING" in ap
          and int(row["generated_system_prompt_override"] or 0) == 1)
    print(f"written: {len(ap)} chars, LOCK={row['generated_system_prompt_override']}")
    print("VERIFIED OK" if ok else "VERIFY FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
