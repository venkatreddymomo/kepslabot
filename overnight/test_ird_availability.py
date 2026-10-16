"""Prove the per-dish availability table against the REAL menu's stated timings.

The old resolver handed the model 3 section headings for a 152-dish menu and left
it to map dishes to sections over 41k chars. These cases pin the two failures that
came from that - Idli losing its BREAKFAST window through a sub-section, and the
whole INDIAN SELECTION reading as shut because of a kebab-only note - plus the
rule that matters most: a closed dish is still a dish we serve.
"""
import io
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from services import ird_availability as A

H = "sarovar_portico_sriperumbudur"
fails = 0


def at(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 8, 26, int(h), int(m))


def check(label: str, got, want) -> None:
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"       want {want!r}\n       got  {got!r}")


def avail(item: str, hhmm: str) -> bool:
    return A.item_status(H, item, at(hhmm))["available"]


print("=== the bug that started this: a breakfast dish in a SUB-section ===")
# Idli sits in INDIAN DELICACIES, whose parent heading is BREAKFAST. The old
# flat resolver lost that link entirely.
check("Idli at 08:19 (guest's real order time)", avail("Idli", "08:19"), True)
check("Idli at 06:59 (one minute early)", avail("Idli", "06:59"), False)
check("Idli at 07:00 (window opens)", avail("Idli", "07:00"), True)
check("Idli at 10:29 (last minute)", avail("Idli", "10:29"), True)
check("Idli at 10:30 (window shuts)", avail("Idli", "10:30"), False)
check("Idli at 23:00", avail("Idli", "23:00"), False)
check("Medu Vada at 20:00", avail("Medu Vada", "20:00"), False)
check("Continental Breakfast at 13:00", avail("Continental Breakfast", "13:00"), False)

print("\n=== the over-restriction: all-day dishes near a timed note ===")
# These sit inside / next to INDIAN SELECTION, which the old resolver reported
# CLOSED as a whole because of its Tandoori-Kebab-only note.
for dish in ("Dal Tadka", "Vegetable Dum Biryani", "Chicken Burger",
             "Arrabbiata", "Tiramisu", "Filter Coffee (South Indian Style)"):
    check(f"{dish} at 08:00 (no stated timing -> always open)", avail(dish, "08:00"), True)
    check(f"{dish} at 23:30", avail(dish, "23:30"), True)

print("\n=== tandoor: the 5:36 PM regression ===")
check("Tandoori Murgh at 17:36 (the original bug)", avail("Tandoori Murgh", "17:36"), False)
check("Tandoori Murgh at 13:00", avail("Tandoori Murgh", "13:00"), True)
check("Tandoori Murgh at 19:30", avail("Tandoori Murgh", "19:30"), True)
check("Tandoori Murgh at 23:30", avail("Tandoori Murgh", "23:30"), False)
check("Jaitooni Paneer Tikka at 17:36 (no 'tandoori' in the name)",
      avail("Jaitooni Paneer Tikka", "17:36"), False)
check("Naan at 17:36 (tandoor bread, name says nothing)", avail("Naan", "17:36"), False)
check("Kulcha at 13:00", avail("Kulcha", "13:00"), True)

print("\n=== tawa breads are deliberately NOT restricted (fail open) ===")
for dish in ("Phulka", "Kerala Paratha"):
    check(f"{dish} at 17:36 (tawa, left out of the table)", avail(dish, "17:36"), True)

print("\n=== name matching ===")
check("'idli' lowercase", avail("idli", "23:00"), False)
check("'  IDLI  ' padded/upper", avail("  IDLI  ", "23:00"), False)
check("full menu name (what the model declares)", avail("Dosa (Plain or Masala)", "23:00"), False)
check("model's echo with quantity words", avail("Dosa Plain or Masala", "23:00"), False)
check("'Bakers Basket' vs \"Baker's Basket\"", avail("Bakers Basket", "23:00"), False)
# ONE-WORD PARTIALS FAIL OPEN, ON PURPOSE. The table lists restricted dishes only,
# so it cannot see that a bare "paratha" might mean the all-day Kerala Paratha, or
# "dosa" some other dosa - refusing on that would deny food the kitchen would cook.
# The model resolves these from the closed list, which names each dish in full.
check("bare 'paratha' -> available (cannot tell which paratha)", avail("paratha", "23:00"), True)
check("bare 'dosa' -> available (same reason)", avail("dosa", "23:00"), True)
# Two words IS enough to identify one dish.
check("'aloo paratha' -> the breakfast paratha, refused", avail("aloo paratha", "23:00"), False)
check("'medu vada' exact", avail("medu vada", "23:00"), False)
check("unknown dish -> available", avail("Chicken Tikka Masala Pizza Thing", "23:00"), True)
check("empty name -> available", avail("", "23:00"), True)
check("junk name -> available", avail("!!!", "23:00"), True)

