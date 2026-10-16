"""The in-room dining agent's knowledge base should be the menu, not the rooms.

Service 98's extracted_knowledge is 41,118 chars and STARTS with Suite Room,
Executive Room, Standard Room and Specially Abled Room - bed configurations,
floor areas, Wi-Fi instructions - 4,804 characters of it before the menu. None of
it helps take a food order, and all of it competes with the menu and the live
order state for the model's attention.

The dining-relevant sections stay: the outlets, room service hours, the outside
food policy, complimentary in-room items, and the menu itself.

    python overnight/patch_98_kb_menu_only.py --dry-run
    python overnight/patch_98_kb_menu_only.py
"""
import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
DROP = ("## Suite Room", "## Executive Room", "## Standard Room",
        "## Specially Abled Room")


def main() -> int:
    dry = "--dry-run" in sys.argv
    url = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 15})
    with eng.connect() as c:
        raw = c.execute(text(
            "SELECT service_prompt_pack FROM new_bot_services WHERE id=98")).scalar()
    pack = json.loads(raw) if isinstance(raw, str) else (raw or {})
    ek = str(pack.get("extracted_knowledge") or "")
    print("extracted_knowledge: %d chars" % len(ek))
    if not ek:
        print("ABORT: no extracted_knowledge")
        return 2
    if not any(h in ek for h in DROP):
        print("already menu-only")
        return 0

    heads = [(m.start(), m.group(0).strip()) for m in re.finditer(r"(?m)^##+ .+$", ek)]
    heads.append((len(ek), "<end>"))
    keep_parts, removed = [], []
    for i in range(len(heads) - 1):
        a, name = heads[i]
        b = heads[i + 1][0]
        (removed if name in DROP else keep_parts).append((name, ek[a:b]))
    if heads and heads[0][0] > 0:                      # anything before the first heading
        keep_parts.insert(0, ("<preamble>", ek[:heads[0][0]]))

    new_ek = "".join(part for _n, part in keep_parts).strip()
    print("\nREMOVING:")
    for n, part in removed:
        print("  %-30s %6d chars" % (n, len(part)))
    print("KEEPING:")
    for n, part in keep_parts:
        print("  %-30s %6d chars" % (n, len(part)))
    print("\n%d -> %d chars" % (len(ek), len(new_ek)))

    # The menu is the whole point - refuse to write a version without it.
    assert "IN ROOM DINING MENU" in new_ek, "the menu would be lost"
    assert len(new_ek) > 30000, "suspiciously small result"

    if dry:
        print("\n--dry-run: nothing written")
        return 0

    (ROOT / "backups" / "svc98_prompt_pack_before_menu_only.json").write_text(
        json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    pack["extracted_knowledge"] = new_ek
    with eng.begin() as c:
        c.execute(text("UPDATE new_bot_services SET service_prompt_pack=:p WHERE id=98"),
                  {"p": json.dumps(pack, ensure_ascii=False)})
    with eng.connect() as c:
        raw2 = c.execute(text(
            "SELECT service_prompt_pack FROM new_bot_services WHERE id=98")).scalar()
    p2 = json.loads(raw2) if isinstance(raw2, str) else (raw2 or {})
    ek2 = str(p2.get("extracted_knowledge") or "")
    ok = ("IN ROOM DINING MENU" in ek2 and not any(h in ek2 for h in DROP))
    print("\nwritten: %d chars" % len(ek2))
    print("VERIFIED OK" if ok else "VERIFY FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
