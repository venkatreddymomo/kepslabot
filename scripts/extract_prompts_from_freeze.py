#!/usr/bin/env python
"""
Extract prompts from a local Zone freeze snapshot into the per-model folders.
Offline fallback for export_model_prompts.py when the DB/VPN is unreachable.

Reads:  backups/hotel_freeze/<latest>.json  (effective_prompts)
Writes: prompts/models/gpt-4.1-mini/<key>.md   (always refreshed — safekeeping)
        prompts/models/gpt-5.4-mini/<key>.md   (only if absent — never clobbers tuned files)

No DB, no creds. NOTE: a freeze is a point-in-time snapshot; on wake, prefer
export_model_prompts.py against the live DB to confirm the baseline is current.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FREEZE_DIR = ROOT / "backups" / "hotel_freeze"


def _slug(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def _fm(key: str, desc, variables) -> str:
    if isinstance(variables, str):
        try:
            variables = json.loads(variables)
        except Exception:
            variables = []
    if not isinstance(variables, list):
        variables = []
    desc = str(desc or "").replace("\n", " ").strip()
    return f"---\nkey: {key}\ndescription: {desc}\nvariables: [{', '.join(variables)}]\n---\n"


def main() -> None:
    snaps = sorted(FREEZE_DIR.glob("zone_*.json"))
    snaps = [s for s in snaps if "summary" not in s.name]
    if not snaps:
        raise SystemExit(f"no freeze snapshot in {FREEZE_DIR}")
    snap = snaps[-1]
    data = json.loads(snap.read_text(encoding="utf-8"))
    eps = data.get("effective_prompts") or []
    print(f"source: {snap.name}  captured_at={data.get('captured_at')}  keys={len(eps)}")

    dir_41 = ROOT / "prompts" / "models" / "gpt-4.1-mini"
    dir_54 = ROOT / "prompts" / "models" / "gpt-5.4-mini"
    dir_41.mkdir(parents=True, exist_ok=True)
    dir_54.mkdir(parents=True, exist_ok=True)

    for e in eps:
        key = e.get("key")
        if not key:
            continue
        content = str(e.get("content") or "")
        body = _fm(key, e.get("description"), e.get("variables")) + content + "\n"
        (dir_41 / f"{_slug(key)}.md").write_text(body, encoding="utf-8")
        f54 = dir_54 / f"{_slug(key)}.md"
        kept = f54.exists()
        if not kept:
            f54.write_text(body, encoding="utf-8")
        print(f"  {key:<40} {len(content):>7} ch | 4.1 refreshed | "
              f"{'5.4 kept (tuned)' if kept else '5.4 baseline'}")

    print("\nDONE (offline, from freeze).")


if __name__ == "__main__":
    main()
