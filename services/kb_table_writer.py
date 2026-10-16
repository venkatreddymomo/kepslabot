"""
Phase 4 â€” Dual-write the structured library into the new KB tables.

Mirrors the JSON-blob library into:
- new_bot_properties
- new_bot_kb_pages
- new_bot_kb_books
- new_bot_kb_book_pages

Called from config_service after rebuild_structured_kb_library /
ensure_structured_kb_llm_books has saved the JSON blob, so the JSON path
remains the source of truth for one release while the tables get populated.

Idempotent (upserts on slug-based keys). Safe to call repeatedly.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Sequence

from sqlalchemy import Column, delete, select
from sqlalchemy.exc import DBAPIError, OperationalError

from models.database import (
    AsyncSessionLocal,
    Hotel,
    KBBook,
    KBBookPage,
    KBPage,
    Property,
)


def _is_deadlock_or_lock_timeout(exc: BaseException) -> bool:
    """MySQL: 1213 = deadlock, 1205 = lock wait timeout. Postgres: SQLSTATE 40P01."""
    args = getattr(exc, "args", ()) or ()
    for arg in args:
        if isinstance(arg, int) and arg in (1205, 1213):
            return True
        text = str(arg or "").lower()
        if "1213" in text or "1205" in text or "deadlock" in text or "lock wait" in text:
            return True
    orig = getattr(exc, "orig", None)
    if orig is not None and orig is not exc:
        return _is_deadlock_or_lock_timeout(orig)
    return False


async def _run_with_deadlock_retry(coro_factory, *, max_attempts: int = 5, base_delay: float = 0.15):
    """Run an async coroutine with exponential-backoff retry on MySQL deadlocks.

    `coro_factory` must be a no-arg callable that returns a fresh coroutine
    each call (because retries can't reuse a finished coroutine).
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except (OperationalError, DBAPIError) as exc:
            if not _is_deadlock_or_lock_timeout(exc):
                raise
            last_exc = exc
            if attempt >= max_attempts:
                break
            sleep_for = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
            await asyncio.sleep(sleep_for)
    if last_exc is not None:
        raise last_exc


def _coerce_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


async def _upsert_and_get_id(
    session,
    model,
    *,
    values: dict[str, Any],
    conflict_cols: Sequence[Column],
    update_keys: Sequence[str],
    id_col: Column,
) -> int:
    """Cross-dialect upsert that returns the affected row's id.

    Uses PG's ``ON CONFLICT DO UPDATE ... RETURNING`` when running against
    PostgreSQL, and the MySQL ``ON DUPLICATE KEY UPDATE`` + follow-up
    ``SELECT`` when running against MySQL. Either way the caller gets the
    id of the row that now matches ``values`` for the unique-key columns.
    """
    backend = str(getattr(session.bind, "dialect", None) and session.bind.dialect.name or "").lower()
    if not backend:
        # Async sessions sometimes hide the bind on the engine — fall back to
        # the global engine's dialect name.
        from models.database import engine as _engine
        backend = str(_engine.dialect.name or "").lower()

    update_set = {k: values.get(k) for k in update_keys}

    if backend in {"postgresql", "postgres"}:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(model)
            .values(**values)
            .on_conflict_do_update(
                index_elements=list(conflict_cols),
                set_=update_set,
            )
            .returning(id_col)
        )
        return int((await session.execute(stmt)).scalar_one())

    # MySQL path — ON DUPLICATE KEY UPDATE then read back by the unique key.
    from sqlalchemy.dialects.mysql import insert as mysql_insert

    stmt = mysql_insert(model).values(**values)
    stmt = stmt.on_duplicate_key_update(**update_set)
    await session.execute(stmt)

    where_clauses = []
    for col in conflict_cols:
        col_name = col.key  # python attribute name on the model
        where_clauses.append(col == values.get(col_name))
    sel = select(id_col).where(*where_clauses).limit(1)
    return int((await session.execute(sel)).scalar_one())


