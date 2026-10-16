"""Teach service 98 to read the ABSENCE of a serving-time line as "no restriction".

The bot keeps inventing breakfast hours for Filter Coffee - an all-day drink that
is on no closed list and carries no serving-time line. Twice observed, and on the
same build that answered it correctly four minutes earlier, so it is variance
rather than bad data. The likely pull is the BREAKFAST combos, which literally
list "Tea / Coffee" among their contents and now carry a NOT AVAILABLE stamp.

Every menu item the model reads either has a `Serving time:` line or does not, so
this rule is checkable against the text in front of it - a far easier ask than
inferring a restriction from something's absence from a separate list.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
ANCHOR = "- `menu_availability` IS NOT THE MENU."

NEW = """- NO `Serving time:` LINE MEANS NO RESTRICTION AT ALL. Every restricted dish in the menu carries a `Serving time:` line of its own, right under its price, saying AVAILABLE NOW or NOT AVAILABLE NOW. A dish WITHOUT that line has no serving hours whatsoever and can be ordered at any hour the kitchen is open. NEVER state hours for such a dish, never say it is unavailable, and never borrow hours from a nearby section, a combo that happens to mention it, or a similarly-named dish. If you are about to tell a guest a dish is unavailable, first check that the dish has a `Serving time:` line saying so - if it has none, it is available and you simply take the order.
"""


def main() -> int:
    url = [
        l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
        if l.startswith("DATABASE_URL=")
    ][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 10})

    with eng.connect() as c:
        prompt = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98"
        )).scalar() or "")
    print(f"current prompt: {len(prompt)} chars")

    if "NO `Serving time:` LINE MEANS NO RESTRICTION" in prompt:
        print("already applied - nothing to do")
        return 0
    i = prompt.find(ANCHOR)
    if i < 0:
        print(f"ABORT: anchor not found: {ANCHOR!r}")
        return 2

    new_prompt = prompt[:i] + NEW + prompt[i:]
    assert "NO `Serving time:` LINE MEANS NO RESTRICTION" in new_prompt
    assert ANCHOR in new_prompt
    assert len(new_prompt) > len(prompt)

    with eng.begin() as c:
        c.execute(
            text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                 "generated_system_prompt_override=1 WHERE id=98"),
            {"p": new_prompt},
        )
    with eng.connect() as c:
        row = dict(c.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=98")).first()._mapping)
    ap = str(row["generated_system_prompt"] or "")
    ok = ("NO `Serving time:` LINE MEANS NO RESTRICTION" in ap
          and int(row["generated_system_prompt_override"] or 0) == 1)
    print(f"written: {len(ap)} chars, LOCK={row['generated_system_prompt_override']}")
    print("VERIFIED OK" if ok else "VERIFY FAILED")
    print("\nRESTART the backend.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
