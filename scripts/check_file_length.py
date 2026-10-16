#!/usr/bin/env python3
"""File-length ratchet for first-party Python source.

Goal: stop the god-files from growing while NOT forcing an immediate (risky)
rewrite of the existing large files. It works as a ratchet:

  * Files already over budget are frozen at their baseline size — they may
    shrink freely, but a commit that makes them BIGGER fails the check.
  * Brand-new files must come in under ``SOFT_CAP`` (default 800 lines).

Usage:
    python scripts/check_file_length.py                 # check (CI gate)
    python scripts/check_file_length.py --update-baseline  # re-freeze current sizes

The baseline lives in scripts/file_length_baseline.json (commit it).
Vendored sub-apps (KB scraper, OCR pipeline, admin_ui, node_modules) and
generated dirs are out of scope.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "file_length_baseline.json"

# First-party source roots only.
INCLUDE_DIRS = [
    "api", "core", "services", "handlers", "llm",
    "models", "schemas", "bootstrap", "config", "integrations",
]
INCLUDE_ROOT_FILES = ["main.py"]

SOFT_CAP = 800  # max lines allowed for a NEW first-party file


def _iter_source_files():
    for name in INCLUDE_ROOT_FILES:
        p = REPO_ROOT / name
        if p.is_file():
            yield p
    for d in INCLUDE_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            yield p


def _line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _collect() -> dict[str, int]:
    return {_rel(p): _line_count(p) for p in _iter_source_files()}


def update_baseline() -> int:
    sizes = _collect()
    BASELINE_PATH.write_text(json.dumps(sizes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    over = sum(1 for n in sizes.values() if n > SOFT_CAP)
    print(f"Baseline written: {len(sizes)} files ({over} currently over the {SOFT_CAP}-line soft cap).")
    return 0


def check() -> int:
    sizes = _collect()
    baseline: dict[str, int] = {}
    if BASELINE_PATH.exists():
        try:
            baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except Exception:
            baseline = {}

    failures: list[str] = []
    for name, count in sorted(sizes.items()):
        if name in baseline:
            allowed = baseline[name]
            if count > allowed:
                failures.append(f"  GREW  {name}: {count} lines (baseline {allowed}) — split before growing further")
        else:
            if count > SOFT_CAP:
                failures.append(f"  NEW   {name}: {count} lines (> {SOFT_CAP} soft cap for new files)")

    if failures:
        print("File-length ratchet FAILED:")
        print("\n".join(failures))
        print("\nFix by splitting the file, or run --update-baseline if the growth is intentional.")
        return 1

    biggest = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:5]
    print("File-length ratchet OK. Largest first-party files (frozen at baseline):")
    for name, count in biggest:
        print(f"  {count:>6}  {name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update-baseline", action="store_true", help="re-freeze current sizes as the baseline")
    args = ap.parse_args()
    return update_baseline() if args.update_baseline else check()


if __name__ == "__main__":
    sys.exit(main())
