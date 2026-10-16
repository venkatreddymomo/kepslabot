"""Tighten Zone's answer-length guidance in the book-answerer prompt.

Scope: Zone (hotel 67) ONLY, and it EDITS the existing "HOW TO ANSWER" and
"FORMATTING" sections in place rather than appending another block — the prompt
is already large and stacking more rules on the end makes it worse, not better.

Which row is live: MULTI_MODEL_ENABLED=true and CHAT_LLM_BOOK_ANSWERER_MODEL is
gpt-5.4-mini, and prompt_registry resolves hotel+variant before hotel+default,
so `orchestrator.book_answerer` for Zone resolves to the (hotel 67,
gpt-5.4-mini) row. That is the row edited here. The (hotel 67, default) row
already carries an equivalent "No long paragraphs. No filler openers." rule, so
it is left alone.

Intent (per product feedback): the problem is huge blocky paragraphs and padding,
NOT thoroughness. So no word counts or hard caps — those backfire. Instead: say
everything that matters, but write it compactly and cut anything that isn't
load-bearing. And never answer in a single bare word: keep a warm, complete line.

Usage:
    python tools/tighten_zone_answer_style.py --dry-run
    python tools/tighten_zone_answer_style.py
    python tools/tighten_zone_answer_style.py --restore backups/<file>.json
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

import pymysql

HOTEL_ID = 67
TARGET_KEY = "orchestrator.book_answerer"
TARGET_VARIANT = "gpt-5.4-mini"

DB = dict(
    host=os.getenv("MYSQL_HOST", "172.16.5.32"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", "CHANGE_ME"),
    database=os.getenv("MYSQL_DATABASE", "GHN_PROD_BAK"),
    charset="utf8mb4",
    connect_timeout=15,
)

OLD_SIZING = """1. Right-size the answer.
- Simple question: answer briefly and stop.
- Broader exploratory question: give a little structure, but only the most useful highlights.
- Do not expand just because you can."""

NEW_SIZING = """1. Right-size the answer, then tighten it.
- Say everything that genuinely helps, but write it as compactly as the facts allow. A thorough answer is fine; a long-winded one is not.
- NEVER a wall of text. If the answer carries more than one idea, break it into short scannable lines — a guest reads this on a phone.
- Cut everything that is not load-bearing: scene-setting, marketing adjectives, repeating the guest's question back, hedging, and closing filler. Prefer the shortest wording that still says the whole thing.
- Simple question: answer it and stop — but always as a warm, complete line. Never a bare one-word reply.
- Broader exploratory question: cover the useful ground, still as short scannable points rather than paragraphs.
- Do not expand just because you can. If a fact does not help the guest decide, leave it out."""

OLD_FORMAT = """- Prefer a short lead-in plus 2-4 compact sections or points when the answer has multiple useful ideas
- Keep each point short and guest-friendly"""

NEW_FORMAT = """- Prefer a short lead-in plus 2-4 compact sections or points when the answer has multiple useful ideas
- Keep each point short and guest-friendly — if a point runs past about two lines, split it or cut it
- No long plain paragraphs, and no filler openers ("Currently,", "At our hotel,", "Please note that,", "I'd be happy to,")"""

EDITS = [(OLD_SIZING, NEW_SIZING), (OLD_FORMAT, NEW_FORMAT)]


def _backup_path() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, f"zone_book_answerer_prompt_{stamp}.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", default="")
    args = ap.parse_args()

    conn = pymysql.connect(**DB)
    cur = conn.cursor(pymysql.cursors.DictCursor)

    if args.restore:
        row = json.load(open(args.restore, encoding="utf-8"))
        cur.execute(
            "UPDATE new_bot_prompt_registry SET content=%s WHERE id=%s",
            (row["content"], row["id"]),
        )
        conn.commit()
        print(f"restored prompt row {row['id']} from {args.restore}")
        return 0

    cur.execute(
        "SELECT id, content FROM new_bot_prompt_registry "
        "WHERE prompt_key=%s AND hotel_id=%s AND model_variant=%s AND is_active=1",
        (TARGET_KEY, HOTEL_ID, TARGET_VARIANT),
    )
    row = cur.fetchone()
    if not row:
        print(f"!! no active row for {TARGET_KEY} hotel={HOTEL_ID} variant={TARGET_VARIANT}")
        return 1

    content = row["content"]
    updated = content
    for old, new in EDITS:
        found = updated.count(old)
        label = old.splitlines()[0][:60]
        if found != 1:
            print(f"!! expected exactly 1 match for {label!r}, found {found} — aborting")
            return 1
        updated = updated.replace(old, new)
        print(f"  edited: {label!r}")

    backup = _backup_path()
    json.dump({"id": row["id"], "content": content}, open(backup, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"backup -> {backup}")
    print(f"  size {len(content)} -> {len(updated)} chars")

    if args.dry_run:
        print("\nDRY RUN — nothing written")
        return 0

    cur.execute(
        "UPDATE new_bot_prompt_registry SET content=%s, updated_by=%s WHERE id=%s",
        (updated, "tighten_zone_answer_style", row["id"]),
    )
    conn.commit()
    print(f"\nupdated prompt row {row['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
