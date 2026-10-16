"""
Verify the DB log sink is storing logs in the active database.

Run this ON A MACHINE WITH DB ACCESS (VPN), AFTER the backend has been running
and you've chatted with the bot a bit:

    python tools/check_db_logging.py            # last 30 min
    python tools/check_db_logging.py 120        # last 120 min

It uses the app's own DB connection, so it checks exactly the DB the bot writes
to. Reports per-table row counts (recent + total), source_env split, a sample
latest row from each table, and the LLM cost rollup. Read-only.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from models.database import AsyncSessionLocal, engine

LOG_TABLES = [
    "new_bot_log_messages",
    "new_bot_log_chips",
    "new_bot_log_routing",
    "new_bot_log_backend",
    "new_bot_log_llm",
    "new_bot_log_tickets",
    "new_bot_log_errors",
]


async def main(minutes: int) -> None:
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    print(f"DB: {engine.url.render_as_string(hide_password=True)}")
    print(f"Window: last {minutes} min (since {cutoff.isoformat()})\n")

    async with AsyncSessionLocal() as s:
        # existence
        print("=== TABLES ===")
        for t in LOG_TABLES + ["new_bot_log_llm_daily", "new_bot_sessions"]:
            try:
                await s.execute(text(f"SELECT 1 FROM {t} LIMIT 1"))
                print(f"  [OK]      {t}")
            except Exception:
                print(f"  [MISSING] {t}  <-- not created yet (start the backend once)")

        print("\n=== ROW COUNTS (recent / total) ===")
        for t in LOG_TABLES:
            try:
                recent = (await s.execute(
                    text(f"SELECT COUNT(*) FROM {t} WHERE ts >= :c"), {"c": cutoff}
                )).scalar_one()
                total = (await s.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
                print(f"  {t:28s} {recent:>8} / {total}")
            except Exception as e:
                print(f"  {t:28s} ERROR: {e}")

        print("\n=== source_env split (recent) ===")
        for t in LOG_TABLES:
            try:
                rows = (await s.execute(
                    text(f"SELECT COALESCE(source_env,'?') env, COUNT(*) n FROM {t} "
                         f"WHERE ts >= :c GROUP BY source_env"), {"c": cutoff}
                )).all()
                if rows:
                    print(f"  {t}: " + ", ".join(f"{r.env}={r.n}" for r in rows))
            except Exception:
                pass

        print("\n=== latest sample per table ===")
        for t in LOG_TABLES:
            try:
                r = (await s.execute(
                    text(f"SELECT ts, source_env, session_id, hotel_code, level, component, event "
                         f"FROM {t} ORDER BY ts DESC LIMIT 1")
                )).first()
                print(f"  {t}: {dict(r._mapping) if r else '(empty)'}")
            except Exception as e:
                print(f"  {t}: ERROR {e}")

        print("\n=== LLM cost (recent) ===")
        try:
            r = (await s.execute(
                text("SELECT COUNT(*) calls, COALESCE(SUM(total_tokens),0) tok, "
                     "COALESCE(SUM(total_cost_usd),0) cost FROM new_bot_log_llm WHERE ts >= :c"),
                {"c": cutoff},
            )).first()
            print(f"  calls={r.calls}  tokens={r.tok}  cost_usd={float(r.cost):.6f}")
            rows = (await s.execute(
                text("SELECT day, model, calls, total_tokens, total_cost_usd "
                     "FROM new_bot_log_llm_daily ORDER BY day DESC, total_cost_usd DESC LIMIT 10")
            )).all()
            print("  daily rollup:")
            for r in rows:
                print(f"    {r.day} {r.model}: calls={r.calls} tokens={r.total_tokens} cost=${float(r.total_cost_usd):.6f}")
        except Exception as e:
            print(f"  ERROR {e}")

    await engine.dispose()


if __name__ == "__main__":
    mins = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    asyncio.run(main(mins))