async def _upsert_link_ignore(
    session,
    model,
    *,
    values: dict[str, Any],
    conflict_cols: Sequence[Column],
) -> None:
    """Cross-dialect 'insert if not exists' for link/join tables.

    PG: ``ON CONFLICT DO NOTHING`` keyed on ``conflict_cols``.
    MySQL: ``INSERT IGNORE`` (the natural equivalent — duplicate-key errors
    are silently swallowed since the unique constraint is the same).
    """
    backend = str(getattr(session.bind, "dialect", None) and session.bind.dialect.name or "").lower()
    if not backend:
        from models.database import engine as _engine
        backend = str(_engine.dialect.name or "").lower()

    if backend in {"postgresql", "postgres"}:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(model)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(conflict_cols))
        )
        await session.execute(stmt)
        return

    from sqlalchemy.dialects.mysql import insert as mysql_insert

    stmt = mysql_insert(model).values(**values).prefix_with("IGNORE")
    await session.execute(stmt)


async def persist_library_to_tables(
    hotel_code: str,
    library_index: dict[str, Any],
    property_scope_index: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Mirror the just-built library into the relational tables.

    Wrapped with deadlock-retry: MySQL error 1213 (deadlock) and 1205 (lock
    wait timeout) are retried with exponential backoff. KB writes can race
    with each other (same hotel, two simultaneous uploads or refresh +
    upload) and the gap-locks on the `WHERE book_id IN (SELECT ...)` DELETE
    can deadlock — the retry handles it transparently so the user never
    sees the 1213 error spam in logs.
    """
    return await _run_with_deadlock_retry(
        lambda: _persist_library_to_tables_once(
            hotel_code=hotel_code,
            library_index=library_index,
            property_scope_index=property_scope_index,
        )
    )


async def _persist_library_to_tables_once(
    *,
    hotel_code: str,
    library_index: dict[str, Any],
    property_scope_index: dict[str, Any] | None,
) -> dict[str, int]:
    counts = {"properties": 0, "pages": 0, "books": 0, "book_pages": 0}
    if not isinstance(library_index, dict) or not library_index:
        return counts

    pages = _coerce_list(library_index.get("pages"))
    books = _coerce_list(library_index.get("books"))
    if not pages and not books:
        return counts

    scope = property_scope_index if isinstance(property_scope_index, dict) else {}
    properties_raw = _coerce_list(scope.get("properties"))
    common_page_ids = {str(pid) for pid in _coerce_list(scope.get("common_page_ids"))}

    async with AsyncSessionLocal() as session:
        hotel_id = (
            await session.execute(select(Hotel.id).where(Hotel.code == hotel_code))
        ).scalar_one_or_none()
        if not hotel_id:
            return counts

        # One-time migration: drop legacy rows where property_id IS NULL for
        # this hotel. They were created before we always-assigned a property
        # and they coexist as duplicates because PG's UNIQUE treats NULL as
        # not-equal-to-NULL. Hotel-scoped â€” never touches other hotels.
        await session.execute(
            delete(KBBookPage).where(
                KBBookPage.book_id.in_(
                    select(KBBook.id).where(
                        KBBook.hotel_id == hotel_id, KBBook.property_id.is_(None)
                    )
                )
            )
        )
        await session.execute(
            delete(KBBook).where(
                KBBook.hotel_id == hotel_id, KBBook.property_id.is_(None)
            )
        )
        await session.execute(
            delete(KBPage).where(
                KBPage.hotel_id == hotel_id, KBPage.property_id.is_(None)
            )
        )

        # ------------------------------------------------------------------
        # Properties (multi-property scope index)
        #
        # Invariant after this block: prop_slug_to_id has at least one entry
        # ('default' for single-property hotels, 'shared' for multi-property)
        # and every page/book that would otherwise have a NULL property_id
        # falls back to that sentinel id. NULL property_id is never written.
        # ------------------------------------------------------------------
        page_to_property: dict[str, int | None] = {}
        prop_slug_to_id: dict[str, int] = {}

        for row in properties_raw:
            if not isinstance(row, dict):
                continue
            slug = str(row.get("id") or "").strip()
            if not slug:
                continue
            name = str(row.get("name") or slug.replace("_", " ").title()).strip()
            aliases = [str(a).strip() for a in _coerce_list(row.get("aliases")) if str(a).strip()]
            source_names = [
                str(s).strip() for s in _coerce_list(row.get("source_names")) if str(s).strip()
            ]

            prop_slug_to_id[slug] = await _upsert_and_get_id(
                session,
                Property,
                values={
                    "hotel_id": hotel_id,
                    "property_slug": slug,
                    "name": name,
                    "aliases": aliases or None,
                    "source_names": source_names or None,
                    "is_active": True,
                    "is_default": False,
                },
                conflict_cols=[Property.hotel_id, Property.property_slug],
                update_keys=["name", "aliases", "source_names"],
                id_col=Property.id,
            )
            counts["properties"] += 1

            for page_id in _coerce_list(row.get("page_ids")):
                page_to_property[str(page_id)] = prop_slug_to_id[slug]

        # Fallback property for pages/books with no specific assignment.
        # Goal: never write NULL property_id (would create duplicates under
        # PG's UNIQUE-with-NULL semantics).
        #   - single-property hotels: create a 'default' row (also flagged
        #     is_default). It IS the property — admin sees one row, expected.
        #   - multi-property hotels: do NOT create a 'shared' meta row. Any
        #     page/book without a specific property falls back to the first
        #     real property (lowest id, deterministic). The admin sees only
        #     the real properties they uploaded — no synthetic 'shared' row.
        if not prop_slug_to_id:
            sentinel_property_id = await _upsert_and_get_id(
                session,
                Property,
                values={
                    "hotel_id": hotel_id,
                    "property_slug": "default",
                    "name": "Default",
                    "is_active": True,
                    "is_default": True,
                },
                conflict_cols=[Property.hotel_id, Property.property_slug],
                update_keys=["is_active", "is_default"],
                id_col=Property.id,
            )
            prop_slug_to_id["default"] = sentinel_property_id
            counts["properties"] += 1
        else:
            # Multi-property: deterministic first-real-property fallback.
            sentinel_property_id = min(prop_slug_to_id.values())

        # ------------------------------------------------------------------
        # Pages
        # ------------------------------------------------------------------
        page_slug_to_id: dict[str, int] = {}
        seen_page_slugs: set[str] = set()

        for page in pages:
            if not isinstance(page, dict):
                continue
            slug = str(page.get("id") or "").strip()
            if not slug or slug in seen_page_slugs:
                continue
            seen_page_slugs.add(slug)

            resolved_property_id = page_to_property.get(slug)
            if slug in common_page_ids:
                resolved_property_id = sentinel_property_id
            if resolved_property_id is None:
                resolved_property_id = sentinel_property_id

            topics = [str(t).strip() for t in _coerce_list(page.get("topics")) if str(t).strip()]
            content_text = str(page.get("text") or "")

            page_values = {
                "hotel_id": hotel_id,
                "property_id": resolved_property_id,
                "document_id": str(page.get("document_id") or "") or None,
                "page_slug": slug,
                "title": str(page.get("title") or "") or None,
                "content": content_text,
                "source_name": str(page.get("source_name") or "") or None,
                "source_path": str(page.get("source_path") or "") or None,
                "location": str(page.get("location") or "") or None,
                "topics": topics or None,
                "char_count": int(page.get("char_count") or len(content_text)),
            }
            page_slug_to_id[slug] = await _upsert_and_get_id(
                session,
                KBPage,
                values=page_values,
                conflict_cols=[KBPage.hotel_id, KBPage.page_slug],
                update_keys=[
                    "property_id", "title", "content", "source_name",
                    "source_path", "location", "topics", "char_count",
                ],
                id_col=KBPage.id,
            )
            counts["pages"] += 1

        # ------------------------------------------------------------------
        # Books + join rows
        #
        # Track which (property_id, book_slug) pairs we wrote this run so we
        # can drop stale books afterwards. Scoped to the current hotel, so
        # this never disturbs other hotels' data.
        # ------------------------------------------------------------------
        written_book_keys: set[tuple[int, str]] = set()
        for book in books:
            if not isinstance(book, dict):
                continue
            slug = str(book.get("id") or "").strip()
            if not slug:
                continue

            book_page_slugs = [str(p) for p in _coerce_list(book.get("page_ids")) if str(p)]

            # Property assignment for this book.
            #
            # Preferred path (sequential aggregator, current behavior):
            # the book entry already carries a ``property_slug`` field — the
            # aggregator built each book inside one property's group, so the
            # whole book belongs to that property by construction. Trust it.
            #
            # Legacy fallback: when no ``property_slug`` is present (older
            # library JSON or callers that haven't been migrated), infer the
            # property by majority vote from each page's ``page_to_property``
            # mapping. Same shape as the previous behavior so old data still
            # writes cleanly.
            explicit_property_slug = str(book.get("property_slug") or "").strip()
            pages_by_property: dict[int, list[str]] = {}
            if explicit_property_slug and explicit_property_slug in prop_slug_to_id:
                pages_by_property[int(prop_slug_to_id[explicit_property_slug])] = list(book_page_slugs)
            else:
                for page_slug in book_page_slugs:
                    pid = page_to_property.get(page_slug)
                    if page_slug in common_page_ids or pid is None:
                        pid = sentinel_property_id
                    pid_int = int(pid)
                    pages_by_property.setdefault(pid_int, []).append(page_slug)

            if not pages_by_property:
                # No pages at all — fall back so the book row still exists.
                pages_by_property[int(sentinel_property_id)] = []

            aliases = [str(a).strip() for a in _coerce_list(book.get("aliases")) if str(a).strip()]
            keywords = [str(k).strip() for k in _coerce_list(book.get("keywords")) if str(k).strip()]
            source_names = [
                str(s).strip() for s in _coerce_list(book.get("sources")) if str(s).strip()
            ]
            description = str(book.get("description") or "").strip()
            if not description:
                description = str(book.get("title") or slug).strip() or slug
            base_title = str(book.get("title") or slug).strip() or slug

            for resolved_book_property_id, sub_pages in pages_by_property.items():
                sub_char_count = 0
                # Recompute char_count for this property's slice. If the
                # caller provided one for the merged book, fall back to it
                # only when we can't derive a per-property number.
                book_values = {
                    "hotel_id": hotel_id,
                    "property_id": resolved_book_property_id,
                    "book_slug": slug,
                    "title": base_title,
                    "description": description,
                    "aliases": aliases or None,
                    "keywords": keywords or None,
                    "page_count": len(sub_pages) if sub_pages else int(book.get("page_count") or 0),
                    "char_count": sub_char_count or int(book.get("char_count") or 0),
                    "source_names": source_names or None,
                    "is_active": True,
                }
                book_id = await _upsert_and_get_id(
                    session,
                    KBBook,
                    values=book_values,
                    conflict_cols=[KBBook.hotel_id, KBBook.property_id, KBBook.book_slug],
                    update_keys=[
                        "title", "description", "aliases", "keywords",
                        "page_count", "char_count", "source_names", "is_active",
                    ],
                    id_col=KBBook.id,
                )
                counts["books"] += 1
                written_book_keys.add((int(resolved_book_property_id), slug))

                await session.execute(delete(KBBookPage).where(KBBookPage.book_id == book_id))
                for ordinal, page_slug in enumerate(sub_pages):
                    page_id = page_slug_to_id.get(page_slug)
                    if not page_id:
                        continue
                    await _upsert_link_ignore(
                        session,
                        KBBookPage,
                        values={"book_id": book_id, "page_id": page_id, "ordinal": ordinal},
                        conflict_cols=[KBBookPage.book_id, KBBookPage.page_id],
                    )
                    counts["book_pages"] += 1

        # Authoritative cleanup: for each property scope included in this run,
        # drop books no longer present in source. Property-scoped so books in
        # other properties are untouched.
        touched_property_ids = {int(pid) for pid in prop_slug_to_id.values()}
        for pid in touched_property_ids:
            kept_slugs = {slug for owner, slug in written_book_keys if owner == pid}
            stale_query = select(KBBook.id).where(
                KBBook.hotel_id == hotel_id,
                KBBook.property_id == pid,
            )
            if kept_slugs:
                stale_query = stale_query.where(~KBBook.book_slug.in_(kept_slugs))
            stale_book_ids = ((await session.execute(stale_query)).scalars().all())
            if stale_book_ids:
                await session.execute(
                    delete(KBBookPage).where(KBBookPage.book_id.in_(stale_book_ids))
                )
                await session.execute(
                    delete(KBBook).where(KBBook.id.in_(stale_book_ids))
                )

        await session.commit()
    return counts