print("\n=== the refusal carries hours + a return time ===")
st = A.item_status(H, "Idli", at("20:00"))
check("Idli 20:00 served window", st["served"], "7:00 AM-10:30 AM")
check("Idli 20:00 next opens", st["next_opens"], "tomorrow at 7:00 AM")
st = A.item_status(H, "Tandoori Murgh", at("17:36"))
check("Tandoori Murgh 17:36 served", st["served"], "12:00 PM-3:00 PM and 7:00 PM-11:00 PM")
check("Tandoori Murgh 17:36 next opens", st["next_opens"], "today at 7:00 PM")

print("\n=== the payload block ===")
b = A.build_availability_block(H, at("08:19"))
check("08:19 mode", b.get("mode"), "these_are_NOT_available_now")
names = [r["item"] for r in b["items"]]
check("08:19 lists the 9 tandoor dishes", len(names), 9)
check("08:19 does NOT list Idli (it is open)", "Idli" in names, False)
check("08:19 lists Tandoori Murgh", "Tandoori Murgh" in names, True)

b = A.build_availability_block(H, at("13:00"))
names = [r["item"] for r in b["items"]]
check("13:00 lists the 18 breakfast dishes", len(names), 18)
check("13:00 lists Idli", "Idli" in names, True)
check("13:00 does NOT list Tandoori Murgh", "Tandoori Murgh" in names, False)

b = A.build_availability_block(H, at("17:36"))
check("17:36 lists breakfast + tandoor", len(b["items"]), 27)

# THE COUNTABLE LIST. Absence from the closed list was too weak a signal - the
# bot invented breakfast hours for an all-day Filter Coffee five runs running.
# Naming every restricted dish makes "not restricted" a lookup, not a deduction.
# RESTRICTED IS NOT THE SAME AS SHUT. Listing bare names made the model treat
# every named dish as closed - it recited hours for a Tandoori Murgh at 13:37,
# inside its own 12-3 window. Each entry now carries its verdict.
for _hh, _open_murgh in (("08:19", False), ("13:00", True), ("17:36", False)):
    _b = A.build_availability_block(H, at(_hh))
    _all = _b.get("every_dish_that_has_any_serving_restriction") or []
    _by = {r["item"]: r for r in _all}
    check(f"{_hh}: all 27 restricted dishes listed", len(_all), 27)
    check(f"{_hh}: Filter Coffee is NOT among them",
          any("filter coffee" in d.lower() for d in _by), False)
    check(f"{_hh}: Tandoori Murgh available_now", _by["Tandoori Murgh"]["available_now"], _open_murgh)
    check(f"{_hh}: an OPEN entry carries no hours to recite",
          "served" in _by["Tandoori Murgh"], not _open_murgh)
    check(f"{_hh}: rule says being listed does not mean unavailable",
          "does NOT mean" in _b.get("rule", ""), True)

# The block must always carry the never-say-we-don't-have-it rule.
for hhmm in ("08:19", "13:00", "17:36", "23:30"):
    blk = A.build_availability_block(H, at(hhmm))
    ok = "NEVER say we do" in blk.get("rule", "")
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {hhmm}: block carries the 'never deny a real dish' rule")

