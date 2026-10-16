"""Point service 98's serving-times block at the new per-dish `menu_availability`.

The old block handed the model three SECTION headings and told it to "decide from
the menu which dishes that scope covers" - over a 41k-char menu whose timing notes
never say which dishes they mean. That is the guessing the table removes, so the
instruction to guess has to go with it.

What replaces it says less, not more: read a list of names, do no arithmetic, and
never turn "not right now" into "we don't have it".

Asserts the anchor was found and that the override LOCK stays set, because a patch
that silently changes nothing has cost real time here before.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "backups" / "svc98_20260826_132612" / "service_98_prompt.txt"

OLD_START = "- SERVING TIMES ARE ALREADY WORKED OUT FOR YOU."
OLD_END = "- Some items note advance-preparation time in the KB — mention it when the guest adds such an item.\n"

NEW = """- WHAT CAN BE ORDERED RIGHT NOW IS ALREADY DECIDED FOR YOU. The payload carries `menu_availability`, worked out per dish from the hotel's local clock every turn. It has `as_of` (the time it was computed), a `mode`, a list of `items`, and a `rule`. NEVER work out serving hours yourself, never compare times in your head, and never overrule it. You do no timing reasoning at all - you read names off a list.
- THE THREE MODES:
  - `these_are_NOT_available_now` - every dish named in `items` is closed right now; each carries `served` (its hours) and `returns` (when it comes back). EVERY OTHER DISH ON THE MENU IS AVAILABLE. This is the normal case and the list is short.
  - `ONLY_these_are_available_now` - the kitchen is on its limited overnight menu; only the named dishes can be ordered right now.
  - `everything_is_available_now` - nothing is restricted; never refuse anything on timing grounds this turn.
  - If `menu_availability` is absent entirely, treat the whole menu as available.
- `menu_availability` IS NOT THE MENU. It only ever answers "can this be ordered at this moment". It never says what the hotel serves. The menu above is the only thing that says that. Never treat a short availability list as though it were the full menu, and never conclude from it that the kitchen is shut, the menu is unavailable, or the guest must wait for everything.
- NEVER TURN "NOT RIGHT NOW" INTO "WE DON'T HAVE IT". These are two different answers and confusing them tells a guest the hotel does not serve a dish it is proud of:
  - Dish on the menu but closed right now -> say we DO serve it, give its hours from `served`, say when it returns using `returns`, and offer specific alternatives that are available now. Example: "We do serve Idli - it's available 7:00 AM-10:30 AM, so it'll be back tomorrow at 7:00 AM. Can I get you something else in the meantime?"
  - Dish genuinely not on the menu at all -> that, and only that, is when you say we don't have it.
  Never answer the first with the wording of the second. If the guest insists on a closed dish, stay warm but hold the line - the kitchen cannot make it now.
- Do NOT add a closed dish to the order. Anything else the guest asked for on the same turn still stands - one closed dish never throws away the rest of an order.
- When you show the menu, highlights, a section, or answer "what's available now", leave out dishes that are closed right now. If the guest asks for one by name, name it with its hours rather than pretending it does not exist.
- Some items note advance-preparation time in the KB - that is separate from serving hours; mention it when the guest adds such an item.
"""


def main() -> int:
    url = [
        l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
        if l.startswith("DATABASE_URL=")
    ][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 10})

    with eng.connect() as c:
        row = dict(c.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=98")).first()._mapping)
    prompt = str(row["generated_system_prompt"] or "")
    lock = row["generated_system_prompt_override"]
    print(f"current prompt: {len(prompt)} chars, override LOCK={lock}")

    if not BACKUP.exists():
        print(f"ABORT: backup missing at {BACKUP}")
        return 2

    if "menu_availability" in prompt:
        print("ABORT: prompt already mentions menu_availability - nothing to do")
        return 0

    i = prompt.find(OLD_START)
    j = prompt.find(OLD_END)
    if i < 0:
        print(f"ABORT: anchor not found: {OLD_START!r}")
        return 2
    if j < 0 or j < i:
        print(f"ABORT: end anchor not found after start: {OLD_END!r}")
        return 2
    j += len(OLD_END)

    replaced = prompt[i:j]
    new_prompt = prompt[:i] + NEW + prompt[j:]
    print(f"replacing {len(replaced)} chars with {len(NEW)} chars")

    # Prove the swap did what it claims before writing anything.
    assert "menu_serving_windows" not in new_prompt, "old field name survived"
    assert "menu_timing_note" not in new_prompt, "old note field survived"
    assert "menu_availability" in new_prompt, "new field name missing"
    assert "NEVER TURN \"NOT RIGHT NOW\" INTO \"WE DON'T HAVE IT\"" in new_prompt
    assert len(new_prompt) > 15000, "prompt got suspiciously short"

    with eng.begin() as c:
        c.execute(
            text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                 "generated_system_prompt_override=1 WHERE id=98"),
            {"p": new_prompt},
        )

    with eng.connect() as c:
        after = dict(c.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=98")).first()._mapping)
    ap = str(after["generated_system_prompt"] or "")
    print(f"written: {len(ap)} chars, override LOCK={after['generated_system_prompt_override']}")
    ok = (
        "menu_availability" in ap
        and "menu_serving_windows" not in ap
        and int(after["generated_system_prompt_override"] or 0) == 1
    )
    print("VERIFIED OK" if ok else "VERIFY FAILED")
    print("\nRESTART the backend - services are cached per hotel_code.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
