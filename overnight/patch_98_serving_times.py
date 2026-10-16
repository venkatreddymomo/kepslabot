"""4.2 - menu serving times.

The old rule ("Respect the serving timings stated in the menu") asked the model to
do two jobs: work out which note governs a dish, and compare two windows against
the clock. The clock half now arrives pre-computed as payload.menu_serving_windows,
so the rule points at that instead of asking for arithmetic.

The reading half stays with the model on purpose: each entry carries the note's OWN
wording ("All Tandoori Kebabs..."), so which dishes it covers is read off the menu
rather than from a dish list invented in code.
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

SERVICE_ID = 98

ANCHOR = (
    "- Respect the serving timings stated in the menu (e.g. breakfast hours, "
    "tandoori kebab/bread hours, any midnight menu). If a guest asks for a "
    "time-restricted item outside its hours, tell them the hours from the KB and "
    "offer available alternatives."
)

REPLACEMENT = (
    "- SERVING TIMES ARE ALREADY WORKED OUT FOR YOU. The payload carries "
    "`menu_serving_windows`: one entry per serving-time note in the menu, each with "
    "the note's own wording (`note`), the section it heads (`menu_section`), the "
    "hours (`served`), whether it is being served AT THIS MOMENT (`open_now`) and "
    "when it next opens (`next_opens`). This is computed from the hotel's local "
    "clock every turn. NEVER work out the hours yourself, never compare times in "
    "your head, and never override `open_now` - a wrong answer here has the guest "
    "waiting for food the kitchen is not cooking.\n"
    "- BEFORE you offer, list, recommend or add ANY item, check whether one of "
    "those notes governs it. The note names its own scope in the menu's words (e.g. "
    "\"All Tandoori Kebabs\", \"All Tandoori Breads\", a breakfast section) - decide "
    "from the menu which dishes that scope covers, exactly as the menu is written. "
    "An item that NO note in `menu_serving_windows` covers has no stated timing and "
    "is AVAILABLE NOW - never refuse it and never invent hours for it.\n"
    "- If the governing note has `open_now` false, DO NOT add the item. Say warmly "
    "that it is not available right now, give its hours from `served`, say when it "
    "comes back using `next_opens`, and offer specific alternatives that ARE "
    "available now. If the guest insists, hold the line - the kitchen cannot make "
    "it.\n"
    "- When you show the menu, highlights, a section, or answer \"what's available "
    "now\", show only what is being served at this moment. Leave out anything whose "
    "note has `open_now` false; if the guest asks for that section by name, name it "
    "with its hours instead of listing its dishes.\n"
    "- Some items note advance-preparation time in the KB - that is separate from "
    "serving hours; mention it when the guest adds such an item."
)


def main() -> int:
    url = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    engine = create_engine(url, connect_args={"connect_timeout": 10})

    with engine.begin() as conn:
        prompt = conn.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=:i"),
            {"i": SERVICE_ID}).scalar()
        if prompt is None:
            print("FAIL: service {} not found".format(SERVICE_ID))
            return 1

        found = prompt.count(ANCHOR)
        if found != 1:
            print("FAIL: anchor found {} times (need exactly 1)".format(found))
            return 1

        before = len(prompt)
        prompt = prompt.replace(ANCHOR, REPLACEMENT)
        conn.execute(text(
            "UPDATE new_bot_services SET generated_system_prompt=:p, "
            "generated_system_prompt_override=1 WHERE id=:i"),
            {"p": prompt, "i": SERVICE_ID})
        print("prompt {} -> {} chars".format(before, len(prompt)))

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=:i"), {"i": SERVICE_ID}).first()._mapping
        ok = True
        if ANCHOR in row["generated_system_prompt"]:
            print("VERIFY FAIL: old rule still present")
            ok = False
        if "menu_serving_windows" not in row["generated_system_prompt"]:
            print("VERIFY FAIL: new rule absent")
            ok = False
        if int(row["generated_system_prompt_override"] or 0) != 1:
            print("VERIFY FAIL: override lock is not 1")
            ok = False
        # 4.1 must still be in place - the two patches touch the same row.
        if "ONE QUESTION, ONE YES" not in row["generated_system_prompt"]:
            print("VERIFY FAIL: the 4.1 double-confirm fix was lost")
            ok = False
        print("VERIFIED IN DB (4.1 + 4.2 both present)" if ok else "VERIFY FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
