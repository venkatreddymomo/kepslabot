"""Give the guest's choice of version a place on the order line.

Menu entries like "Phulka (Plain or Ghee)", "Naan (Plain, Butter, Garlic)" and
"Kulcha (Aloo, Paneer, Onion)" are ONE item with options. The model writes the
canonical menu name in `order_items`, so the option the guest actually chose was
never recorded: the reply said "2 x Phulka (Ghee)" and the kitchen ticket said
"Phulka". The guest gets plain bread.

Not a prompt scold - there was genuinely nowhere to put it. `variant` is that
place, and the ticket prints it.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
ANCHOR = ("(price = the per-item ₹ from the menu; allergens = exactly as the menu "
          "lists, or \"none\")")
ADD = (". When the dish comes in versions - Phulka (Plain or Ghee), Naan (Plain, "
       "Butter or Garlic), Kulcha (Aloo, Paneer or Onion), Tandoori Roti (Plain or "
       "Butter), Soup of the Day (veg or non-veg) - add a `variant` field on that "
       "line holding the one the guest chose, e.g. {\"name\":\"Phulka\",\"qty\":2,"
       "\"price\":225,\"variant\":\"Ghee\"}. The kitchen ticket is printed from these "
       "lines, so a missing variant means the guest is sent the plain version of "
       "something they asked for specifically. If they have not said which, ask "
       "before adding it")
MARK = "add a `variant` field on that line"


def main() -> int:
    dry = "--dry-run" in sys.argv
    url = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 15})
    with eng.connect() as c:
        p = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98")).scalar() or "")
    print("current: %d chars" % len(p))
    if MARK in p:
        print("already applied")
        return 0
    if ANCHOR not in p:
        print("ABORT: anchor not found verbatim")
        return 2
    new = p.replace(ANCHOR, ANCHOR + ADD, 1)
    if dry:
        i = new.find(MARK)
        print("\nWOULD INSERT:\n" + new[i - 150:i + len(ADD)])
        return 0
    (ROOT / "backups" / "svc98_before_variant.txt").write_text(p, encoding="utf-8")
    with eng.begin() as c:
        c.execute(text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                       "generated_system_prompt_override=1 WHERE id=98"), {"p": new})
    with eng.connect() as c:
        ap = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98")).scalar() or "")
    print("written: %d chars" % len(ap))
    print("VERIFIED OK" if MARK in ap else "VERIFY FAILED")
    return 0 if MARK in ap else 1


if __name__ == "__main__":
    sys.exit(main())
