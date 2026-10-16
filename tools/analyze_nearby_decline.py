"""Why does the answerer sometimes ignore verified nearby places?

Three attempts to fix this by wording failed, so this stops guessing and
compares the ACTUAL prompts: for the same question, one turn where the bot gave
distances and one where it declined. If the inputs are identical the problem is
model behaviour and needs a different lever; if they differ, something upstream
is dropping the facts and that is the real bug.

    python tools/analyze_nearby_decline.py [--since BYTE_OFFSET]

Reads logs/llm_io_trace.jsonl forward from an offset (the file is ~2 GB, so it
never loads the whole thing).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TRACE = "logs/llm_io_trace.jsonl"
HAS_DISTANCE = re.compile(
    r"\b\d[\d.,]*\s*(km|kms|kilometres?|kilometers?)\b|\b\d[\d.,]*\s*m\b|\b\d+\s*(min|mins|minutes)\b",
    re.I,
)


def all_text(inputs: dict) -> str:
    """Every string the model was actually shown, prompt and messages alike."""
    parts = [str(inputs.get("system_prompt") or "")]
    msgs = inputs.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict):
                parts.append(str(m.get("content") or ""))
            else:
                parts.append(str(m))
    for k, v in inputs.items():
        if k not in ("system_prompt", "messages") and isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=0)
    args = ap.parse_args()

    turns = []
    with open(TRACE, "rb") as f:
        f.seek(args.since)
        blob = f.read().decode("utf-8", errors="replace")
    for line in blob.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        actor = rec.get("trace_actor") or {}
        if str(actor.get("agent") or "") != "book_answerer":
            continue
        inputs = rec.get("inputs") or {}
        text = all_text(inputs)
        if "NEARBY PLACES" not in text:
            continue  # no verified places were injected -> not the case we study
        out = str(rec.get("output") or "")
        # the places the model was HANDED
        given = [n.strip() for n in re.findall(r"^-\s+([^|\n]+?)\s*\|", text, re.M)]
        given = [g for g in given if len(g) > 2]
        used = [g for g in given if g.lower() in out.lower()]
        turns.append({
            "q": str(rec.get("user_query") or "").strip(),
            "model": rec.get("model"),
            "out": out,
            "given": given,
            "used": used,
            "answered": bool(HAS_DISTANCE.search(out)) and bool(used),
            "text": text,
        })

    print(f"{len(turns)} answerer turns that were HANDED verified nearby places\n")
    if not turns:
        print("none found — run the nearby test first, or widen --since")
        return 1

    ok = [t for t in turns if t["answered"]]
    bad = [t for t in turns if not t["answered"]]
    print(f"used them : {len(ok)}")
    print(f"ignored   : {len(bad)}")
    print()
    for t in bad:
        print(f"  IGNORED  q={t['q'][:60]!r}  was given {len(t['given'])} places: "
              f"{', '.join(t['given'][:3])}")
        print(f"           replied: {t['out'][:150]!r}")
    print()

    # the money question: same wording, different outcome?
    by_q = defaultdict(list)
    for t in turns:
        by_q[t["q"].strip().lower()].append(t)
    both = {q: v for q, v in by_q.items()
            if any(x["answered"] for x in v) and any(not x["answered"] for x in v)}
    print(f"questions asked identically that went BOTH ways: {len(both)}")
    for q, v in both.items():
        good = next(x for x in v if x["answered"])
        poor = next(x for x in v if not x["answered"])
        print("\n" + "=" * 78)
        print(f"QUESTION: {q}")
        print("=" * 78)
        print(f"  passing prompt : {len(good['text']):>7} chars, {len(good['given'])} places given")
        print(f"  failing prompt : {len(poor['text']):>7} chars, {len(poor['given'])} places given")
        gt, pt = good["text"], poor["text"]
        print(f"  prompts identical: {gt == pt}")
        if gt != pt:
            # locate the first divergence so the difference is concrete
            i = next((n for n in range(min(len(gt), len(pt))) if gt[n] != pt[n]),
                     min(len(gt), len(pt)))
            print(f"  first difference at char {i}:")
            print(f"    passing: ...{gt[max(0,i-90):i+90]!r}")
            print(f"    failing: ...{pt[max(0,i-90):i+90]!r}")
        # what the block itself looked like
        for tag, t in (("PASSING", good), ("FAILING", poor)):
            m = re.search(r"NEARBY PLACES.{0,700}", t["text"], re.S)
            print(f"\n  --- {tag} injected block ---")
            print("   ", (m.group(0)[:520] if m else "(not found)").replace("\n", "\n    "))
            print(f"    -> replied: {t['out'][:200]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
