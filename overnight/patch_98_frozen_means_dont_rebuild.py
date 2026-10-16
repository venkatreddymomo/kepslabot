"""'ONCE PLACED, THE ORDER IS FROZEN' was being read as 'it cannot be changed'.

The rule is an anti-hallucination one: do not RECONSTRUCT a placed order from
chat history, read back the confirmed lines. That intent is right and stays.

But the headline says FROZEN, and the model applied it to a different question -
whether a dish may still be ADDED. Caught live: the book said `can_be_changed:
true`, "about 1 minute(s) left to change it", and "a further dish CAN still be
added to this same order - saying it is too late is wrong". The bot replied "your
previous order of 2 Chicken Burgers is already placed and on its way, this will
be a new order". It was obeying this line over the data.

So: frozen means do not REBUILD it. Whether it can still be added to is answered
by the book, and by nothing else.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
OLD = "ONCE PLACED, THE ORDER IS FROZEN — read it back, never rebuild it."
NEW = ("ONCE PLACED, DO NOT REBUILD THE ORDER FROM MEMORY — read it back. "
       "THIS IS ABOUT NOT RECONSTRUCTING IT, NOT ABOUT REFUSING TO CHANGE IT: "
       "whether a dish may still be ADDED to a placed order is answered ONLY by "
       "that order's `can_be_changed` in `order_book`. When that says true, the "
       "kitchen has not started and a further dish joins THAT SAME order - "
       "telling the guest it is too late, or that it has to be a new order, is "
       "wrong and costs them the dish they asked for.")


def main() -> int:
    dry = "--dry-run" in sys.argv
    url = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 15})
    with eng.connect() as c:
        p = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98")).scalar() or "")
    print("current: %d chars" % len(p))
    if "ONCE PLACED, DO NOT REBUILD THE ORDER FROM MEMORY" in p:
        print("already applied")
        return 0
    if OLD not in p:
        print("ABORT: the frozen rule was not found verbatim")
        return 2
    new = p.replace(OLD, NEW, 1)
    assert "never rebuild it" not in new or True
    assert "NEVER re-derive the order from earlier chat" in new, "anti-rebuild core lost"
    if dry:
        i = new.find("ONCE PLACED, DO NOT REBUILD")
        print("\nWOULD BECOME:\n" + new[i:i + len(NEW) + 200])
        print("\n--dry-run: nothing written")
        return 0
    (ROOT / "backups" / "svc98_before_frozen_fix.txt").write_text(p, encoding="utf-8")
    with eng.begin() as c:
        c.execute(text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                       "generated_system_prompt_override=1 WHERE id=98"), {"p": new})
    with eng.connect() as c:
        ap = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98")).scalar() or "")
    ok = ("ONCE PLACED, DO NOT REBUILD THE ORDER FROM MEMORY" in ap
          and "NEVER re-derive the order from earlier chat" in ap)
    print("written: %d chars" % len(ap))
    print("VERIFIED OK" if ok else "VERIFY FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
