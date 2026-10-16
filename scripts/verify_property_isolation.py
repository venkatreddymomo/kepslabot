"""
Verify the multi-property guarantee from Phase 3.

Inserts two synthetic properties under a test hotel, each with its own books
and pages, plus one common (shared) book/page. Then asserts:

1. With property A active  → catalog/context returns A's books + common.
                              Zero pages from B leak in.
2. With property B active  → catalog/context returns B's books + common.
                              Zero pages from A leak in.
3. With NO active property → only common books are visible.
4. With both properties active → both A's and B's books + common visible.

Cleans up the synthetic rows when done.

Usage:
    python scripts/verify_property_isolation.py
    python scripts/verify_property_isolation.py --keep   # keep the test rows for inspection
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.database import (
    AsyncSessionLocal,
    Hotel,
    KBBook,
    KBBookPage,
    KBPage,
    Property,
    init_db,
)
from services.kb_table_reader import (
    build_book_context_from_ids_db,
    get_property_catalog_db,
    get_structured_book_catalog_db,
)


TEST_HOTEL_CODE = "_isolation_test_hotel"
TEST_PROPS = [
    {"slug": "alpha_resort", "name": "Alpha Resort"},
    {"slug": "beta_lodge", "name": "Beta Lodge"},
]
TEST_PAGES = {
    "alpha_resort": [
        ("alpha_p1", "Alpha Pool", "Pool open 6am-10pm at Alpha Resort."),
        ("alpha_p2", "Alpha Spa", "Spa hours 9am-9pm at Alpha Resort."),
    ],
    "beta_lodge": [
        ("beta_p1", "Beta Pool", "Pool open 7am-9pm at Beta Lodge."),
        ("beta_p2", "Beta Restaurant", "Dining 7am-11pm at Beta Lodge."),
    ],
    None: [
        ("common_p1", "Hotel Policies", "Check-in 3pm, check-out 11am."),
    ],
}
TEST_BOOKS = {
    "alpha_resort": [
        ("alpha_pool_book", "Alpha Pool Info", ["alpha_p1"]),
        ("alpha_spa_book", "Alpha Spa Info", ["alpha_p2"]),
    ],
    "beta_lodge": [
        ("beta_pool_book", "Beta Pool Info", ["beta_p1"]),
        ("beta_dining_book", "Beta Dining Info", ["beta_p2"]),
    ],
    None: [
        ("policies_book", "Hotel Policies", ["common_p1"]),
    ],
}


async def _setup(session) -> int:
    """Create test hotel + properties + pages + books. Returns hotel_id."""
    # Hotel (idempotent on code)
    stmt = (
        pg_insert(Hotel)
        .values(code=TEST_HOTEL_CODE, name="Isolation Test Hotel", city="Testville")
        .on_conflict_do_update(
            index_elements=[Hotel.code],
            set_={"name": "Isolation Test Hotel"},
        )
        .returning(Hotel.id)
    )
    hotel_id = (await session.execute(stmt)).scalar_one()

    # Properties
    prop_slug_to_id: dict[str, int] = {}
    for prop in TEST_PROPS:
        stmt = (
            pg_insert(Property)
            .values(hotel_id=hotel_id, property_slug=prop["slug"], name=prop["name"])
            .on_conflict_do_update(
                index_elements=[Property.hotel_id, Property.property_slug],
                set_={"name": prop["name"]},
            )
            .returning(Property.id)
        )
        prop_slug_to_id[prop["slug"]] = (await session.execute(stmt)).scalar_one()

    # Pages
    page_slug_to_id: dict[str, int] = {}
    for prop_slug, page_list in TEST_PAGES.items():
        prop_id = prop_slug_to_id.get(prop_slug) if prop_slug else None
        for page_slug, title, content in page_list:
            stmt = (
                pg_insert(KBPage)
                .values(
                    hotel_id=hotel_id,
                    property_id=prop_id,
                    page_slug=page_slug,
                    title=title,
                    content=content,
                    char_count=len(content),
                )
                .on_conflict_do_update(
                    index_elements=[KBPage.hotel_id, KBPage.page_slug],
                    set_={
                        "property_id": prop_id,
                        "title": title,
                        "content": content,
                        "char_count": len(content),
                    },
                )
                .returning(KBPage.id)
            )
            page_slug_to_id[page_slug] = (await session.execute(stmt)).scalar_one()

    # Books + join rows
    for prop_slug, book_list in TEST_BOOKS.items():
        prop_id = prop_slug_to_id.get(prop_slug) if prop_slug else None
        for book_slug, title, page_slugs in book_list:
            stmt = (
                pg_insert(KBBook)
                .values(
                    hotel_id=hotel_id,
                    property_id=prop_id,
                    book_slug=book_slug,
                    title=title,
                    description=f"Synthetic book: {title}",
                    page_count=len(page_slugs),
                )
                .on_conflict_do_update(
                    index_elements=[KBBook.hotel_id, KBBook.property_id, KBBook.book_slug],
                    set_={
                        "title": title,
                        "description": f"Synthetic book: {title}",
                        "page_count": len(page_slugs),
                    },
                )
                .returning(KBBook.id)
            )
            book_id = (await session.execute(stmt)).scalar_one()

            await session.execute(delete(KBBookPage).where(KBBookPage.book_id == book_id))
            for ordinal, page_slug in enumerate(page_slugs):
                page_id = page_slug_to_id[page_slug]
                await session.execute(
                    pg_insert(KBBookPage)
                    .values(book_id=book_id, page_id=page_id, ordinal=ordinal)
                    .on_conflict_do_nothing(index_elements=[KBBookPage.book_id, KBBookPage.page_id])
                )

    await session.commit()
    return hotel_id


async def _teardown(session, hotel_id: int) -> None:
    # CASCADE deletes will tear down properties / books / pages / joins.
    await session.execute(delete(Hotel).where(Hotel.id == hotel_id))
    await session.commit()


def _ok(label: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(': ' + detail) if detail else ''}")
    return condition


async def _run_assertions(hotel_code: str) -> bool:
    all_ok = True

    # 1) property A active → only A + common
    catalog = await get_structured_book_catalog_db(
        hotel_code=hotel_code, property_slugs=["alpha_resort"]
    )
    book_slugs = {b["id"] for b in catalog}
    print("\nScenario 1: property=alpha_resort active")
    all_ok &= _ok("alpha_pool_book present", "alpha_pool_book" in book_slugs)
    all_ok &= _ok("alpha_spa_book present", "alpha_spa_book" in book_slugs)
    all_ok &= _ok("policies_book (common) present", "policies_book" in book_slugs)
    all_ok &= _ok("beta_pool_book NOT present", "beta_pool_book" not in book_slugs)
    all_ok &= _ok("beta_dining_book NOT present", "beta_dining_book" not in book_slugs)

    # context: even if router somehow returns beta_pool_book, it must be filtered
    ctx = await build_book_context_from_ids_db(
        hotel_code=hotel_code,
        book_ids=["alpha_pool_book", "beta_pool_book"],  # one valid, one cross-property
        property_slugs=["alpha_resort"],
    )
    all_ok &= _ok(
        "context contains alpha_p1", "alpha_p1" in (ctx.page_ids or [])
    )
    all_ok &= _ok(
        "context does NOT contain beta_p1 (cross-property leakage check)",
        "beta_p1" not in (ctx.page_ids or []),
    )

    # 2) property B active → only B + common
    catalog = await get_structured_book_catalog_db(
        hotel_code=hotel_code, property_slugs=["beta_lodge"]
    )
    book_slugs = {b["id"] for b in catalog}
    print("\nScenario 2: property=beta_lodge active")
    all_ok &= _ok("beta_pool_book present", "beta_pool_book" in book_slugs)
    all_ok &= _ok("beta_dining_book present", "beta_dining_book" in book_slugs)
    all_ok &= _ok("policies_book (common) present", "policies_book" in book_slugs)
    all_ok &= _ok("alpha_pool_book NOT present", "alpha_pool_book" not in book_slugs)
    all_ok &= _ok("alpha_spa_book NOT present", "alpha_spa_book" not in book_slugs)

    # 3) no active property → only common visible
    catalog = await get_structured_book_catalog_db(hotel_code=hotel_code, property_slugs=None)
    book_slugs = {b["id"] for b in catalog}
    print("\nScenario 3: no active property")
    all_ok &= _ok("policies_book (common) present", "policies_book" in book_slugs)
    all_ok &= _ok("alpha_pool_book NOT present", "alpha_pool_book" not in book_slugs)
    all_ok &= _ok("beta_pool_book NOT present", "beta_pool_book" not in book_slugs)

    # 4) both properties active → both visible + common
    catalog = await get_structured_book_catalog_db(
        hotel_code=hotel_code, property_slugs=["alpha_resort", "beta_lodge"]
    )
    book_slugs = {b["id"] for b in catalog}
    print("\nScenario 4: both alpha_resort + beta_lodge active")
    all_ok &= _ok("alpha_pool_book present", "alpha_pool_book" in book_slugs)
    all_ok &= _ok("beta_pool_book present", "beta_pool_book" in book_slugs)
    all_ok &= _ok("policies_book (common) present", "policies_book" in book_slugs)

    # 5) property catalog (asked when no active property)
    props = await get_property_catalog_db(hotel_code=hotel_code)
    print("\nScenario 5: property catalog endpoint")
    all_ok &= _ok("property catalog has 2 entries", len(props) == 2, f"got {len(props)}")

    return all_ok


async def _main(keep: bool) -> int:
    await init_db()

    async with AsyncSessionLocal() as session:
        print("Setting up synthetic isolation fixtures...")
        hotel_id = await _setup(session)

    try:
        ok = await _run_assertions(TEST_HOTEL_CODE)
    finally:
        if not keep:
            print("\nCleaning up test rows...")
            async with AsyncSessionLocal() as session:
                await _teardown(session, hotel_id)
        else:
            print(
                f"\n--keep specified. Test rows remain (hotel.code={TEST_HOTEL_CODE}, hotel.id={hotel_id})."
            )

    print("\n" + "=" * 50)
    print("RESULT:", "ALL PASSED" if ok else "FAILURES — see above")
    print("=" * 50)
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep test rows after run")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.keep)))
