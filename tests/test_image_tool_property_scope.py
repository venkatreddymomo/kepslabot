"""Unit tests for the new sub-property scoping in image_tool_service.

The DB layer is monkeypatched so these run without a live database. Coverage:

- `_normalize_property_hint` and `_scope_cache_suffix` produce expected keys
- `invalidate_candidate_cache(hotel_code)` clears every scope variant for that hotel
  (regression: prior to the property-scope feature the cache was keyed by hotel
  alone, so a single pop sufficed; now it must walk the prefix)
- `resolve_active_property_id` matches a hint by slug, exact name, alias, and
  substring on the property name, falling back to None when nothing fits
- `list_candidate_assets(active_property_id=...)` constrains the SQL filter to
  `(property_id == active_property_id OR property_id IS NULL)` — verified by
  observing what the query's `.where(...)` chain ends up filtering against a
  hand-built in-memory result set
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.image_tool_service import ImageToolService, image_tool_service


class _DummyImageAsset:
    def __init__(
        self,
        *,
        asset_id: int,
        title: str,
        image_url: str,
        property_id: int | None,
        is_active: bool = True,
    ):
        self.id = asset_id
        self.title = title
        self.image_url = image_url
        self.description = ""
        self.category = ""
        self.tags = []
        self.priority = 0
        self.is_active = is_active
        self.property_id = property_id


class _DummyProperty:
    def __init__(
        self,
        *,
        prop_id: int,
        slug: str,
        name: str,
        aliases: list[str] | None = None,
        is_active: bool = True,
    ):
        self.id = prop_id
        self.property_slug = slug
        self.name = name
        self.aliases = aliases or []
        self.is_active = is_active


def test_scope_cache_suffix_handles_none_and_int():
    assert ImageToolService._scope_cache_suffix(None) == "all"
    assert ImageToolService._scope_cache_suffix(42) == "p:42"
    # Garbage in defaults to "all" so a buggy caller never poisons cache.
    assert ImageToolService._scope_cache_suffix("not_an_int") == "all"  # type: ignore[arg-type]


def test_normalize_property_hint_collapses_whitespace_and_lowercases():
    assert ImageToolService._normalize_property_hint("  Lotus  Eco  Beach  ") == "lotus eco beach"
    assert ImageToolService._normalize_property_hint(None) == ""
    assert ImageToolService._normalize_property_hint("") == ""


def test_invalidate_candidate_cache_drops_every_scope_for_hotel():
    # Seed three entries under one hotel and one under another. Invalidation
    # by hotel_code must pop the three and leave the unrelated one intact.
    ImageToolService._candidate_cache.clear()
    ImageToolService._candidate_cache["KHIL::all"] = (0.0, [])
    ImageToolService._candidate_cache["KHIL::p:1"] = (0.0, [])
    ImageToolService._candidate_cache["KHIL::p:7"] = (0.0, [])
    ImageToolService._candidate_cache["OTHER::all"] = (0.0, [{"keep": True}])

    ImageToolService.invalidate_candidate_cache("khil")

    remaining = set(ImageToolService._candidate_cache.keys())
    assert remaining == {"OTHER::all"}


@pytest.mark.asyncio
async def test_resolve_active_property_id_matches_slug_name_alias_and_substring(monkeypatch):
    # Drop the in-process resolver cache so this test isn't affected by
    # leftover state from other test runs.
    ImageToolService._property_id_resolver_cache.clear()

    rows = [
        _DummyProperty(
            prop_id=10,
            slug="lotus_eco_beach_resort",
            name="Lotus Eco Beach Resort Murud Dapoli",
            aliases=["Lotus Eco Murud"],
        ),
        _DummyProperty(
            prop_id=20,
            slug="ira_orchid_mumbai",
            name="Ira by Orchid Mumbai",
            aliases=[],
        ),
    ]

    # Stub `db_config_service.get_current_hotel_id` → fixed hotel id 99.
    from services.image_tool_service import db_config_service as ds_module

    class _ScopedHotelCtx:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(ds_module, "scoped_hotel", lambda _code: _ScopedHotelCtx())

    async def _fake_get_hotel_id():
        return 99

    monkeypatch.setattr(ds_module, "get_current_hotel_id", _fake_get_hotel_id)

    # Stub AsyncSessionLocal so that any SELECT against Property returns our
    # canned rows. We intentionally don't try to honor WHERE clauses here —
    # the resolver applies its match logic in Python after the fetch.
    class _FakeScalars:
        def __init__(self, items):
            self._items = items

        def all(self):
            return list(self._items)

    class _FakeResult:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return _FakeScalars(self._items)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, _stmt):
            return _FakeResult(rows)

    import services.image_tool_service as its_module

    monkeypatch.setattr(its_module, "AsyncSessionLocal", lambda: _FakeSession())

    # 1) slug exact
    assert await image_tool_service.resolve_active_property_id(
        hotel_code="khil",
        property_hint="lotus_eco_beach_resort",
    ) == 10

    # 2) name exact (case + whitespace normalized)
    ImageToolService._property_id_resolver_cache.clear()
    assert await image_tool_service.resolve_active_property_id(
        hotel_code="khil",
        property_hint="Ira by Orchid Mumbai",
    ) == 20

    # 3) alias exact
    ImageToolService._property_id_resolver_cache.clear()
    assert await image_tool_service.resolve_active_property_id(
        hotel_code="khil",
        property_hint="Lotus Eco Murud",
    ) == 10

    # 4) substring (KB scope shorter than the row's name)
    ImageToolService._property_id_resolver_cache.clear()
    assert await image_tool_service.resolve_active_property_id(
        hotel_code="khil",
        property_hint="Lotus Eco Beach",  # substring of the full name
    ) == 10

    # 5) no match → None (caller should treat as "no scope filter")
    ImageToolService._property_id_resolver_cache.clear()
    assert await image_tool_service.resolve_active_property_id(
        hotel_code="khil",
        property_hint="Some Other Hotel",
    ) is None

    # 6) blank hint short-circuits without a DB call
    ImageToolService._property_id_resolver_cache.clear()
    assert await image_tool_service.resolve_active_property_id(
        hotel_code="khil",
        property_hint="",
    ) is None


@pytest.mark.asyncio
async def test_list_candidate_assets_filters_to_property_or_shared(monkeypatch):
    """When active_property_id is given, results must include rows with that
    property_id AND rows where property_id IS NULL (shared). Rows scoped to
    other properties must be excluded.
    """
    ImageToolService._candidate_cache.clear()

    assets = [
        _DummyImageAsset(asset_id=1, title="Shared lobby", image_url="https://x/1.jpg", property_id=None),
        _DummyImageAsset(asset_id=2, title="Lotus poolside", image_url="https://x/2.jpg", property_id=10),
        _DummyImageAsset(asset_id=3, title="Mumbai suite", image_url="https://x/3.jpg", property_id=20),
    ]

    from services.image_tool_service import db_config_service as ds_module

    class _ScopedHotelCtx:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(ds_module, "scoped_hotel", lambda _code: _ScopedHotelCtx())

    async def _fake_get_hotel_id():
        return 99

    monkeypatch.setattr(ds_module, "get_current_hotel_id", _fake_get_hotel_id)

    # property_id -> name, matching what the real LEFT JOIN would yield. None
    # property_id stays None on the join (shared image, no property row).
    property_name_by_id = {10: "Lotus Eco Beach Resort", 20: "Mumbai Suite Hotel"}

    class _FakeSession:
        def __init__(self, rows):
            self._rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, stmt):
            # Inspect the compiled WHERE clause: when an active_property scope
            # is passed, the query carries an OR(property_id IN (...), property_id IS NULL)
            # criterion. We pre-filter our in-memory rows accordingly so the
            # test exercises the same logic the DB would. Accept both the
            # single-id `= 10` form and the multi-id `IN (10, ...)` form so
            # the test is robust to the implementation switching between the
            # two as the multi-property scope was added.
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            if (
                ("property_id IN (10" in compiled or "property_id = 10" in compiled)
                and "property_id IS NULL" in compiled
            ):
                allowed = [r for r in self._rows if r.property_id in (10, None)]
            elif (
                "property_id IS NULL" in compiled
                and "property_id = " not in compiled
                and "property_id IN" not in compiled
            ):
                allowed = [r for r in self._rows if r.property_id is None]
            else:
                allowed = list(self._rows)

            # The real query is `select(ImageAsset, Property.name)` after the
            # outerjoin, so callers get tuples back via `.all()` (not
            # `.scalars().all()`). Mirror that exact shape here.
            tupled = [(asset, property_name_by_id.get(asset.property_id)) for asset in allowed]

            class _R:
                def __init__(self, items):
                    self._items = items

                def all(self):
                    return list(self._items)

            return _R(tupled)

    import services.image_tool_service as its_module

    monkeypatch.setattr(its_module, "AsyncSessionLocal", lambda: _FakeSession(assets))

    # No scope → all three rows.
    all_rows = await image_tool_service.list_candidate_assets(hotel_code="khil")
    titles = {row["title"] for row in all_rows}
    assert titles == {"Shared lobby", "Lotus poolside", "Mumbai suite"}

    # Drop cache so the next call hits our fake session, not the prior result.
    ImageToolService._candidate_cache.clear()

    # Scoped to property 10 → only shared + property-10 rows.
    scoped = await image_tool_service.list_candidate_assets(
        hotel_code="khil",
        active_property_id=10,
    )
    titles = {row["title"] for row in scoped}
    assert titles == {"Shared lobby", "Lotus poolside"}
    # Each row carries its property_id and property_name back out so the chat
    # carousel can label cards by sub-property when comparing two properties.
    by_title = {row["title"]: row for row in scoped}
    assert by_title["Shared lobby"]["property_id"] is None
    assert by_title["Shared lobby"]["property_name"] is None
    assert by_title["Lotus poolside"]["property_id"] == 10
    assert by_title["Lotus poolside"]["property_name"] == "Lotus Eco Beach Resort"
