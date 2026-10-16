"""Tell the service prompt that `order_slot` exists.

The model could emit the dishes but had no way to say WHICH order they belonged
to, so when the guest asked for a separate order it could not express that - it
asked the guest to choose instead, and then wrote nothing at all. The dishes and
the destination now travel together.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
ANCHOR = "Emit the full current order as structured JSON in pending_data_updates.order_items"
ADD = (" AND emit `pending_data_updates.order_slot` alongside it, saying WHICH order "
       "these lines belong to: the `slot` number of an order in `order_book`, or the "
       "string 'new' to open a fresh one. Decide it yourself from what the guest said - "
       "a request for a separate or new order means 'new'; adding to something still "
       "open means that order's slot number. NEVER ask the guest to choose between "
       "joining and starting a new order.")
MARK = "order_slot` alongside it"


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
        print("ABORT: anchor not found")
        return 2
    new = p.replace(ANCHOR, ANCHOR + ADD, 1)
    if dry:
        i = new.find(MARK)
        print("\nWOULD INSERT:\n" + new[i - 120:i + len(ADD)])
        return 0
    (ROOT / "backups" / "svc98_before_order_slot.txt").write_text(p, encoding="utf-8")
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
