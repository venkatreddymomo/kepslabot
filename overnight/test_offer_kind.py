"""Which kind of offer did the bot just make, and what does a bare "yes" mean?

The three kinds lead to opposite instructions, so getting one wrong is not a
degraded answer, it is the wrong action:

  add_item     a named dish was offered for the ORDER -> yes means ADD IT
  show         options were offered to be LISTED      -> yes means list them,
                                                         and place nothing
  invite_more  "anything else?" with nothing named    -> yes means ask what

`add_item` is new. Without it, "would you like to add the Filter Coffee?" was
classified `show`, whose injected instruction ends "do NOT place, re-summarise or
confirm any order on this turn" - so a guest's yes produced a tidy restatement of
the coffee and no coffee. The `show` cases below are the ones that must NOT
regress into add_item, because conflating them the other way once placed a food
order when the guest had only said yes to hearing about drinks.
"""
import asyncio
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from services.llm_orchestration_service import llm_orchestration_service as S

CASES = [
    # (assistant said, guest replied, expected kind or None)
    ("A new item, Filter Coffee (South Indian Style) at ₹325, can be added to a new "
     "order. Would you like to add the Filter Coffee as a separate order?",
     "yes", "add_item"),
    ("Would you like to add 1 Filter Coffee at ₹325 to your existing order?",
     "yes", "add_item"),
    ("Shall I add 2 Garlic Naan to that?", "sure", "add_item"),
    ("The Arrabbiata Pasta is priced at ₹700. Would you like to add one to your "
     "current order of 2 Chicken Burgers?", "yes please", "add_item"),

    ("Shall I suggest some beverages that go well with your meal?", "yes", "show"),
    ("Would you like to hear about our desserts?", "yes please", "show"),
    ("I can show you what's available right now if you like.", "ok", "show"),

    ("Would you like to add anything else to your order?", "yes", "invite_more"),
    # A NAMED DISH WITH AN OPEN-ENDED TAIL. Read as invite_more, the dish is
    # thrown away and the guest is asked to choose again after already saying
    # yes - which is what happened to a Vegetable Burger.
    ("This order is already being prepared and cannot be changed now. If you would "
     "like, I can help you add a new separate order with the Vegetable Burger "
     "(Rs 650) or any other available items. What would you like to do?",
     "yeah do that", "add_item"),
    ("I can add the Filter Coffee at Rs 325, or anything else you fancy - what "
     "would you like?", "yes", "add_item"),
    ("Anything else for you?", "yes", "invite_more"),

    # Asking permission to ACT is not an offer at all - the normal confirm flow
    # must keep these, or a "yes place it" stops placing anything.
    ("Shall I place this order for your room now?", "yes", None),
    ("Shall I go ahead and raise this with housekeeping?", "yes please", None),
    ("Your order: 2 Chicken Burger, total ₹1500. Shall I place it?", "go ahead", None),
    # NAMING THE ACTION. The bot had suggested a paneer tikka in passing and
    # never asked to place; "yes place it" was read as accepting the tikka, so
    # the guest was asked what else they wanted and 2 Chicken Burgers never
    # reached the kitchen. An instruction is not a yes to the last thing said.
    ("You've added 2x Chicken Burger at Rs 750 each to your cart. Your cart total "
     "is Rs 1500. What else would you like to add? For example, you could try our "
     "Jaitooni Paneer Tikka at Rs 750 or a Naan starting at Rs 225.",
     "yes place it", None),
    ("Would you like to add anything else, or shall I suggest a dessert?",
     "place the order", None),
    ("Here are our desserts: Tiramisu Rs 500, Elaneer Payasam Rs 450.",
     "order it", None),

    # OFFERING A SEPARATE / NEW ORDER FOR A NAMED DISH. Filed as `show`, whose
    # injected text says do NOT place anything - so the guest said yes and got a
    # list. 2 Arrabbiata Pasta were confirmed to a guest and never recorded.
    ("The kitchen has already started that order, so it cannot be added to. If you'd "
     "like, I can help you start a new order for the 2 x Arrabbiata Pasta separately.",
     "yes", "add_item"),
    ("Shall I put the Tiramisu on a separate order for you?", "yes please", "add_item"),

    # Not a bare agreement - the guest said something specific.
    ("Would you like to add the Filter Coffee?", "no, make it a masala chai", None),
]


async def main() -> int:
    fails = 0
    for said, replied, want in CASES:
        got = await S._bare_yes_accepts_an_offer(
            previous_bot_message=said, user_message=replied)
        kind = (got or {}).get("kind")
        ok = kind == want
        fails += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  [{str(kind):11s}] want {str(want):11s} "
              f"| {replied!r} to {said[:52]!r}")
        if ok and kind == "add_item":
            print(f"          offered: {(got or {}).get('offered')!r}")
    print("\n" + ("ALL PASS" if not fails else f"{fails} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
