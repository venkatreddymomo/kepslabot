"""Backfill LLM cost columns that were logged as $0.00.

Why this exists
---------------
`LLMSimpleCallLogger._DEFAULT_PRICING_USD_PER_1M` only ever knew the gpt-4o
family. When the fleet moved to gpt-4.1-mini / gpt-5.4-mini, `_resolve_pricing`
started returning (0.0, 0.0) for every chat call and `_compute_cost` multiplied
by zero — silently. Three months and 264.8M tokens later, `new_bot_log_llm_daily`
held $14.70 in total, all of it from residual gpt-4o traffic.

Tokens were never lost, so cost is pure arithmetic over what is already stored.
This script recomputes it using the SAME `_resolve_pricing` the live logger now
uses, so the backfill and future rows can never disagree about a rate.

What it touches
---------------
1. `new_bot_log_llm_daily`  — every day, back to the earliest row. This is the
   durable billing history; the raw table prunes at 7 days.
2. `new_bot_log_llm`        — rows inside the retention window ONLY.
   Not cosmetic: `db_log_sink.rollup_llm_daily()` delete-then-reinserts the last
   `db_logging_retention_days` from these raw rows on every app start. Without
   this pass the newest week of the backfill is silently reverted to $0 the next
   time the server boots.

Usage:
    python backfill_llm_costs.py            # dry run — prints the delta, writes nothing
    python backfill_llm_costs.py --apply    # commit
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.llm_simple_call_logger import llm_simple_call_logger  # noqa: E402

RETENTION_DAYS = 7


def _db_config() -> dict[str, object]:
    env_path = Path(__file__).resolve().parent / ".env"
    cfg: dict[str, str] = {}
    with env_path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = re.match(r"^\s*(MYSQL_[A-Z]+)\s*=\s*(.*?)\s*$", line)
            if m:
                cfg[m.group(1)] = m.group(2)
    return {
        "host": cfg["MYSQL_HOST"],
        "port": int(cfg["MYSQL_PORT"]),
        "user": cfg["MYSQL_USER"],
        "password": cfg["MYSQL_PASSWORD"],
        "database": cfg["MYSQL_DATABASE"],
        "charset": "utf8mb4",
    }


def _cost(model: str, in_tok: int, out_tok: int) -> tuple[float, float, float, float, float, str]:
    """Delegate to the live logger so rates can never fork from production."""
    return llm_simple_call_logger._compute_cost(
        model=model or "",
        input_tokens=int(in_tok or 0),
        output_tokens=int(out_tok or 0),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit (default is a dry run)")
    args = ap.parse_args()

    cn = pymysql.connect(**_db_config())  # type: ignore[arg-type]
    cur = cn.cursor()

    # ── 1. daily rollup ──────────────────────────────────────────────────────
    cur.execute(
        """SELECT id, model, input_tokens, output_tokens, total_cost_usd
           FROM new_bot_log_llm_daily"""
    )
    daily = cur.fetchall()

    old_total = 0.0
    new_total = 0.0
    unpriced: dict[str, int] = {}
    daily_updates: list[tuple[float, int]] = []
    for row_id, model, itok, otok, old_cost in daily:
        _, _, total, _, _, source = _cost(model, itok, otok)
        old_total += float(old_cost or 0)
        new_total += total
        if source == "unpriced":
            unpriced[model or "<blank>"] = unpriced.get(model or "<blank>", 0) + 1
        if abs(total - float(old_cost or 0)) > 1e-9:
            daily_updates.append((round(total, 8), row_id))

    print(f"new_bot_log_llm_daily : {len(daily)} rows, {len(daily_updates)} need updating")
    print(f"  total cost  BEFORE  : ${old_total:,.4f}")
    print(f"  total cost  AFTER   : ${new_total:,.4f}")
    if unpriced:
        print("  STILL UNPRICED (no rate known — these stay at $0):")
        for m, n in sorted(unpriced.items(), key=lambda kv: -kv[1]):
            print(f"    {m}: {n} rows")

    # per-model breakdown of the change
    cur.execute(
        """SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(total_cost_usd)
           FROM new_bot_log_llm_daily GROUP BY model ORDER BY SUM(total_tokens) DESC"""
    )
    print("\n  per model:")
    print(f"    {'model':<26}{'before':>12}{'after':>14}  source")
    for model, itok, otok, old_cost in cur.fetchall():
        _, _, total, _, _, source = _cost(model, itok, otok)
        print(f"    {str(model):<26}{float(old_cost or 0):>12.4f}{total:>14.4f}  {source}")

    # ── 2. raw rows inside the retention window ─────────────────────────────
    cur.execute(
        """SELECT id, model, input_tokens, output_tokens
           FROM new_bot_log_llm
           WHERE ts >= (NOW() - INTERVAL %s DAY)""",
        (RETENTION_DAYS,),
    )
    raw = cur.fetchall()
    raw_updates = []
    for row_id, model, itok, otok in raw:
        in_cost, out_cost, total, in_rate, out_rate, source = _cost(model, itok, otok)
        raw_updates.append(
            (round(in_rate, 8), round(out_rate, 8), round(in_cost, 8),
             round(out_cost, 8), round(total, 8), source, row_id)
        )
    print(
        f"\nnew_bot_log_llm       : {len(raw_updates)} raw rows in the "
        f"{RETENTION_DAYS}-day window get cost + rate + pricing_source"
    )

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return 0

    cur.executemany(
        "UPDATE new_bot_log_llm_daily SET total_cost_usd=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
        daily_updates,
    )
    cur.executemany(
        """UPDATE new_bot_log_llm
           SET input_rate=%s, output_rate=%s, input_cost_usd=%s,
               output_cost_usd=%s, total_cost_usd=%s,
               currency='USD', pricing_source=%s
           WHERE id=%s""",
        raw_updates,
    )
    cn.commit()
    print(f"\nAPPLIED: {len(daily_updates)} daily rows, {len(raw_updates)} raw rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
