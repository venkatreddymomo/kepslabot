"""The five cart/order states, and the two ways an order stops being editable.

    EMPTY -> ONGOING -> PLACED -> PROCESSING -> DELIVERED

PLACED becomes PROCESSING on whichever comes FIRST: the edit window elapsing, or
the staff dashboard moving the ticket off `open`. Both directions matter - a
ticket a person picked up 30 seconds ago is already untouchable, and a ticket
nobody has touched stays editable right up to the window.

The dashboard is stubbed here so the states can be tested without one.
"""
import asyncio
import io
import sys
from datetime import datetime, timedelta, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from services import ird_order_lifecycle as L

fails = 0


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def check(label, got, want):
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"       want {want!r}\n       got  {got!r}")


def stub(*, picked=None, closed=None):
    """Pretend the staff dashboard says this."""
    async def _p(ref):
        return picked
    async def _c(ref):
        return closed
    import services.ticketing_service as ts
    ts.ticketing_service._staff_have_picked_up = _p
    L._ticket_is_closed = _c


async def main() -> int:
    global fails
    order = lambda **kw: {"ticket_ref": "11099", "n": 1,
                          "items": [{"item": "Chicken Burger", "qty": 2, "price": 750}], **kw}

    print("=== the cart ===")
    stub(picked=False, closed=False)
    lc = await L.build_lifecycle({})
    check("no cart, no orders -> EMPTY", lc["cart"]["state"], L.EMPTY)
    check("EMPTY says do not claim an order exists",
          "Do NOT claim" in lc["cart"]["what_you_may_say"], True)
    # THE SNAPSHOT IS NOT A VERDICT. Read as one, it cost a guest their Tiramisu:
    # they said yes to adding it and were told "your cart is empty, there is no
    # new order to place". The state describes the moment BEFORE the message.
    check("EMPTY says a dish asked for NOW still goes in",
          "goes INTO the cart now" in lc["cart"]["what_you_may_say"], True)
    check("the rules say this is the moment BEFORE the message",
          "MOMENT BEFORE THIS MESSAGE" in lc["rules"], True)

    lc = await L.build_lifecycle({"_ird_cart": [{"item": "Idli", "qty": 2, "price": 400}]})
    check("items, nothing placed -> ONGOING", lc["cart"]["state"], L.ONGOING)
    check("ONGOING is the only state that may ask to place",
          "ONLY STATE" in lc["cart"]["what_you_may_say"], True)
    check("ONGOING total", lc["cart"]["total"], 800)

    print("\n=== an order, and the two roads to PROCESSING ===")
    stub(picked=False, closed=False)
    lc = await L.build_lifecycle({"_ird_placed_orders": [order(placed_at=ago(1))]})
    check("placed 1 min ago, untouched -> PLACED", lc["orders"][0]["state"], L.PLACED)
    check("PLACED is editable", lc["orders"][0]["can_be_changed"], True)
    check("PLACED must not be re-offered",
          "NEVER ask" in lc["orders"][0]["what_you_may_say"], True)
    # THE REGRESSION THIS CAUGHT: the first wording said only "changed or
    # cancelled", and the bot read "already on its way" as "too late" - it told a
    # guest it could not add a coffee to an order placed 60 seconds earlier, which
    # is the exact thing the 5-minute window exists to allow.
    check("PLACED explicitly allows ADDING a dish",
          "CAN still be added" in lc["orders"][0]["what_you_may_say"], True)
    check("...and names the wrong answer",
          "cannot add to it" in lc["orders"][0]["what_you_may_say"], True)
    check("the rules say placed is not closed",
          "PLACED IS NOT CLOSED" in lc["rules"], True)

    lc = await L.build_lifecycle({"_ird_placed_orders": [order(placed_at=ago(9))]})
    check("placed 9 min ago (window 5) -> PROCESSING", lc["orders"][0]["state"], L.PROCESSING)
    check("PROCESSING is NOT editable", lc["orders"][0]["can_be_changed"], False)

    stub(picked=True, closed=False)
    lc = await L.build_lifecycle({"_ird_placed_orders": [order(placed_at=ago(0.5))]})
    check("picked up 30s ago -> PROCESSING (dashboard beats the clock)",
          lc["orders"][0]["state"], L.PROCESSING)
    check("and it says why", "dashboard" in lc["orders"][0]["why"], True)

    stub(picked=False, closed=True)
    lc = await L.build_lifecycle({"_ird_placed_orders": [order(placed_at=ago(1))]})
    check("ticket closed -> DELIVERED", lc["orders"][0]["state"], L.DELIVERED)
    check("DELIVERED is past tense",
          "past tense" in lc["orders"][0]["what_you_may_say"], True)

    print("\n=== the cart never repeats food already with the kitchen ===")
    stub(picked=False, closed=False)
    lc = await L.build_lifecycle({
        "_ird_cart": [{"item": "Chicken Burger", "qty": 2, "price": 750}],
        "_ird_placed_orders": [order(placed_at=ago(1))],
    })
    check("cart holding only the placed order -> EMPTY", lc["cart"]["state"], L.EMPTY)
    check("...and lists nothing", lc["cart"]["items"], [])

    lc = await L.build_lifecycle({
        "_ird_cart": [{"item": "Chicken Burger", "qty": 2, "price": 750},
                      {"item": "Filter Coffee", "qty": 1, "price": 325}],
        "_ird_placed_orders": [order(placed_at=ago(1))],
    })
    check("only the genuinely new dish is in the cart",
          lc["cart"]["items"], ["1 x Filter Coffee"])

    lc = await L.build_lifecycle({
        "_ird_cart": [{"item": "Chicken Burger", "qty": 3, "price": 750}],
        "_ird_placed_orders": [order(placed_at=ago(1))],
    })
    check("a third burger is a genuine addition", lc["cart"]["items"], ["1 x Chicken Burger"])

    lc = await L.build_lifecycle({
        "_ird_cart": [{"item": "Chicken Burger", "qty": 2, "price": 750}],
        "_ird_placed_orders": [order(placed_at=ago(1))],
        "_ird_fresh_order": True,
    })
    check("a FRESH order keeps its repeat", lc["cart"]["items"], ["2 x Chicken Burger"])

    print("\n=== several orders, each with its own state ===")
    stub(picked=False, closed=False)
    lc = await L.build_lifecycle({"_ird_placed_orders": [
        {"ticket_ref": "1", "n": 1, "placed_at": ago(30),
         "items": [{"item": "Chicken Burger", "qty": 2, "price": 750}]},
        {"ticket_ref": "2", "n": 2, "placed_at": ago(1),
         "items": [{"item": "Arrabbiata Pasta", "qty": 2, "price": 700}]},
    ]})
    check("order 1 (30 min old) is PROCESSING", lc["orders"][0]["state"], L.PROCESSING)
    check("order 2 (1 min old) is PLACED", lc["orders"][1]["state"], L.PLACED)
    check("each keeps its own total", [o["total"] for o in lc["orders"]], [1500, 1400])
    check("the rules forbid combining totals",
          "never add them" in lc["rules"], True)

    print("\n=== it never raises ===")
    for junk in (None, "nonsense", 42, {"_ird_cart": "not a list"},
                 {"_ird_placed_orders": [None, 7]}):
        try:
            await L.build_lifecycle(junk)
            print(f"PASS  survived {junk!r}")
        except Exception as exc:
            fails += 1
            print(f"FAIL  raised on {junk!r}: {exc!r}")

    print("\n" + ("ALL PASS" if not fails else f"{fails} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
