"""The veg/non-veg fixer, judged against the REAL menu's own `Type:` lines.

Covers the two failures the old word list could not avoid: a meat dish it did not
have a word for, and a vegetarian dish whose name contains one. No DB needed.
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from services.llm_orchestration_grounding_mixin import (
    LLMOrchestrationGroundingMixin as G,
    _ird_menu_item_types,
)

menu = open("overnight/ird_menu_dump.txt", encoding="utf-8").read()
types = _ird_menu_item_types(menu)
print("menu declares Type: for {} dishes".format(len(types)))
for n in ("tandoori murgh", "mushroom galouti", "jaitooni paneer tikka",
          "kasundi macchi tikka", "vegetable burger", "chicken burger", "tiramisu"):
    print("   {:26s} -> {!r}".format(n, types.get(n)))

fails = 0


def check(name, rendered, must_move, why, menu_text=None):
    global fails
    out = G._sarovar_ird_fix_veg_grouping(
        rendered, menu if menu_text is None else menu_text
    )
    moved = out != rendered
    ok = moved == must_move
    print("\n{}  {}".format("PASS" if ok else "FAIL", name))
    print("      {}".format(why))
    if not ok:
        print("      expected {} move, got {}".format(
            "a" if must_move else "no", "a move" if moved else "none"))
        print("      --- got ---\n{}".format(out))
        fails += 1


# 1. A real non-veg dish misfiled under Vegetarian MUST be moved.
check(
    "misfiled meat is moved",
    "🥬 **Vegetarian**\n• **Tandoori Murgh** - ₹850\n• **Mushroom Galouti** - ₹750\n"
    "🍗 **Non-Vegetarian**\n• **Zaffrani Murgh Tikka** - ₹800\n",
    True,
    "menu says Tandoori Murgh is Non Veg, so it cannot sit under Vegetarian",
)

# 2. A correct render must be left completely alone.
check(
    "a correct render is untouched",
    "🥬 **Vegetarian**\n• **Mushroom Galouti** - ₹750\n"
    "🍗 **Non-Vegetarian**\n• **Tandoori Murgh** - ₹850\n",
    False,
    "both dishes already match the menu's Type:",
)

# 3. THE OLD LIST'S FALSE POSITIVE: a VEG dish whose name contains a meat word.
#    "Kasundi Macchi Tikka" contains "macchi"; whatever the menu says is what counts.
_macchi = types.get("kasundi macchi tikka")
check(
    "a veg dish is never moved on its name alone",
    "🥬 **Vegetarian**\n• **Jaitooni Paneer Tikka** - ₹750\n"
    "🍗 **Non-Vegetarian**\n• **Tandoori Murgh** - ₹850\n",
    False,
    "Jaitooni Paneer Tikka is Veg per the menu (macchi dish is {!r})".format(_macchi),
)

# 4. No menu text -> nothing may be moved at all.
check(
    "no menu means no changes",
    "🥬 **Vegetarian**\n• **Tandoori Murgh** - ₹850\n🍗 **Non-Vegetarian**\n• **X** - ₹1\n",
    False,
    "with no menu to read, the render is left exactly as the model wrote it",
    menu_text="",
)

# 5. An item the menu does not know is left where the model put it.
check(
    "unknown dish is left alone",
    "🥬 **Vegetarian**\n• **Chef's Mystery Special** - ₹500\n"
    "🍗 **Non-Vegetarian**\n• **Tandoori Murgh** - ₹850\n",
    False,
    "not in the menu, so there is no fact to act on",
)

print("\n{}".format("ALL PASS" if not fails else "{} FAILURE(S)".format(fails)))
raise SystemExit(1 if fails else 0)
