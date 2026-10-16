"""Grade a run against the spec's pass criteria.

Deliberately mechanical: it flags turns for a human to read rather than declaring
a pass. The one thing it judges outright is the criterion that is objective - a
ticket_ref starting LOCAL- means the external call never landed.

Usage: python overnight/grade_run.py <RUN_TAG>
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

tag = sys.argv[1] if len(sys.argv) > 1 else "run50"
OUT = os.path.dirname(os.path.abspath(__file__))
turns = [json.loads(l) for l in open(os.path.join(OUT, tag + "_turns.jsonl"), encoding="utf-8")]

RUPEE = "₹"


def has_price(t):
    return RUPEE in t or bool(re.search(r"\bRs\.?\s*\d", t, re.I))


def has_total(t):
    return bool(re.search(r"total", t, re.I)) and has_price(t)


print("=" * 78)
print("RUN {}  -  {} turns".format(tag, len(turns)))
print("=" * 78)

flags = []
for r in turns:
    n, guest, bot = r["turn"], r["guest"], r.get("bot") or ""
    intent = r.get("intent") or ""
    low = bot.lower()
    issues = []

    # recall turns must be grounded in the real order: prices AND a total
    if re.search(r"what did i order|dubara batao", guest, re.I):
        if not has_price(bot):
            issues.append("NO PRICES - ungrounded recall")
        elif not has_total(bot):
            issues.append("prices but NO total")

    # nothing may be announced as on its way without a ticket this turn
    if re.search(r"on its way|being prepared|order placed|kitchen has", low):
        if not r.get("tickets_changed"):
            issues.append("says placed/on-its-way but NO ticket changed this turn")

    # a refusal turn must not quietly place something
    if re.search(r"red wine|kadak", guest, re.I) and r.get("tickets_changed"):
        issues.append("ticket touched on a turn that should have been refused")

    # the serving-time turns
    if "OUT of window" in intent:
        if not re.search(r"7:00 ?am|7 ?am", low) or not re.search(
                r"not available|isn.t available|unavailable|not served", low):
            issues.append("did not clearly refuse + give the window")
    if "no stated timing" in intent and re.search(r"not available|unavailable", low):
        issues.append("refused an item that has NO stated timing")

    if issues:
        flags.append((n, guest, issues, bot))

print("\n--- TURNS NEEDING A LOOK ({}) ---".format(len(flags)))
for n, guest, issues, bot in flags:
    print("\n[turn {:02d}] {}".format(n, guest))
    for i in issues:
        print("    !! {}".format(i))
    print("    bot: {}".format(bot[:260].replace("\n", " ")))

# tickets
res_path = os.path.join(OUT, tag + "_result.json")
if os.path.exists(res_path):
    res = json.load(open(res_path, encoding="utf-8"))
    tk = res.get("tickets", [])
    local = [t for t in tk if str(t.get("ticket_ref") or "").upper().startswith("LOCAL-")]
    print("\n--- TICKETS ({}) ---".format(len(tk)))
    for t in tk:
        print("  {} [{}] {:22s} {}".format(
            t.get("ticket_ref"), t.get("status"), str(t.get("department"))[:22],
            str(t.get("issue"))[:90]))
    print("\nlocal-only (INVALID): {}".format(len(local)))
    print("latency: {}".format(res.get("latency")))

print("\n--- ticket activity by turn ---")
for r in turns:
    for t in (r.get("tickets_changed") or []):
        print("  turn {:02d} {:12s} [{}] {}".format(
            r["turn"], str(t.get("ref")), t.get("status"), str(t.get("issue"))[:70]))
