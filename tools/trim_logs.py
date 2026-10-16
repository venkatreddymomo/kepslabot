# -*- coding: utf-8 -*-
"""Trim logs/ to the last N days. Keep-last-7-days is the standing policy.

Files are append-only and chronological, so we binary-search the byte offset of
the first record dated >= cutoff and keep only the tail. Three timestamp
families (see the log-trim memory):
  * JSONL      -> first key "timestamp" (decisions/decisions.jsonl uses "ts")
  * block text -> lines starting "[YYYY-MM-DD HH:MM:SS"
  * new_detailed.log -> date only on an inner "Timestamp   : YYYY-MM-DD" line

Usage: python tools/trim_logs.py [--days 7] [--apply]
"""
import os, re, sys, json, argparse, shutil

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
DATE_RE      = re.compile(rb'"(?:timestamp|ts)"\s*:\s*"(\d{4}-\d{2}-\d{2})')
BLOCK_RE     = re.compile(rb'^\[(\d{4}-\d{2}-\d{2})')
DETAILED_RE  = re.compile(rb'Timestamp\s*:\s*(\d{4}-\d{2}-\d{2})')

def date_of_line(line, kind):
    if kind == "jsonl":
        m = DATE_RE.search(line)
    elif kind == "detailed":
        m = DETAILED_RE.search(line)
    else:
        m = BLOCK_RE.match(line)
    return m.group(1).decode() if m else None

def kind_for(path):
    base = os.path.basename(path).lower()
    if base == "new_detailed.log":
        return "detailed"
    if base.endswith(".jsonl") or base == "observability.log":
        return "jsonl"
    return "block"

def first_offset_ge(path, cutoff, kind):
    """Binary-search the first line whose date >= cutoff. Returns byte offset."""
    size = os.path.getsize(path)
    lo, hi = 0, size
    best = None
    with open(path, "rb") as f:
        while lo < hi:
            mid = (lo + hi) // 2
            f.seek(mid)
            if mid:
                f.readline()                      # discard partial line
            pos = f.tell()
            d = None
            for _ in range(400):                  # scan forward for a dated line
                line = f.readline()
                if not line:
                    break
                d = date_of_line(line, kind)
                if d:
                    break
            if d is None:
                hi = mid                          # no date ahead; shrink (hi=mid, never pos)
                continue
            if d >= cutoff:
                best = pos
                hi = mid                          # MUST be mid, else hi can grow and we loop forever
            else:
                lo = f.tell()
        return best if best is not None else (0 if lo == 0 else size)

def has_data_in_window(path, cutoff, kind, probe=3_000_000):
    """Cheap guard: does the tail contain any date >= cutoff?"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - probe))
        f.readline()
        for line in f:
            d = date_of_line(line, kind)
            if d and d >= cutoff:
                return True
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-mb", type=float, default=1.0)
    a = ap.parse_args()
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=a.days)).isoformat()
    print(f"cutoff = keep entries dated >= {cutoff}   ({'APPLY' if a.apply else 'DRY RUN'})\n")
    files = []
    for dp, _, fns in os.walk(ROOT):
        for fn in fns:
            p = os.path.join(dp, fn)
            try:
                if os.path.getsize(p) >= a.min_mb * 1024 * 1024:
                    files.append(p)
            except OSError:
                pass
    files.sort(key=os.path.getsize, reverse=True)
    tot_before = tot_after = 0
    for p in files:
        size = os.path.getsize(p)
        kind = kind_for(p)
        rel = os.path.relpath(p, ROOT)
        try:
            if not has_data_in_window(p, cutoff, kind):
                print(f"  {size/1048576:9.1f} MB  {rel:<42} SKIP (nothing newer than cutoff)")
                tot_before += size; tot_after += size
                continue
            off = first_offset_ge(p, cutoff, kind)
        except Exception as e:
            print(f"  {size/1048576:9.1f} MB  {rel:<42} SKIP ({type(e).__name__})")
            tot_before += size; tot_after += size
            continue
        keep = size - off
        if off <= 0 or keep <= 0:
            print(f"  {size/1048576:9.1f} MB  {rel:<42} SKIP (would keep {keep} bytes)")
            tot_before += size; tot_after += size
            continue
        tot_before += size; tot_after += keep
        print(f"  {size/1048576:9.1f} MB  {rel:<42} -> {keep/1048576:8.1f} MB  (drop {(off)/1048576:.1f} MB)")
        if a.apply:
            # In-place: shift the tail down inside the SAME file, then truncate.
            # Replacing the file fails while uvicorn holds it open on Windows.
            try:
                with open(p, "r+b") as f:
                    src, dst, buf = off, 0, 8 * 1024 * 1024
                    while True:
                        f.seek(src)
                        chunk = f.read(buf)
                        if not chunk:
                            break
                        f.seek(dst)
                        f.write(chunk)
                        src += len(chunk); dst += len(chunk)
                    f.truncate(dst)
                    f.flush(); os.fsync(f.fileno())
            except Exception as e:
                print(f"      !! in-place trim failed: {type(e).__name__}: {e}")
    print(f"\n  TOTAL  {tot_before/1073741824:.2f} GB -> {tot_after/1073741824:.2f} GB")
main()
