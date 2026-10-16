"""Per-LLM-call latency breakdown for a Playwright run.

    python tools/zone_latency_report.py                    # last full run
    python tools/zone_latency_report.py sid_a sid_b ...    # explicit sessions

Reads `tools/_kb_verify/full_results.json` for the session ids the harness just
drove, then joins the wall time it measured against `new_bot_log_llm` so you can
see where a turn's seconds actually went — which role, how many calls, how much
of the turn is model time and how much is everything else (DB over VPN, image
enrichment, chip scheduling).

Read-only. Uses the app's own engine, so no credentials live in this file.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import text  # noqa: E402

from models.database import AsyncSessionLocal  # noqa: E402

RESULTS = _ROOT / "tools" / "_kb_verify" / "full_results.json"


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))]


def _row(label: str, values: list[float], unit: str = "s") -> str:
    if not values:
        return f"  {label:<44} (none)"
    return (
        f"  {label:<44} n={len(values):<4} "
        f"min {min(values):6.2f}{unit}  p50 {_pct(values, .5):6.2f}{unit}  "
        f"p90 {_pct(values, .9):6.2f}{unit}  max {max(values):6.2f}{unit}  "
        f"mean {statistics.fmean(values):6.2f}{unit}"
    )


async def main() -> int:
    sessions = sys.argv[1:]
    wall: list[float] = []
    turns: list[dict] = []
    if not sessions:
        if not RESULTS.exists():
            print(f"no {RESULTS} — pass session ids explicitly")
            return 1
        blob = json.loads(RESULTS.read_text(encoding="utf-8"))
        sessions = blob.get("sessions") or []
        turns = blob.get("results") or []
        wall = [r["ms"] / 1000 for r in turns]

    if not sessions:
        print("no sessions to report on")
        return 1

    async with AsyncSessionLocal() as session:
        placeholders = ",".join(f":s{i}" for i in range(len(sessions)))
        rows = (
            await session.execute(
                text(
                    "SELECT session_id, call_site, model, latency_ms, input_tokens, "
                    "output_tokens, total_cost_usd "
                    f"FROM new_bot_log_llm WHERE session_id IN ({placeholders}) ORDER BY id"
                ),
                {f"s{i}": s for i, s in enumerate(sessions)},
            )
        ).mappings().all()

    if not rows:
        print("no new_bot_log_llm rows for those sessions")
        return 1

    by_site: dict[str, list[float]] = defaultdict(list)
    by_model: dict[str, int] = defaultdict(int)
    tokens_in = tokens_out = 0
    cost = 0.0
    for r in rows:
        by_site[str(r["call_site"] or "(unknown)")].append(float(r["latency_ms"] or 0) / 1000)
        by_model[str(r["model"] or "(unknown)")] += 1
        tokens_in += int(r["input_tokens"] or 0)
        tokens_out += int(r["output_tokens"] or 0)
        cost += float(r["total_cost_usd"] or 0)

    n_turns = len(wall) or len({(r["session_id"]) for r in rows})
    llm_total = sum(sum(v) for v in by_site.values())
    wall_total = sum(wall)

    print("=" * 96)
    print("LATENCY REPORT   sessions: " + " ".join(sessions))
    print("=" * 96)
    print(f"\nmodels used: {dict(by_model)}")
    print(f"LLM calls: {len(rows)} across {n_turns} turns "
          f"({len(rows) / max(n_turns, 1):.1f} calls/turn)")
    print(f"tokens: {tokens_in:,} in / {tokens_out:,} out    cost: ${cost:.4f} "
          f"(${cost / max(n_turns, 1):.4f}/turn)")

    if wall:
        print("\n-- GUEST-PERCEIVED (end-to-end wall time per turn) " + "-" * 44)
        print(_row("whole turn", wall))

    print("\n-- PER LLM CALL, BY ROLE " + "-" * 70)
    for site, vals in sorted(by_site.items(), key=lambda kv: -sum(kv[1])):
        print(_row(site[:44], vals))

    if wall_total:
        print("\n-- WHERE THE TIME GOES " + "-" * 72)
        print(f"  model time      {llm_total:8.1f}s   {llm_total / wall_total * 100:5.1f}%")
        print(f"  everything else {wall_total - llm_total:8.1f}s   "
              f"{(wall_total - llm_total) / wall_total * 100:5.1f}%   "
              "(DB over VPN, image enrichment, chip scheduling, transport)")
        print(f"  total           {wall_total:8.1f}s")

    if turns:
        print("\n-- SLOWEST TURNS " + "-" * 78)
        for t in sorted(turns, key=lambda x: -x["ms"])[:8]:
            print(f"  {t['ms'] / 1000:6.1f}s  {t['label']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
