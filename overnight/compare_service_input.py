"""Compare the SIZE COMPOSITION of the service-agent input between two sessions,
so "the prompt got bigger" names which part got bigger.

Usage: python overnight/compare_service_input.py <old_session> <new_session>
"""
import io
import json
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

url = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
eng = create_engine(url, connect_args={"connect_timeout": 10})


def sample(sid):
    with eng.connect() as c:
        r = c.execute(text(
            "SELECT input_text, input_tokens FROM new_bot_log_llm "
            "WHERE session_id=:s AND call_site LIKE '%service agent%' "
            "ORDER BY id LIMIT 1"), {"s": sid}).first()
    if r is None:
        return None
    d = json.loads(r._mapping["input_text"])
    sysmsg = "\n".join(m.get("content", "") for m in d["messages"] if m.get("role") == "system")
    usr = "\n".join(m.get("content", "") for m in d["messages"] if m.get("role") == "user")
    return {"tokens": r._mapping["input_tokens"], "system": sysmsg, "user": usr}


def sections(sysmsg):
    """Split the system prompt on its === HEADING === markers."""
    parts = re.split(r"\n(?==== )", sysmsg)
    out = {}
    for p in parts:
        head = p.split("\n", 1)[0].strip()[:60] or "(preamble)"
        out[head] = out.get(head, 0) + len(p)
    return out


old_sid, new_sid = sys.argv[1], sys.argv[2]
old, new = sample(old_sid), sample(new_sid)
if not old or not new:
    print("missing service-agent call for one of the sessions")
    raise SystemExit(1)

print("{:<12} {:>12} {:>12} {:>12}".format("part", "OLD chars", "NEW chars", "delta"))
print("-" * 52)
for k in ("system", "user"):
    print("{:<12} {:>12} {:>12} {:>+12}".format(
        k, len(old[k]), len(new[k]), len(new[k]) - len(old[k])))
print("{:<12} {:>12} {:>12} {:>+12}".format(
    "tokens", old["tokens"], new["tokens"], (new["tokens"] or 0) - (old["tokens"] or 0)))

so, sn = sections(old["system"]), sections(new["system"])
keys = sorted(set(so) | set(sn), key=lambda k: -(sn.get(k, 0) - so.get(k, 0)))
print("\n--- system-prompt sections, biggest GROWTH first ---")
print("{:<58} {:>9} {:>9} {:>9}".format("section", "old", "new", "delta"))
for k in keys:
    a, b = so.get(k, 0), sn.get(k, 0)
    if a == b:
        continue
    print("{:<58} {:>9} {:>9} {:>+9}".format(k[:58], a, b, b - a))

# the user message is the JSON payload - compare its top-level keys
def payload_keys(u):
    m = re.search(r"\{.*\}", u, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return {}
    return {k: len(json.dumps(v, ensure_ascii=False)) for k, v in d.items()}


po, pn = payload_keys(old["user"]), payload_keys(new["user"])
if po or pn:
    print("\n--- payload keys, biggest GROWTH first ---")
    ks = sorted(set(po) | set(pn), key=lambda k: -(pn.get(k, 0) - po.get(k, 0)))
    for k in ks[:18]:
        a, b = po.get(k, 0), pn.get(k, 0)
        if a == b:
            continue
        print("{:<40} {:>9} {:>9} {:>+9}".format(k[:40], a, b, b - a))
