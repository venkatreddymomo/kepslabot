"""One-shot uploader for Khil's per-property image XLSX exports.

What this does
--------------
Reads every property folder inside the export root, matches it to a row in
``new_bot_properties`` under the ``khil`` hotel, parses the ``<folder>_images.xlsx``
file (sheet ``Images``: col B = title, col C = image URL, col A = ignored row #),
and upserts the rows into ``bot_hotel_images_scraper`` scoped to that property.

By default this runs **dry**: it prints the folder->property mapping plus image
counts and writes nothing. Pass ``--commit`` to actually upsert.

Pure DB. No HTTP, no JSON. Uses the same ``AsyncSessionLocal`` the app uses,
so the active ``DATABASE_URL`` (or its env override) controls where rows land.

Why a new script and not the existing ``import_image_assets.py``: that one
predates the per-property feature and writes a single flat list against a
hotel — it has no notion of ``property_id`` and would lump all 22 folders
together under one bucket. Keeping it intact.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import select


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


DEFAULT_EXPORT_ROOT = Path(r"C:\Users\Hp\Downloads\khil_20260504_130520\khil_20260504_130520")
DEFAULT_HOTEL_CODE = "khil"
DEFAULT_SKIP_FOLDERS = {"ira_by_orchid_ayodhya", "fort_jadhavgadh"}

# Explicit folder-slug -> property-slug overrides where heuristic matching
# can't bridge the gap. Kept as a small known list rather than fuzzy guesses
# because cross-city mismatches would be silent and unrecoverable.
#
# - aurangabad: the city was officially renamed to Sambhajinagar. The DB row
#   already uses the new name; the export still uses the old one.
# - bhubaneswar vs bhubaneshwar: spelling variant (DB carries the extra 'h').
FOLDER_PROPERTY_SLUG_OVERRIDES: dict[str, str] = {
    "ira_by_orchid_aurangabad": "ira_orchid_sambhajinagar",
    "ira_by_orchid_bhubaneswar": "ira_orchid_bhubaneshwar",
}


def _slugify(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return text.strip("_")


def _normalize_title(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if text.lower() in {"none", "nan", "null", "n/a", "na"}:
        return ""
    return text


def _read_images_from_xlsx(xlsx_path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return (valid_rows, skipped_rows). Reads sheet 'Images' specifically so
    the Summary sheet that ships with every export doesn't bleed in as data.
    """
    wb = load_workbook(filename=str(xlsx_path), read_only=True, data_only=True)
    sheet_name = "Images" if "Images" in wb.sheetnames else wb.sheetnames[0]
    ws = wb[sheet_name]

    valid: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    seen_titles: dict[str, int] = {}

    for idx, row in enumerate(ws.iter_rows(values_only=True)):
        if row is None:
            continue
        # Layout: A=row#/index (ignored), B=title, C=image_url, rest ignored.
        title_raw = row[1] if len(row) > 1 else ""
        url_raw = row[2] if len(row) > 2 else ""
        title = _normalize_title(title_raw)
        url = str(url_raw or "").strip()

        # Header row: first row without a URL like "https://..." is dropped.
        if idx == 0 and not url.lower().startswith(("http://", "https://")):
            continue

        if not title and not url:
            continue
        if not title:
            skipped.append({"row": str(idx + 1), "label": "", "url": url, "reason": "empty_label"})
            continue
        if not url:
            skipped.append({"row": str(idx + 1), "label": title, "url": "", "reason": "empty_url"})
            continue
        if not url.lower().startswith(("http://", "https://")):
            skipped.append({"row": str(idx + 1), "label": title, "url": url, "reason": "non_http_url"})
            continue

        key = title.lower()
        if key in seen_titles:
            # Later row wins for same label, mirroring the bulk endpoint.
            prev_idx = seen_titles[key]
            skipped.append(
                {
                    "row": str(valid[prev_idx]["row_no"]),
                    "label": valid[prev_idx]["title"],
                    "url": valid[prev_idx]["image_url"],
                    "reason": f"duplicate_in_file_superseded_by_row_{idx + 1}",
                }
            )
            valid[prev_idx] = {
                "row_no": str(idx + 1),
                "title": title,
                "image_url": url,
            }
            continue
        seen_titles[key] = len(valid)
        valid.append({"row_no": str(idx + 1), "title": title, "image_url": url})

    return valid, skipped


def _build_property_match_index(rows: list[Any]) -> dict[str, Any]:
    """Map every possible identifier (slug / normalized name / normalized alias)
    to the Property row, so a folder slug can resolve via any of them.
    """
    index: dict[str, Any] = {}
    for row in rows:
        keys: set[str] = set()
        slug = _slugify(getattr(row, "property_slug", None))
        if slug:
            keys.add(slug)
        keys.add(_slugify(getattr(row, "name", None)))
        aliases = getattr(row, "aliases", None) or []
        if isinstance(aliases, list):
            for alias in aliases:
                key = _slugify(alias)
                if key:
                    keys.add(key)
        for key in keys:
            if not key:
                continue
            # First writer wins. Ambiguous labels would have to be resolved
            # manually anyway; we surface them in the dry-run.
            index.setdefault(key, row)
    return index


