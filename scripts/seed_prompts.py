"""
Re-seed all prompts from the prompts/defaults/ files into the DB registry.

Use this when the startup seed times out (the registry then keeps the previous
DB version, which means file edits never take effect at runtime).

Usage:
    python scripts/seed_prompts.py
    python scripts/seed_prompts.py --keys orchestrator.main_router orchestrator.book_answerer
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.prompt_seed_service import seed_prompts_from_files


async def _run(only_keys: list[str] | None) -> int:
    if only_keys:
        # Filter is applied by listing only — for simplicity we always run the
        # full seed and just print the touched keys, since the seeder is fast
        # once the DB connection is up.
        pass
    inserted, updated, unchanged, updated_keys = await seed_prompts_from_files()
    print(f"Seed complete. inserted={inserted} updated={updated} unchanged={unchanged}")
    if updated_keys:
        print("Touched keys:")
        for k in updated_keys:
            print(f"  - {k}")
    if only_keys:
        missing = [k for k in only_keys if k not in updated_keys]
        if missing:
            print(f"Note: requested keys not in changed-set: {missing}")
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keys", nargs="*", default=None, help="Optional list of expected keys (informational).")
    args = parser.parse_args()
    return asyncio.run(_run(args.keys))


if __name__ == "__main__":
    raise SystemExit(_main())
