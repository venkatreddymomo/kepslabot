"""Classify EVERY dish on the in-room dining menu by when it is served.

The table has been carrying 30 dishes and letting the other ~130 mean "all day"
by being absent. Absence is a weak signal and it has already cost us: Kerala
Paratha and Phulka, both all-day breads, inherited their restricted neighbours'
hours because nothing said otherwise.

So this walks the whole menu, resolves each dish's section path, and proposes a
period for every single item - then prints the result grouped for review, with
the uncertain ones called out separately. Output feeds
`config/ird_serving_windows.json`.

    python overnight/classify_menu.py            # review on screen
    python overnight/classify_menu.py --json     # the items block, ready to paste
"""
import io
import json
import re
import sys
from collections import OrderedDict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MENU = "overnight/ird_menu_dump.txt"
SKIP = re.compile(
    r"^(price|type|allergens|calories|serving size|includes|options|served with|"
    r"choice of|available flavours|note)\b", re.I)
# Lines that are prose or a component, not dish names.
PROSE = re.compile(r"[.!]$|^\d+\s*(kcal|gm|ml)\b|^(white|brown|multigrain)$", re.I)
# A line starting lowercase is a continuation, never a dish name. CASE-SENSITIVE
# on purpose: with re.IGNORECASE this matched every capitalised dish name too and
# silently classified nothing at all.
STARTS_LOWER = re.compile(r"^[a-z]")


def is_heading(l: str) -> bool:
    return (bool(l) and l == l.upper() and any(c.isalpha() for c in l)
            and not any(c.isdigit() for c in l) and len(l) > 2)


# A line opening a list of things that come WITH a dish. Everything until the next
# heading or the next Price/Type belongs to that list, not to the menu: "Danish
# Pastry", "Croissant", "Tea / Coffee" are what a ₹600 breakfast contains, not
# dishes anyone can order. Counting them pushed the total from ~160 to 218.
SUBLIST = re.compile(r"^(includes|options|served with|choice of|available flavours)\b", re.I)


def parse(path: str):
    """Every orderable dish, with the section path it sits under.

    Two passes, because the menu's own shape defeats a single one. A heading is a
    PARENT when no dish appears between it and the next heading - and that has to
    be looked up, not guessed from adjacency: `INDIAN SELECTION` is followed by
    its Tandoori-Kebab note before `APPETIZERS`, so an "are two headings adjacent"
    test filed all five of its sub-sections under INTERNATIONAL SELECTION.

    Handles the three item shapes: food (`Type:` then `Price:`), drinks with their
    own price, and drinks that inherit a section price and carry only `Allergens:`.
    """
    txt = open(path, encoding="utf-8").read()
    start = txt.find("IN ROOM DINING MENU")
    raw = [l.rstrip() for l in txt[start:].split("\n")]
    idx = [i for i, l in enumerate(raw) if l.strip()]

    # ── pass 1: where are the headings, and where are the dishes? ──────────
    #
    # THE IMMEDIATE PREDECESSOR, not a look-ahead window. A dish name is the line
    # DIRECTLY above its `Type:` (food) or `Price:` (drinks). A three-line window
    # instead swept in the contents of every "Includes" list - "Danish Pastry",
    # "Tea / Coffee", "Pickle", "Tomato Chutney" - because those sit a couple of
    # lines above the next real dish's Type:, and the count went 153 -> 218.
    # Nothing inside a sub-list ever has a Type: or Price: of its own, so keying
    # on the line immediately above one is exact.
    heads: list[tuple[int, str]] = []
    dishes: list[tuple[int, str]] = []
    for pos, i in enumerate(idx):
        l = raw[i].strip()
        if is_heading(l):
            heads.append((pos, l))
            continue
        m = re.match(r"^(type|price)\s*:", l, re.I)
        if not m or pos == 0:
            continue
        prev_pos = pos - 1
        prev = raw[idx[prev_pos]].strip()
        # `Price:` right under a heading prices the whole section, not a dish; and
        # a `Price:` under a `Type:` belongs to the dish the Type already named.
        if is_heading(prev) or SKIP.match(prev):
            continue
        if PROSE.search(prev) or STARTS_LOWER.match(prev):
            continue
        dishes.append((prev_pos, prev))

    # ── pass 2: a heading with no dish before the next heading is a PARENT ──
    parents: set[int] = set()
    for n, (hpos, _) in enumerate(heads):
        nxt = heads[n + 1][0] if n + 1 < len(heads) else 10 ** 9
        if not any(hpos < dpos < nxt for dpos, _ in dishes):
            parents.add(hpos)

    out = []
    for dpos, dish in dishes:
        above = [h for h in heads if h[0] < dpos]
        if not above:
            continue
        leaf = above[-1][1]
        par = [h for h in above if h[0] in parents]
        parent = par[-1][1] if par else leaf
        out.append((parent, leaf, dish))
    return out


# Section path -> period. The menu's own structure decides this; only the two
# marked NEEDS-HOTEL lines are judgement calls.
def period_for(parent: str, leaf: str, dish: str):
    p, l = parent.upper(), leaf.upper()
    if "BREAKFAST" in (p, l) or l in {"INDIAN DELICACIES", "WESTERN DELICACIES"}:
        return "breakfast", "BREAKFAST section, 'Timing: 7:00 AM - 10:30 AM'"
    if p == "INDIAN SELECTION" and l == "APPETIZERS":
        return "tandoor", "NEEDS HOTEL: under the 'All Tandoori Kebabs' note"
    if l == "BREAD":
        tandoor = {"naan", "tandoori roti", "kulcha"}
        if dish.strip().lower() in tandoor:
            return "tandoor", "NEEDS HOTEL: judged a tandoor bread"
        return "all_day", "NEEDS HOTEL: judged a tawa bread, so unrestricted"
    if p == "MID NIGHT MENU" or l == "MID NIGHT MENU":
        return "all_day", "NEEDS HOTEL: menu states no window for MID NIGHT MENU"
    return "all_day", "no stated timing anywhere in the menu"


def main() -> int:
    items = parse(MENU)
    rows = []
    seen = set()
    for parent, leaf, dish in items:
        key = dish.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        period, why = period_for(parent, leaf, dish)
        rows.append({"dish": dish, "parent": parent, "leaf": leaf,
                     "period": period, "why": why})

    if "--json" in sys.argv:
        block = OrderedDict()
        for r in sorted(rows, key=lambda r: (r["period"], r["dish"])):
            block[r["dish"]] = [r["period"]]
        print(json.dumps(block, indent=6, ensure_ascii=False))
        return 0

    by = OrderedDict()
    for r in rows:
        by.setdefault(r["period"], []).append(r)
    print(f"TOTAL DISHES CLASSIFIED: {len(rows)}\n")
    for period in ("breakfast", "tandoor", "all_day"):
        got = by.get(period, [])
        print(f"=== {period.upper()}  ({len(got)} dishes) ===")
        cur = None
        for r in sorted(got, key=lambda r: (r["parent"], r["leaf"], r["dish"])):
            path = f"{r['parent']} > {r['leaf']}" if r["parent"] != r["leaf"] else r["leaf"]
            if path != cur:
                print(f"  [{path}]")
                cur = path
            print(f"      {r['dish']}")
        print()
    print("=== NEEDS HOTEL SIGN-OFF ===")
    for r in rows:
        if r["why"].startswith("NEEDS HOTEL"):
            print(f"  {r['period']:<10} {r['dish']:<38} {r['why'][12:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