def _resolve_folder_to_property(folder_slug: str, match_index: dict[str, Any]) -> tuple[Any, str]:
    """Return (matched_row | None, match_method). Tries exact, then progressively
    looser variants (drop common prefixes like 'the_' / 'ira_by_').
    """
    # Hardcoded overrides take precedence — silent fuzzy matches across cities
    # are dangerous (images for one city ending up under another), so the
    # known-tricky mappings live in an explicit table.
    override_slug = FOLDER_PROPERTY_SLUG_OVERRIDES.get(folder_slug)
    if override_slug:
        match = match_index.get(override_slug)
        if match is not None:
            return match, f"override:{override_slug}"

    direct = match_index.get(folder_slug)
    if direct is not None:
        return direct, "exact"

    candidates = [folder_slug]
    # Strip common decorative prefixes used in the export folder names.
    for prefix in ("the_", "ira_by_", "ira_orchid_", "ira_"):
        if folder_slug.startswith(prefix):
            candidates.append(folder_slug[len(prefix):])
    # Swap "by_orchid" -> "orchid" and "orchid_hotel" -> "orchid" type variants.
    derived = folder_slug.replace("by_orchid_", "orchid_")
    if derived != folder_slug:
        candidates.append(derived)
    derived = folder_slug.replace("orchid_hotel_", "orchid_")
    if derived != folder_slug:
        candidates.append(derived)

    for candidate in candidates:
        hit = match_index.get(candidate)
        if hit is not None:
            return hit, f"prefix_strip:{candidate}"

    # Last resort: substring overlap on the slug. Pick the longest matching key.
    best: tuple[int, Any, str] = (0, None, "")
    for key, row in match_index.items():
        if not key:
            continue
        if key in folder_slug or folder_slug in key:
            overlap = min(len(key), len(folder_slug))
            if overlap > best[0]:
                best = (overlap, row, f"substring:{key}")
    if best[1] is not None and best[0] >= 6:  # require non-trivial overlap
        return best[1], best[2]
    return None, "no_match"


