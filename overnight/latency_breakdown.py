"""Where a turn's time actually goes: how many LLM calls it makes, how long each
took, and how much of the turn is NOT LLM at all.

Harness stopwatch numbers include Playwright typing and the settle-wait, so they
overstate. This reads latency_ms straight from new_bot_log_llm.
"""
import io
import json
import re
import sys
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

sid = sys.argv[1]
url = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
eng = create_engine(url, connect_args={"connect_timeout": 10})

with eng.connect() as c:
    rows = [dict(r._mapping) for r in c.execute(text(
        "SELECT id, call_site, model, latency_ms, input_tokens, output_tokens, "
        "input_text, ts FROM new_bot_log_llm WHERE session_id=:s ORDER BY id"), {"s": sid})]

print("LLM calls in session: {}".format(len(rows)))


def user_msg(r):
    try:
        d = json.loads(r["input_text"])
        blob = "\n".join(m.get("content", "") for m in d.get("messages", []))
        m = re.search(r'"user_message":\s*"([^"]{0,60})', blob)
        if m:
            return m.group(1)
        m = re.search(r'GUEST MESSAGE:\s*(.{0,60})', blob)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


# group calls into turns by the user_message they carry
turns = []
cur_msg, cur = None, []
for r in rows:
    um = user_msg(r)
    if um and um != cur_msg:
        if cur:
            turns.append((cur_msg, cur))
        cur_msg, cur = um, [r]
    else:
        cur.append(r)
if cur:
    turns.append((cur_msg, cur))

by_site = defaultdict(lambda: [0, 0, 0])   # calls, total ms, total in-tokens
print("\n{:<3} {:<38} {:>6} {:>9} {:>9}".format("#", "guest", "calls", "llm_ms", "tokens_in"))
print("-" * 72)
for i, (msg, calls) in enumerate(turns, 1):
    ms = sum(int(x["latency_ms"] or 0) for x in calls)
    tin = sum(int(x["input_tokens"] or 0) for x in calls)
    for x in calls:
        s = str(x["call_site"] or "")[:44]
        by_site[s][0] += 1
        by_site[s][1] += int(x["latency_ms"] or 0)
        by_site[s][2] += int(x["input_tokens"] or 0)
    print("{:<3} {:<38} {:>6} {:>9} {:>9}".format(i, str(msg)[:38], len(calls), ms, tin))

print("\n--- per call-site totals (sorted by total ms) ---")
print("{:<46} {:>6} {:>10} {:>9} {:>10}".format("call_site", "calls", "total_ms", "avg_ms", "avg_tok_in"))
for site, (n, ms, tin) in sorted(by_site.items(), key=lambda kv: -kv[1][1]):
    print("{:<46} {:>6} {:>10} {:>9} {:>10}".format(site, n, ms, ms // max(1, n), tin // max(1, n)))

tot_calls = len(rows)
tot_ms = sum(int(x["latency_ms"] or 0) for x in rows)
print("\ntotal LLM calls={}  total LLM ms={}  ({} turns)".format(tot_calls, tot_ms, len(turns)))
if turns:
    print("avg per turn: {:.1f} calls, {:.0f} ms of LLM".format(
        tot_calls / len(turns), tot_ms / len(turns)))
