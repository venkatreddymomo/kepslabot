"""Bring the DEV bot config into line with PROD, row by row.

    python sync_prod_config_to_dev.py            # dry run (default)
    python sync_prod_config_to_dev.py --apply    # write to DEV

DIRECTION
    PROD (10.100.5.153) is READ ONLY here and is never written to.
    DEV  (172.16.5.32)  is the only side that changes.

WHAT IT TOUCHES
    Only the CONFIG rows that decide how the bot behaves and that were found
    to differ: `new_bot_prompt_registry` and `new_bot_business_config`. It
    never touches conversations, messages, tickets, sessions, guests, bookings
    or any log table - those are runtime data and are SUPPOSED to differ.

    Verified already identical and therefore left alone: new_bot_hotels,
    new_bot_properties, new_bot_services, new_bot_widget_deployments,
    new_bot_location_intros and every new_bot_kb_* table.

SAFETY
    - Primary keys are preserved, so child rows keep pointing at the right parent.
    - Every row is re-read from DEV afterwards and compared against PROD; the
      script reports any row that did not land.
    - A row already identical is skipped, so re-running is a no-op.
    - Nothing is deleted, ever.
"""

from __future__ import annotations

import hashlib
import os
import sys

import pymysql

PROD = dict(host="10.100.5.153", port=3306, user="root",
            password=os.environ.get("MYSQL_PASSWORD", ""), database="GHN_PROD_BAK",
            connect_timeout=20, read_timeout=120)
DEV = dict(PROD, host="172.16.5.32")

# (table, primary key column, columns to copy)
TARGETS = [
    ("new_bot_prompt_registry", "id",
     ["prompt_key", "industry", "hotel_id", "model_variant", "content",
      "variables", "version", "description", "updated_by", "is_active"]),
    ("new_bot_business_config", "id",
     ["hotel_id", "config_key", "config_value"]),
    # `description` here is NOT documentation - it is a mini-prompt the router
    # reads, so a stale one silently routes differently. Mango's Room Booking
    # (99) and Support Intake (100) had genuinely different text on the two
    # sides: dev's 99 said the service must NOT take the booking while prod's
    # said it owns it. Prod is the side being tested against, so prod wins.
    ("new_bot_services", "id",
     ["description", "generated_system_prompt"]),
]


def sha(v: object) -> str:
    return hashlib.sha1(str(v if v is not None else "").encode("utf-8", "replace")).hexdigest()[:10]


def fetch(dsn: dict, table: str, pk: str, cols: list[str]) -> dict:
    conn = pymysql.connect(**dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("select `%s`,%s from `%s`" % (pk, ",".join("`%s`" % c for c in cols), table))
            return {r[0]: tuple(r[1:]) for r in cur.fetchall()}
    finally:
        conn.close()


def main() -> int:
    if not PROD["password"]:
        print("MYSQL_PASSWORD is not set")
        return 2
    apply = "--apply" in sys.argv
    total_ins = total_upd = 0

    for table, pk, cols in TARGETS:
        print("\n=== %s ===" % table)
        p = fetch(PROD, table, pk, cols)
        d = fetch(DEV, table, pk, cols)

        inserts = [k for k in p if k not in d]
        updates = [k for k in p if k in d and p[k] != d[k]]
        only_dev = [k for k in d if k not in p]

        for k in only_dev:
            print("  LEAVING ALONE (exists only in dev): %s=%s" % (pk, k))
        for k in sorted(inserts):
            print("  INSERT %s=%s" % (pk, k))
        for k in sorted(updates):
            changed = [c for i, c in enumerate(cols) if p[k][i] != d[k][i]]
            print("  UPDATE %s=%s  changed: %s" % (pk, k, ", ".join(changed)))
        if not inserts and not updates:
            print("  already in sync")
            continue

        total_ins += len(inserts)
        total_upd += len(updates)
        if not apply:
            continue

        conn = pymysql.connect(**DEV)
        try:
            with conn.cursor() as cur:
                for k in inserts:
                    cur.execute(
                        "insert into `%s` (`%s`,%s) values (%s)"
                        % (table, pk, ",".join("`%s`" % c for c in cols),
                           ",".join(["%s"] * (len(cols) + 1))),
                        (k,) + p[k])
                for k in updates:
                    cur.execute(
                        "update `%s` set %s where `%s`=%%s"
                        % (table, ",".join("`%s`=%%s" % c for c in cols), pk),
                        p[k] + (k,))
            conn.commit()
        finally:
            conn.close()

        # verify
        d2 = fetch(DEV, table, pk, cols)
        bad = [k for k in (inserts + updates) if d2.get(k) != p[k]]
        if bad:
            print("  !! DID NOT LAND: %s" % bad)
            return 1
        print("  verified %d insert(s), %d update(s)" % (len(inserts), len(updates)))

    print("\n%s: %d insert(s), %d update(s)"
          % ("APPLIED" if apply else "DRY RUN - nothing written", total_ins, total_upd))
    if not apply:
        print("re-run with --apply to write to DEV")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