async def _upsert_for_property(
    *,
    session,
    hotel_id: int,
    property_id: int,
    rows: list[dict[str, str]],
    source_label: str,
    image_asset_model,
) -> tuple[int, int]:
    """Upsert within (hotel_id, property_id, title.lower()) scope. Matches the
    behavior of the bulk endpoint we already shipped.
    """
    existing = (
        await session.execute(
            select(image_asset_model).where(
                image_asset_model.hotel_id == hotel_id,
                image_asset_model.property_id == property_id,
            )
        )
    ).scalars().all()
    by_label: dict[str, Any] = {}
    for asset in existing:
        key = _normalize_title(getattr(asset, "title", "")).lower()
        if key:
            by_label[key] = asset

    inserted = 0
    updated = 0
    for row in rows:
        key = row["title"].lower()
        match = by_label.get(key)
        if match is not None:
            match.title = row["title"]
            match.image_url = row["image_url"]
            if source_label:
                match.source_label = source_label
            updated += 1
        else:
            new = image_asset_model(
                hotel_id=hotel_id,
                property_id=property_id,
                title=row["title"],
                image_url=row["image_url"],
                source_label=source_label,
                is_active=True,
                priority=0,
            )
            session.add(new)
            by_label[key] = new
            inserted += 1
    return inserted, updated


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export-root",
        type=Path,
        default=DEFAULT_EXPORT_ROOT,
        help=f"Folder containing one sub-folder per property (default: {DEFAULT_EXPORT_ROOT})",
    )
    parser.add_argument(
        "--hotel-code",
        default=DEFAULT_HOTEL_CODE,
        help=f"new_bot_hotels.code to upsert under (default: {DEFAULT_HOTEL_CODE})",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=None,
        help=(
            "Folder slug to skip (repeatable). Defaults to "
            f"{sorted(DEFAULT_SKIP_FOLDERS)} — already uploaded manually."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to the DB. Without this flag the script is dry-run only.",
    )
    args = parser.parse_args()

    skip_set = set(args.skip if args.skip is not None else DEFAULT_SKIP_FOLDERS)
    export_root: Path = args.export_root

    if not export_root.exists():
        print(f"ERROR: export root not found: {export_root}", file=sys.stderr)
        return 2

    # Import lazily so the script's --help doesn't drag in the whole app stack.
    from models.database import AsyncSessionLocal, Hotel, ImageAsset, Property
    from services.image_tool_service import image_tool_service

    async with AsyncSessionLocal() as session:
        hotel = (
            await session.execute(select(Hotel).where(Hotel.code == args.hotel_code))
        ).scalar_one_or_none()
        if hotel is None:
            print(
                f"ERROR: no row in new_bot_hotels with code={args.hotel_code!r}. "
                f"Aborting — would otherwise create images under the wrong tenant.",
                file=sys.stderr,
            )
            return 3
        hotel_id = int(hotel.id)
        print(f"Hotel: id={hotel_id} code={hotel.code!r} name={hotel.name!r}")

        property_rows = (
            await session.execute(
                select(Property).where(
                    Property.hotel_id == hotel_id,
                    Property.is_active == True,  # noqa: E712
                )
            )
        ).scalars().all()
        print(f"Active properties under this hotel: {len(property_rows)}")
        if not property_rows:
            print(
                "ERROR: no sub-properties found under this hotel. Aborting — "
                "every image must scope to a property_id.",
                file=sys.stderr,
            )
            return 4

        match_index = _build_property_match_index(property_rows)

    # Walk the export root.
    folders = sorted(p for p in export_root.iterdir() if p.is_dir())
    print(f"\nExport folders found: {len(folders)} (skipping: {sorted(skip_set)})\n")

    plan: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    summary_counts = defaultdict(int)

    for folder in folders:
        folder_slug = _slugify(folder.name)
        if folder_slug in skip_set:
            summary_counts["skipped_by_request"] += 1
            print(f"  SKIP  {folder.name}  (in --skip)")
            continue

        xlsx_path = folder / f"{folder.name}_images.xlsx"
        if not xlsx_path.exists():
            # Fallback: any *.xlsx inside the folder.
            candidates = list(folder.glob("*_images.xlsx"))
            if candidates:
                xlsx_path = candidates[0]
        if not xlsx_path.exists():
            summary_counts["missing_xlsx"] += 1
            unresolved.append({"folder": folder.name, "reason": "missing_xlsx"})
            print(f"  MISS  {folder.name}  -> no *_images.xlsx file")
            continue

        prop, match_method = _resolve_folder_to_property(folder_slug, match_index)
        if prop is None:
            summary_counts["unresolved_property"] += 1
            unresolved.append({"folder": folder.name, "reason": "no_property_match"})
            print(f"  ????  {folder.name}  -> no Property row matched")
            continue

        try:
            rows, skipped_rows = _read_images_from_xlsx(xlsx_path)
        except Exception as exc:
            summary_counts["xlsx_parse_failed"] += 1
            unresolved.append({"folder": folder.name, "reason": f"xlsx_parse_failed: {exc}"})
            print(f"  ERR   {folder.name}  -> xlsx parse failed: {exc}")
            continue

        plan.append(
            {
                "folder": folder.name,
                "property_id": int(prop.id),
                "property_slug": str(prop.property_slug),
                "property_name": str(prop.name),
                "match_method": match_method,
                "rows": rows,
                "skipped_rows": skipped_rows,
                "xlsx_path": xlsx_path,
            }
        )
        summary_counts["matched"] += 1
        print(
            f"  OK    {folder.name}"
            f"  -> property #{prop.id} {prop.name!r} ({match_method})"
            f"  rows={len(rows)} skipped={len(skipped_rows)}"
        )

    print(f"\n--- DRY RUN MAPPING ({len(plan)} folders to upload) ---")
    for entry in plan:
        print(
            f"  {entry['folder']:50s} -> property_id={entry['property_id']:4d}"
            f"  '{entry['property_name']}'  ({entry['match_method']})"
            f"  rows={len(entry['rows'])}"
        )

    if unresolved:
        print(f"\n--- UNRESOLVED ({len(unresolved)}) ---")
        for item in unresolved:
            print(f"  {item['folder']:50s}  {item['reason']}")

    print(f"\nSummary: {dict(summary_counts)}")

    if not args.commit:
        print("\nDRY RUN — no DB writes performed. Re-run with --commit to upload.")
        return 0

    if unresolved:
        print(
            "\nABORTING --commit: unresolved entries above. Resolve them "
            "(rename folder, add alias to property, or pass --skip) before committing.",
            file=sys.stderr,
        )
        return 5

    # Real upload phase.
    print("\n--- COMMITTING ---")
    total_inserted = 0
    total_updated = 0
    async with AsyncSessionLocal() as session:
        for entry in plan:
            inserted, updated = await _upsert_for_property(
                session=session,
                hotel_id=hotel_id,
                property_id=entry["property_id"],
                rows=entry["rows"],
                source_label=Path(entry["xlsx_path"]).name,
                image_asset_model=ImageAsset,
            )
            await session.commit()
            total_inserted += inserted
            total_updated += updated
            print(
                f"  {entry['folder']:50s}  inserted={inserted}  updated={updated}"
            )

    # Make sure smart_image_gallery is enabled for this hotel so chat actually
    # surfaces what we just uploaded (no-op if already registered).
    try:
        await image_tool_service.ensure_tool_registered(args.hotel_code, enabled=True)
        image_tool_service.invalidate_candidate_cache(args.hotel_code)
        image_tool_service.invalidate_enabled_cache(args.hotel_code)
    except Exception as exc:
        print(f"  WARN: tool register / cache invalidate failed: {exc}", file=sys.stderr)

    print(f"\nDONE. Total inserted={total_inserted}  updated={total_updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
