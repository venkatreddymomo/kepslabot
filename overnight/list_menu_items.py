"""Item names per section, with the serving-window note that governs each, so the
50-turn script uses exact menu names and knows which are all-day."""
import io
import re
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from services.llm_orchestration_service import LLMOrchestrationService as S

menu = open("overnight/ird_menu_dump.txt", encoding="utf-8").read()
body = menu[menu.find("IN ROOM DINING MENU"):]
lines = [l.rstrip() for l in body.split("\n")]

timed_sections = {r["menu_section"] for r in
                  S._build_menu_serving_windows(menu, datetime(2026, 8, 20, 19, 48))}

section = ""
prev = ""
rows = []
for ln in lines:
    s = ln.strip()
    if s and s == s.upper() and any(c.isalpha() for c in s) and not any(c.isdigit() for c in s):
        section = s
        prev = ""
        continue
    if s.startswith("Type:") and prev:
        rows.append((section, prev))
    prev = s if s else prev

want = [a.upper() for a in sys.argv[1:]] or None
cur = None
for sec, item in rows:
    if want and not any(w in sec for w in want):
        continue
    if sec != cur:
        cur = sec
        flag = "  <-- HAS A SERVING WINDOW" if sec in timed_sections else "  (all-day)"
        print("\n== {}{}".format(sec, flag))
    print("   {}".format(item))
