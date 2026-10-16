"""Prove the serving-window resolver against the REAL menu text, at times of day
chosen to hit each boundary. Run before wiring it into the payload."""
import io
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from services.llm_orchestration_service import LLMOrchestrationService as S

menu = open("overnight/ird_menu_dump.txt", encoding="utf-8").read()

# --- the range parser on the exact strings the menu uses -------------------
print("=== window parsing ===")
CASES = [
    ("Timing: 7:00 AM - 10:30 AM", [(420, 630)]),
    ("Note: All Tandoori Kebabs will be available from 12 noon to 3 pm and 7 pm to 11 pm.",
     [(720, 900), (1140, 1380)]),
    ("All Tandoori Breads available from 12 noon–3 pm & 7 pm–11 pm",
     [(720, 900), (1140, 1380)]),
]
fails = 0
for line, want in CASES:
    got = S._ird_parse_windows(line)
    ok = got == want
    fails += 0 if ok else 1
    print("{}  {!r}".format("PASS" if ok else "FAIL", line[:62]))
    if not ok:
        print("      want {} got {}".format(want, got))

# The menu's own two breakfast lines use an en dash; parse them as found.
real = [l.strip() for l in menu.split("\n")
        if "Timing:" in l or ("available" in l.lower() and "pm" in l.lower())]
print("\n=== notes found in the real menu ===")
for l in real:
    print("   {!r} -> {}".format(l[:70], S._ird_parse_windows(l)))

# --- the whole resolver, at the hour that actually broke -------------------
print("\n=== resolver at several clock times ===")
for hhmm in ("08:00", "13:00", "17:36", "19:30", "23:30"):
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime(2026, 8, 20, h, m)
    rows = S._build_menu_serving_windows(menu, now)
    print("\n--- {} IST ---".format(hhmm))
    if not rows:
        print("   (no timing notes -> everything available)")
    for r in rows:
        print("   [{}] {:22s} {:34s} {}".format(
            "OPEN  " if r["open_now"] else "CLOSED",
            r["menu_section"][:22], r["served"],
            "" if r["open_now"] else "next: " + r["next_opens"]))

# --- the specific regression from the handover ----------------------------
print("\n=== regression: Tandoori Murgh at 5:36 PM ===")
rows = S._build_menu_serving_windows(menu, datetime(2026, 8, 20, 17, 36))
keb = [r for r in rows if "kebab" in r["note"].lower()]
if not keb:
    print("FAIL: the Tandoori Kebab note was not picked up at all")
    fails += 1
elif keb[0]["open_now"]:
    print("FAIL: kebabs reported OPEN at 17:36")
    fails += 1
elif keb[0]["next_opens"] != "today at 7:00 PM":
    print("FAIL: next_opens = {!r}".format(keb[0]["next_opens"]))
    fails += 1
else:
    print("PASS: CLOSED at 17:36, reopens today at 7:00 PM")

# a section with no stated timing must produce no entry (= always available)
if any("MID NIGHT" in r["menu_section"] for r in rows):
    print("FAIL: invented a window for MID NIGHT MENU")
    fails += 1
else:
    print("PASS: MID NIGHT MENU has no stated window and got no entry")

print("\n{}".format("ALL PASS" if not fails else "{} FAILURE(S)".format(fails)))
raise SystemExit(1 if fails else 0)
