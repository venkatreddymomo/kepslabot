"""Repair chat histories the "assistant loses its thread id" bug left one-sided.

`retain_safe_internal_entries` dropped `_active_conversation_uid` from
pending_data whenever a flow completed MID-TURN — after the guest's message was
appended and before the reply to it was. The guest's row was written with the
thread id; the bot's reply was written with `conversation_uid` NULL. The widget
replays only the rows matching the thread it is resuming, so those histories came
back as the guest talking to nobody.

The code fix (conversation_memory_service.retain_safe_internal_entries) stops it
happening again. This repairs the rows already written.

A NULL row is adopted by the thread of the message immediately BEFORE it in the
same DB conversation — for the assistant rows this bug produced, that is exactly
the guest turn that prompted the reply. Rows whose whole conversation has no uid
anywhere are TRUE LEGACY (they predate per-thread scoping entirely) and are left
alone: the widget only resumes a thread when it has a stored uid, so those
sessions simply start fresh and were never affected by this bug.

Usage:
    python tools/backfill_message_conversation_uid.py            # dry run
    python tools/backfill_message_conversation_uid.py --apply    # write
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import bindparam, text

from models.database import AsyncSessionLocal

# Resolve every orphan in ONE set-based pass. Row-at-a-time lookups meant ~9k
# sequential round-trips, which is minutes of latency for a job that is really a
# single join.
#
# Scoped to conversations that carry a uid SOMEWHERE — i.e. threads that were
# scoped and lost rows to the bug. Conversations with no uid at all are legacy.
RESOLVED = """
SELECT m.id, m.role,
       COALESCE(
         (SELECT b.conversation_uid FROM new_bot_messages b
          WHERE b.conversation_id = m.conversation_id
            AND b.conversation_uid IS NOT NULL
            AND (b.created_at < m.created_at
                 OR (b.created_at = m.created_at AND b.id < m.id))
          ORDER BY b.created_at DESC, b.id DESC LIMIT 1),
         (SELECT a.conversation_uid FROM new_bot_messages a
          WHERE a.conversation_id = m.conversation_id
            AND a.conversation_uid IS NOT NULL
            AND (a.created_at > m.created_at
                 OR (a.created_at = m.created_at AND a.id > m.id))
          ORDER BY a.created_at ASC, a.id ASC LIMIT 1)
       ) AS adopted_uid
FROM new_bot_messages m
WHERE m.conversation_uid IS NULL
  AND EXISTS (
      SELECT 1 FROM new_bot_messages x
      WHERE x.conversation_id = m.conversation_id
        AND x.conversation_uid IS NOT NULL
  )
"""

UPDATE_BATCH = """
UPDATE new_bot_messages SET conversation_uid = :u WHERE id IN :ids
"""


async def main(apply: bool) -> int:
    by_role: dict[str, int] = {}
    by_uid: dict[str, list[int]] = {}
    skipped = 0

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(RESOLVED))).all()
        print(f"{len(rows)} orphaned rows in threads that have a uid elsewhere")

        for mid, role, uid in rows:
            if not uid:
                skipped += 1
                continue
            by_uid.setdefault(str(uid), []).append(int(mid))
            by_role[role] = by_role.get(role, 0) + 1

        fixed = sum(len(ids) for ids in by_uid.values())
        if apply:
            # Grouped by target uid, chunked so no single statement carries a
            # runaway IN list.
            for uid, ids in by_uid.items():
                for i in range(0, len(ids), 500):
                    chunk = ids[i : i + 500]
                    await db.execute(
                        text(UPDATE_BATCH).bindparams(bindparam("ids", expanding=True)),
                        {"u": uid, "ids": chunk},
                    )
            await db.commit()

    verb = "updated" if apply else "would update"
    roles = ", ".join(f"{r}={n}" for r, n in sorted(by_role.items())) or "none"
    print(f"{verb} {fixed} rows across {len(by_uid)} threads  ({roles})")
    if skipped:
        print(f"{skipped} rows had no neighbouring uid to adopt — left as-is")
    if not apply:
        print("\nDRY RUN — nothing was written. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    raise SystemExit(asyncio.run(main(ap.parse_args().apply)))