print("\n=== inverted mode (dormant in prod until the hotel confirms) ===")
_real = A._hotel_table
A._hotel_table = lambda hc: {
    "periods": {"all_day": [["06:00", "23:00"]], "late_night": [["23:00", "06:00"]]},
    "items": {"Chicken Dum Biryani": ["all_day", "late_night"],
              "Rasmalai": ["all_day", "late_night"],
              "Tandoori Murgh": ["all_day"]},
}
try:
    b = A.build_availability_block("x", at("02:15"))
    check("02:15 flips to the short OPEN list", b.get("mode"), "ONLY_these_are_available_now")
    check("02:15 items", b.get("items"), ["Chicken Dum Biryani", "Rasmalai"])
    check("02:15 rule still forbids denying a dish", "NEVER say we do" in b.get("rule", ""), True)
    # At 14:00 every dish in that synthetic table is open, so there is nothing to
    # say and the block is omitted entirely - which leaves the whole menu available.
    check("14:00 nothing is shut -> no block at all",
          A.build_availability_block("x", at("14:00")), {})
finally:
    A._hotel_table = _real

print("\n=== the menu annotation ===")
try:
    _menu = open("overnight/ird_menu_dump.txt", encoding="utf-8").read()
except OSError:
    print("SKIP  (overnight/ird_menu_dump.txt not present)")
