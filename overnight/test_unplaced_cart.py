"""Prove the cart/kitchen subtraction that decides `not_yet_placed` and
`current_confirmed_order`.

The bug: the cart survives placement on purpose (so a dish added inside the edit
window joins the same ticket), and every one of its lines was then handed to the
model as food nobody had sent - so "what did I order" a minute after ordering was
answered with "Shall I place this order for your room now?".

Every case below is a real turn shape, not an invented one. Cases 2 and 3 are the
reported bug; 4-6 are the behaviour that must NOT regress.
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from services.llm_orchestration_service import LLMOrchestrationService as S

f = S._ird_unplaced_cart_lines


def norm(lines):
    return sorted(
        (str(l.get("item") or "").strip().lower(), int(l.get("qty") or 1))
        for l in lines
    )


CASES = [
    (
        "1. BEFORE placement - nothing sent yet, whole cart is unplaced",
        {"_ird_cart": [{"item": "Idli", "qty": 2, "price": 400}]},
        [("idli", 2)],
    ),
    (
        "2. AFTER placement, untouched - the reported bug (2 idli placed)",
        {
            "_ird_cart": [{"item": "Idli", "qty": 2, "price": 400}],
            "_ird_open_order_items": [{"item": "Idli", "name": "Idli", "qty": 2}],
        },
        [],
    ),
    (
        "3. THE SCREENSHOT - 2 idli placed, one cancelled, then 'what did i order'",
        {
            "_ird_cart": [{"item": "Idli", "qty": 1, "price": 400}],
            "_ird_open_order_items": [{"item": "Idli", "name": "Idli", "qty": 1}],
        },
        [],
    ),
    (
        "4. SECOND HELPING - 'another idli': declared 2, kitchen has 1, so 1 to place",
        {
            "_ird_cart": [{"item": "Idli", "qty": 2, "price": 400}],
            "_ird_open_order_items": [{"item": "Idli", "name": "Idli", "qty": 1}],
        },
        [("idli", 1)],
    ),
    (
        "5. ADDING TO AN OPEN ORDER - only the new dish is awaiting placement",
        {
            "_ird_cart": [
                {"item": "Idli", "qty": 2, "price": 400},
                {"item": "Filter Coffee", "qty": 1, "price": 325},
            ],
            "_ird_open_order_items": [{"item": "Idli", "name": "Idli", "qty": 2}],
        },
        [("filter coffee", 1)],
    ),
    (
        "6. FRESH ORDER - cart was reset, never subtract or a repeat vanishes",
        {
            "_ird_cart": [{"item": "Dosa (Plain or Masala)", "qty": 1, "price": 400}],
            "_ird_open_order_items": [
                {"item": "Dosa (Plain or Masala)", "name": "Dosa (Plain or Masala)", "qty": 1}
            ],
            "_ird_fresh_order": True,
        },
        [("dosa (plain or masala)", 1)],
    ),
    ("7. EMPTY CART", {"_ird_cart": []}, []),
    ("8. NO CART KEY AT ALL", {}, []),
    ("9. pending_data is not a dict - must not raise", None, []),
    (
        "10. JUNK qty / missing name - must not raise, must not invent a line",
        {
            "_ird_cart": [
                {"item": "Idli", "qty": "two", "price": 400},
                {"item": "   ", "qty": 1},
            ],
            "_ird_open_order_items": [{"name": "Idli", "qty": None}],
        },
        [],
    ),
    (
        "11. NAME CASE / SPACING differs between cart and ticket",
        {
            "_ird_cart": [{"item": "  filter COFFEE ", "qty": 1}],
            "_ird_open_order_items": [{"name": "Filter Coffee", "qty": 1}],
        },
        [],
    ),
    (
        "12a. AFTER A SPLIT the open-order record is GONE - fall back to the ledger.\n"
        "     This is the duplicate-food ticket: the split pops _ird_open_order_items,\n"
        "     so every cart line looked unplaced again and the burgers were re-sent.",
        {
            "_ird_cart": [{"item": "Chicken Burger", "qty": 2, "price": 750}],
            # _ird_open_order_items deliberately absent, as a split leaves it
            "_ird_placed_orders": [
                {"ticket_ref": "11084", "n": 1,
                 "items": [{"item": "Chicken Burger", "name": "Chicken Burger", "qty": 2}]}
            ],
        },
        [],
    ),
    (
        "12b. LEDGER FALLBACK still lets a genuinely NEW dish through",
        {
            "_ird_cart": [
                {"item": "Chicken Burger", "qty": 2, "price": 750},
                {"item": "Arrabbiata Pasta", "qty": 2, "price": 700},
            ],
            "_ird_placed_orders": [
                {"ticket_ref": "11084", "n": 1,
                 "items": [{"item": "Chicken Burger", "name": "Chicken Burger", "qty": 2}]}
            ],
        },
        [("arrabbiata pasta", 2)],
    ),
    (
        "12c. TWO placed orders holding the same dish must not over-subtract a repeat",
        {
            "_ird_cart": [{"item": "Dosa", "qty": 2}],
            "_ird_placed_orders": [
                {"ticket_ref": "1", "n": 1, "items": [{"item": "Dosa", "qty": 1}]},
                {"ticket_ref": "2", "n": 2, "items": [{"item": "Dosa", "qty": 1}]},
            ],
        },
        [("dosa", 1)],   # highest per dish (1), not the sum (2)
    ),
    (
        "12d. the open-order record WINS when present (ledger is only a fallback)",
        {
            "_ird_cart": [{"item": "Chicken Burger", "qty": 3}],
            "_ird_open_order_items": [{"item": "Chicken Burger", "qty": 1}],
            "_ird_placed_orders": [
                {"ticket_ref": "11084", "n": 1,
                 "items": [{"item": "Chicken Burger", "qty": 2}]}
            ],
        },
        [("chicken burger", 2)],   # 3 - 1 from the open order, not 3 - 2
    ),
    (
        "12e. FRESH ORDER still wins over the ledger - a repeat must survive.\n"
        "     After a split the cart is emptied, so anything in it was asked for\n"
        "     afterwards; subtracting the ledger here would delete 'another dosa'.\n"
        "     What keeps a CARRIED-FORWARD dish out of a fresh order is the\n"
        "     provenance judge, not this subtraction.",
        {
            "_ird_cart": [{"item": "Dosa", "qty": 1}],
            "_ird_fresh_order": True,
            "_ird_placed_orders": [
                {"ticket_ref": "1", "n": 1, "items": [{"item": "Dosa", "qty": 1}]}
            ],
        },
        [("dosa", 1)],
    ),
    (
        "12. TICKET HOLDS A DISH THE CART DOES NOT - must not resurrect it",
        {
            "_ird_cart": [{"item": "Tiramisu", "qty": 1, "price": 500}],
            "_ird_open_order_items": [{"item": "Idli", "name": "Idli", "qty": 2}],
        },
        [("tiramisu", 1)],
    ),
]

fails = 0
for label, pending, want in CASES:
    try:
        got = norm(f(pending))
    except Exception as exc:
        print("ERROR {}\n       raised {!r}".format(label, exc))
        fails += 1
        continue
    ok = got == sorted(want)
    fails += 0 if ok else 1
    print("{}  {}".format("PASS" if ok else "FAIL", label))
    if not ok:
        print("       want {}\n       got  {}".format(sorted(want), got))

# The price/allergen fields must survive, or the summary loses its money.
kept = f({
    "_ird_cart": [{"item": "Filter Coffee", "qty": 3, "price": 325, "allergens": "dairy"}],
    "_ird_open_order_items": [{"name": "Filter Coffee", "qty": 1}],
})
ok = (
    len(kept) == 1 and kept[0].get("price") == 325
    and kept[0].get("allergens") == "dairy" and kept[0].get("qty") == 2
)
fails += 0 if ok else 1
print("{}  13. price/allergens survive the subtraction -> {}".format(
    "PASS" if ok else "FAIL", kept))

# The caller must not have its stored cart mutated underneath it.
_pd = {
    "_ird_cart": [{"item": "Idli", "qty": 2}],
    "_ird_open_order_items": [{"name": "Idli", "qty": 1}],
}
f(_pd)
ok = _pd["_ird_cart"][0]["qty"] == 2
fails += 0 if ok else 1
print("{}  14. the real cart is left untouched (no in-place mutation)".format(
    "PASS" if ok else "FAIL"))

print("\n{}".format("ALL PASS" if not fails else "{} FAILED".format(fails)))
sys.exit(1 if fails else 0)
