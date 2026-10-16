import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from services.ticketing_service import TicketingService, TicketingResult


_ACCEPTED_CATEGORY_VALUES = set(
    TicketingService._ACCEPTED_TICKET_CATEGORY_BY_KEY.values()
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_category",
    [
        "Reception Front Office",   # the confirmed id:0 rejection
        "Banquets",
        "Sales",
        "Food & Beverage",
        "Concierge",
        "totally unknown value",
        "RESTAURANT",
    ],
)
async def test_create_ticket_never_posts_unaccepted_category(monkeypatch, bad_category):
    """End-to-end guard: whatever category a ticket carries, create_ticket must
    coerce it to an API-accepted value before POSTing. Department mapping is
    bypassed here so this proves the FINAL guard in create_ticket (not just the
    department-mapping remap) protects every ticket path."""
    service = TicketingService()

    monkeypatch.setattr(service, "_use_local_mode", lambda: False)
    monkeypatch.setattr(service, "_ticketing_base_url", lambda: "http://ticket.test")
    monkeypatch.setattr(service, "detect_service_ticketing_disabled", lambda p: None)

    async def _passthrough(p):
        return p

    # Bypass dept mapping + kepsla description so the final guard is the only
    # thing that can fix the category.
    monkeypatch.setattr(service, "_apply_department_mapping", _passthrough)
    monkeypatch.setattr(service, "_apply_kepsla_description", _passthrough)

    async def _noop_save(payload, result):
        return None

    monkeypatch.setattr(service, "_save_ticket_to_db", _noop_save)

    captured: dict = {}

    async def _fake_request_json(method, url, payload, **kwargs):
        captured["payload"] = payload
        return TicketingResult(success=True, ticket_id="9999", status_code=200, response={"id": 9999})

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    await service.create_ticket(
        {
            "issue": "Guest needs help",
            "categorization": bad_category,
            "category": bad_category,
            "session_id": "sess-cat-test",
            "organisation_id": "6290",
        }
    )

    sent = captured["payload"]["categorization"]
    assert sent in _ACCEPTED_CATEGORY_VALUES, (
        f"category {bad_category!r} was sent to the API as {sent!r}, "
        f"which is NOT in the accepted set {_ACCEPTED_CATEGORY_VALUES}"
    )


_DEPARTMENTS = [
    {"department_id": "1", "department_name": "HOUSEKEEPING"},
    {"department_id": "2", "department_name": "IN-ROOM DINING"},
    {"department_id": "3", "department_name": "RESTAURANT"},
    {"department_id": "4", "department_name": "SPA"},
]


class _DummyAsyncSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_department_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    ltr_module = importlib.import_module("integrations.lumira_ticketing_repository")

    async def _fake_fetch_departments_of_entity(_db_session, entity_id=None):
        assert str(entity_id) == "5703"
        return list(_DEPARTMENTS)

    monkeypatch.setattr(
        ltr_module.lumira_ticketing_repository,
        "fetch_departments_of_entity",
        _fake_fetch_departments_of_entity,
    )
    monkeypatch.setattr(
        "models.database.AsyncSessionLocal",
        lambda: _DummyAsyncSessionContext(),
    )


@pytest.mark.asyncio
async def test_apply_department_mapping_falls_back_to_existing_department_when_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ticketing_debug_log_enabled", False, raising=False)
    _patch_department_lookup(monkeypatch)

    service = TicketingService()

    async def _llm_none(*, payload, departments):
        return None

    monkeypatch.setattr(service, "_llm_resolve_department_for_ticket_payload", _llm_none)

    payload = {
        "issue": "Need replacement towels",
        "message": "Guest requested extra towels.",
        "organisation_id": "5703",
        "department_id": "2",
        "department_allocated": "2",
        "categorization": "custom service request",
    }
    resolved = await service._apply_department_mapping(payload)

    assert resolved["department_id"] == "2"
    assert resolved["department_allocated"] == "2"
    assert resolved["department_manager"] == "IN-ROOM DINING"
    # Categorization is coerced to the API-accepted canonical value (the raw
    # department label "IN-ROOM DINING" -> accepted "In-Room Dining").
    assert resolved["categorization"] == "In-Room Dining"


@pytest.mark.asyncio
async def test_apply_department_mapping_ignores_invalid_existing_department_and_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ticketing_debug_log_enabled", False, raising=False)
    _patch_department_lookup(monkeypatch)

    service = TicketingService()

    async def _llm_none(*, payload, departments):
        return None

    monkeypatch.setattr(service, "_llm_resolve_department_for_ticket_payload", _llm_none)

    payload = {
        "issue": "Need a spa booking",
        "message": "Please schedule massage at 6 PM.",
        "organisation_id": "5703",
        "department_id": "999",
        "department_allocated": "999",
        "categorization": "spa booking",
    }
    resolved = await service._apply_department_mapping(payload)

    assert resolved["department_id"] == "4"
    assert resolved["department_allocated"] == "4"
    assert resolved["department_manager"] == "SPA"


@pytest.mark.asyncio
async def test_apply_department_mapping_prefers_llm_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ticketing_debug_log_enabled", False, raising=False)
    _patch_department_lookup(monkeypatch)

    service = TicketingService()

    async def _llm_pick_restaurant(*, payload, departments):
        for item in departments:
            if str(item.get("department_id")) == "3":
                return item
        return None

    monkeypatch.setattr(service, "_llm_resolve_department_for_ticket_payload", _llm_pick_restaurant)

    payload = {
        "issue": "Please send food menu",
        "message": "Guest asked to order dinner.",
        "organisation_id": "5703",
        "department_id": "1",
        "department_allocated": "1",
        "categorization": "food service",
    }
    resolved = await service._apply_department_mapping(payload)

    assert resolved["department_id"] == "3"
    assert resolved["department_allocated"] == "3"
    assert resolved["department_manager"] == "RESTAURANT"
    # Categorization is coerced to an API-accepted value: the "RESTAURANT"
    # department maps to the accepted food category "In-Room Dining" (the raw
    # department name is not a valid category — that is the id:0 bug class).
    assert resolved["categorization"] == "In-Room Dining"
