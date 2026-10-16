"""Render the full menu classification as a review sheet the hotel can sign off."""
import datetime
import importlib.util
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

spec = importlib.util.spec_from_file_location("c", "overnight/classify_menu.py")
C = importlib.util.module_from_spec(spec)
sys.argv = ["classify_menu.py", "--json"]      # keeps its main() from printing
try:
    spec.loader.exec_module(C)
except SystemExit:
    pass

from services import ird_availability as A

H = "sarovar_portico_sriperumbudur"
rows, seen = [], set()
for parent, leaf, dish in C.parse("overnight/ird_menu_dump.txt"):
    k = dish.strip().lower()
    if k in seen:
        continue
    seen.add(k)
    period, why = C.period_for(parent, leaf, dish)
    rows.append({"dish": dish, "parent": parent, "leaf": leaf, "period": period, "why": why})

per = {}
for r in rows:
    per.setdefault(r["period"], []).append(r)

L = []
w = L.append
w("# In-Room Dining — full menu availability classification\n")
w(f"**Sarovar Portico Sriperumbudur · {len(rows)} dishes · generated {datetime.date.today()}**\n")
w("Every dish on the menu, classified by when it is served. Produced by walking the")
w("menu itself (`overnight/classify_menu.py`), so it can be regenerated whenever the")
w("menu changes rather than maintained by hand.\n")
w("This is what `config/ird_serving_windows.json` encodes and what the bot reads.\n")
w("---\n")
w("## The three periods\n")
w("| period | hours | dishes |")
w("|---|---|---|")
w(f"| `breakfast` | 07:00 – 10:30 | **{len(per.get('breakfast', []))}** |")
w(f"| `tandoor` | 12:00 – 15:00 and 19:00 – 23:00 | **{len(per.get('tandoor', []))}** |")
w(f"| `all_day` | any hour | **{len(per.get('all_day', []))}** |")
w("\nOnly the first two are restrictions. Everything else can be ordered at any hour.\n")
w("---\n")

for p, title in (("breakfast", "BREAKFAST — 07:00 to 10:30 only"),
                 ("tandoor", "TANDOOR — 12:00–15:00 and 19:00–23:00 only")):
    w(f"## {title}\n")
    cur = None
    for r in sorted(per.get(p, []), key=lambda r: (r["parent"], r["leaf"], r["dish"])):
        path = f"{r['parent']} › {r['leaf']}" if r["parent"] != r["leaf"] else r["leaf"]
        if path != cur:
            w(f"\n**{path}**\n")
            cur = path
        w(f"- {r['dish']}")
    w("")

w("---\n")
w("## ALL DAY — no restriction\n")
w(f"{len(per.get('all_day', []))} dishes, never refused on timing.\n")
cur = None
for r in sorted(per.get("all_day", []), key=lambda r: (r["parent"], r["leaf"], r["dish"])):
    path = f"{r['parent']} › {r['leaf']}" if r["parent"] != r["leaf"] else r["leaf"]
    if path != cur:
        w(f"\n**{path}**\n")
        cur = path
    w(f"- {r['dish']}")

w("\n---\n")
w("## Needs the hotel to confirm\n")
w("Three judgement calls. Everything else comes straight from the menu's own text.\n")
w("### 1. Which appetizers are \"Tandoori Kebabs\"?\n")
w("The menu says *\"All Tandoori Kebabs will be available from 12 noon to 3 pm and")
w("7 pm to 11 pm\"* but never says which dishes it means. Only **Tandoori Murgh** has")
w("the word in its name. All six are currently restricted — tick any that should NOT be:\n")
for r in rows:
    if r["period"] == "tandoor" and r["leaf"] == "APPETIZERS":
        w(f"- [ ] {r['dish']}")
w("\n### 2. Which breads are \"Tandoori Breads\"?\n")
w("Same problem, same note. Currently split like this:\n")
w("| bread | treated as | orderable |")
w("|---|---|---|")
for r in rows:
    if r["leaf"] == "BREAD":
        w(f"| {r['dish']} | {'tandoor' if r['period'] == 'tandoor' else 'tawa'} | "
          f"{'12–3 and 7–11 only' if r['period'] == 'tandoor' else 'any hour'} |")
w("\n### 3. When is the MID NIGHT MENU served?\n")
w("The menu states **no hours at all** for it, so these are currently orderable at any")
w("hour — including 2 pm. The property KB says *\"24-hour room service, limited menu")
w("after 23:00\"*. If they should be late-night only, they move to a `late_night`")
w("period — already built and tested, currently dormant.\n")
for r in rows:
    if r["parent"] == "MID NIGHT MENU":
        w(f"- [ ] {r['leaf']} › {r['dish']}")

w("\n---\n")
w("## It updates itself\n")
w("Nothing is fixed to a time of day. Every turn resolves the clock afresh:\n")
w("| time of day | dishes unavailable |")
w("|---|---|")
for hh in ("08:19", "11:00", "13:00", "16:00", "20:00", "23:30"):
    h, m = hh.split(":")
    b = A.build_availability_block(H, datetime.datetime(2026, 8, 27, int(h), int(m)))
    w(f"| {hh} | {len(b.get('items') or [])} |")
w("\nA dish flips the moment its window opens or closes, mid-conversation if need be.")
w("No restart, no cache, no manual step.\n")

open("IRD_MENU_CLASSIFICATION.md", "w", encoding="utf-8").write("\n".join(L))
print(f"wrote IRD_MENU_CLASSIFICATION.md ({len(rows)} dishes)")
