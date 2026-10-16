"""Aggregate the per-substep durations the backend already records, so the non-LLM
half of a turn stops being a guess. Streams the trace file - it is large.

Usage: python overnight/step_hotspots.py <session_id> [tail_megabytes]
"""
import io
import json
import os
import sys
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sid = sys.argv[1]
tail_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 40
path = os.path.join("logs", "step_trace.jsonl")
size = os.path.getsize(path)
start = max(0, size - tail_mb * 1024 * 1024)

agg = defaultdict(lambda: [0, 0.0, 0.0])   # count, total_ms, max_ms
turn_ids = set()
with open(path, "rb") as f:
    f.seek(start)
    if start:
        f.readline()                        # discard the partial line
    for raw in f:
        if sid.encode() not in raw:
            continue
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        ms = (d.get("payload") or {}).get("duration_ms")
        if ms is None:
            continue
        step = str(d.get("step") or d.get("event") or "?")
        a = agg[step]
        a[0] += 1
        a[1] += float(ms)
        a[2] = max(a[2], float(ms))
        if d.get("turn_trace_id"):
            turn_ids.add(d["turn_trace_id"])

if not agg:
    print("no timed substeps found for {} in the last {}MB".format(sid, tail_mb))
    raise SystemExit(0)

# Turn count from the router step if present, else from distinct trace ids.
router = agg.get("chat_service.orchestrate") or agg.get("chat_service.llm_orchestration")
turns = router[0] if router else (len(turn_ids) or 1)

print("session {}  (~{} turns)".format(sid, turns))
print("\n{:<52} {:>5} {:>10} {:>9} {:>9}".format("step", "n", "total_ms", "avg_ms", "max_ms"))
print("-" * 90)
for step, (n, tot, mx) in sorted(agg.items(), key=lambda kv: -kv[1][1])[:28]:
    print("{:<52} {:>5} {:>10.0f} {:>9.0f} {:>9.0f}".format(step[:52], n, tot, tot / n, mx))

grand = sum(v[1] for v in agg.values())
print("\nsum of all timed substeps: {:.0f} ms over ~{} turns = {:.0f} ms/turn".format(
    grand, turns, grand / max(1, turns)))
print("(substeps nest, so this over-counts - read the table, not the sum)")