else:
    ann = A.annotate_menu(H, _menu, at("13:46"))
    check("30 dishes annotated (18 breakfast + 9 tandoor + 3 affirmed all-day)", ann.count("Serving time for "), 30)
    check("the menu only grew by the annotations", len(ann) > len(_menu), True)

    import re as _re

    _SKIP = _re.compile(r"^(price|type|calories|allergens|serving size)\s*:", _re.I)
    _lines = ann.split("\n")
    _named = set()
    for _i, _l in enumerate(_lines):
        if _l.startswith("Serving time for "):
            _j = _i - 1
            while _j > 0 and (not _lines[_j].strip() or _SKIP.match(_lines[_j].strip())):
                _j -= 1
            _named.add(_lines[_j].strip())
    for _d in ("Idli", "Tandoori Murgh", "Naan", "Continental Breakfast", "Kulcha"):
        check(f"{_d} carries its hours", _d in _named, True)
    # Everything else is visibly unmarked. Filter Coffee is the dish the bot
    # invented breakfast hours for, and unmarked is what finally stopped it.
    for _d in ("Filter Coffee (South Indian Style)", "Chicken Burger", "Tiramisu"):
        check(f"{_d} carries NO serving line", _d in _named, False)

    # THE TAWA BREADS ARE THE EXCEPTION, and are listed on purpose. Unmarked, they
    # inherited their neighbours' hours: "Kerala Paratha is served from 7:00 AM to
    # 10:30 AM" (those belong to the breakfast Paratha, which shares the word) and
    # "Phulka is not currently available as a menu item" (it is on the menu). They
    # sit in BREAD beside three tandoor breads that ARE restricted, so silence was
    # not enough - each now says out loud that it has no restriction.
    def _serving_line(dish: str) -> str:
        _a = ann.split("\n")
        for _i, _l in enumerate(_a):
            if _l.strip() == dish:
                for _x in _a[_i:_i + 8]:
                    if _x.startswith("Serving time for "):
                        return _x
        return ""

    for _d in ("Phulka", "Kerala Paratha", "Lachha Paratha"):
        check(f"{_d} is affirmed AVAILABLE ALL DAY", _d in _named, True)
        _ln = _serving_line(_d)
        check(f"{_d} says no restriction", "no serving restriction" in _ln, True)
        check(f"{_d} quotes NO hours to be misread", bool(_re.search(r"\d{1,2}:\d{2}", _ln)), False)
    # ...while the breakfast paratha it collides with still says the opposite.
    check("the breakfast Paratha is still restricted",
          "NOT AVAILABLE NOW" in _serving_line("Paratha (Aloo or Paneer)"), True)
    check("nothing else was annotated", len(_named), 30)

    # NO CLOCK ARITHMETIC MAY SURVIVE IN THE MENU TEXT. The menu's own prose notes
    # ("All Tandoori Kebabs will be available from 12 noon to 3 pm and 7 pm to 11
    # pm") were the last thing inviting the model to compare a window to the clock,
    # and it did: at 14:07, with the dish's own line reading AVAILABLE NOW, it
    # recited the note and told the guest the kebab was back at 7 PM.
    # The breakfast combos' contents end "Tea / Coffee", a dozen lines under a
    # NOT-AVAILABLE-until-7am stamp. That pairing had the bot quote breakfast
    # hours for an all-day Filter Coffee. The list now says what it is.
    check("all 5 Includes lists labelled as set components",
          ann.count("the components of this set"), 5)
    check("all 4 prose timing notes replaced",
          ann.count("(Serving times are shown on each dish below.)"), 4)
    _RANGE = _re.compile(
        r"\d{1,2}(:\d{2})?\s*(am|pm|noon)?\s*(?:[-‐-―]|to|till|until)\s*"
        r"\d{1,2}(:\d{2})?\s*(am|pm|noon)", _re.I)
    _left = [l for l in ann.split("\n")
             if _re.search(r"\b(timing|available)\b", l, _re.I)
             and _RANGE.search(l) and not l.startswith("Serving time for ")]
    check("no prose timing line survives outside the per-dish lines", len(_left), 0)

    # THE VERDICT IS ON THE LINE, NOT LEFT TO THE MODEL. Bare hours had it doing
    # its own clock comparison: at 13:46 it read "12:00 PM-3:00 PM and 7:00 PM-
    # 11:00 PM" off a Tandoori Murgh - a window it was INSIDE - and refused it.
    def line_for(dish: str, hhmm: str) -> str:
        a = A.annotate_menu(H, _menu, at(hhmm)).split("\n")
        for _i, _l in enumerate(a):
            if _l.strip() == dish:
                for _x in a[_i:_i + 8]:
                    if _x.startswith("Serving time for "):
                        return _x
        return ""

    # An OPEN dish carries no hours - nothing for the model to narrate. Printing
    # them let it recite the wrong window of two and imply the dish was shut.
    check("Tandoori Murgh at 13:46 (inside 12-3) says only AVAILABLE NOW",
          line_for("Tandoori Murgh", "13:46"), "Serving time for Tandoori Murgh: AVAILABLE NOW")
    check("Continental Breakfast at 08:19 says only AVAILABLE NOW",
          line_for("Continental Breakfast", "08:19"), "Serving time for Continental Breakfast: AVAILABLE NOW")
    # A CLOSED dish carries the hours and the return time - that is the whole
    # explanation the guest is owed.
    check("Tandoori Murgh at 17:36 explains itself",
          line_for("Tandoori Murgh", "17:36"),
          "Serving time for Tandoori Murgh: NOT AVAILABLE NOW - served 12:00 PM-3:00 PM and 7:00 PM-11:00 PM, back today at 7:00 PM")
    check("Naan at 08:19 explains itself",
          line_for("Naan", "08:19"),
          "Serving time for Naan: NOT AVAILABLE NOW - served 12:00 PM-3:00 PM and 7:00 PM-11:00 PM, back today at 12:00 PM")
    check("Continental Breakfast at 13:46 explains itself",
          line_for("Continental Breakfast", "13:46"),
          "Serving time for Continental Breakfast: NOT AVAILABLE NOW - served 7:00 AM-10:30 AM, back tomorrow at 7:00 AM")
    # The menu line and the availability list are built from one table and one
    # clock, so they must never contradict each other.
    for _hh in ("08:19", "13:46", "17:36", "23:30"):
        _blk = A.build_availability_block(H, at(_hh))
        _shut = {r["item"] for r in (_blk.get("items") or [])}
        _bad = [d for d in _named
                if (("NOT AVAILABLE NOW" in line_for(d, _hh)) != (d in _shut))]
        fails += 0 if not _bad else 1
        print(f"{'PASS' if not _bad else 'FAIL'}  {_hh}: menu line agrees with the list"
              + (f" -- disagreed on {_bad[:3]}" if _bad else ""))
    check("unknown hotel -> menu untouched", A.annotate_menu("nope", _menu, at("13:46")), _menu)
    check("empty menu -> empty", A.annotate_menu(H, "", at("13:46")), "")

print("\n=== fail open ===")
check("unknown hotel -> no block", A.build_availability_block("nope", at("13:00")), {})
check("unknown hotel -> dish available", A.item_status("nope", "Idli", at("23:00"))["available"], True)

print("\n" + ("ALL PASS" if not fails else f"{fails} FAILED"))
sys.exit(1 if fails else 0)
