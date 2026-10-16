"""
Fix #2 regression: the opening info-collection form (name + location) must
capture the guest's lead profile WITHOUT creating a support ticket.

A service whose form_config carries `lead_capture_only: true` is the onboarding
form — submitting it should:
  - NOT call ticketing_service.create_ticket (no ticket, no external API push),
  - still return success with an empty ticket_id,
  - still capture the session lead profile (upsert_session_profile is called),
  - be allowed even when ticketing is disabled for the service.

A normal ticketing service (no flag) must still create a ticket exactly as before.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.routes.chat as chat_mod
from api.routes.chat import FormSubmitRequest, submit_form
from schemas.chat import ConversationContext
from services.config_service import config_service
from services.ticketing_service import ticketing_service
from services.db_config_service import db_config_service


class _FakeURL:
    path = "/api/chat/form-submit"


class _FakeState:
    trace_id = "trace-test"


class _FakeRequest:
    state = _FakeState()
    url = _FakeURL()
    method = "POST"


def _install_common_patches(monkeypatch, *, service: dict, create_calls: dict):
    """Patch the heavy singletons/helpers submit_form pulls in so the test
    exercises only the lead-capture branch decision."""

    # config_service (re-imported inside the function from services.config_service)
    monkeypatch.setattr(config_service, "resolve_hotel_code", lambda code: "TESTHOTEL", raising=False)
    monkeypatch.setattr(config_service, "get_service", lambda sid: dict(service), raising=False)

    # db_config_service hotel-context + lookups
    monkeypatch.setattr(db_config_service, "set_hotel_context", lambda code: object(), raising=False)
    monkeypatch.setattr(db_config_service, "reset_hotel_context", lambda tok: None, raising=False)

    async def _fake_get_services():
        return [dict(service)]

    async def _fake_get_business_info():
        return {}

    monkeypatch.setattr(db_config_service, "get_services", _fake_get_services, raising=False)
    monkeypatch.setattr(db_config_service, "get_business_info", _fake_get_business_info, raising=False)

    # context_manager
    ctx = ConversationContext(session_id="sess-lead", hotel_code="TESTHOTEL")

    async def _fake_get_or_create_context(*, session_id, hotel_code):
        return ctx

    async def _fake_save_context(context, db_session=None):
        return None

    monkeypatch.setattr(chat_mod.context_manager, "get_or_create_context", _fake_get_or_create_context, raising=False)
    monkeypatch.setattr(chat_mod.context_manager, "save_context", _fake_save_context, raising=False)

    # ticket description + payload + booking persistence (avoid LLM / DB)
    async def _fake_desc(**kwargs):
        return ("Lead summary", "Lead detail")

    monkeypatch.setattr(chat_mod, "_generate_form_ticket_description", _fake_desc, raising=False)
    monkeypatch.setattr(
        ticketing_service, "build_lumira_ticket_payload",
        lambda **kwargs: {"issue": "x", "property_name": "Test Property", "organisation_id": "1"},
        raising=False,
    )

    async def _fake_persist(**kwargs):
        return {}

    monkeypatch.setattr(chat_mod, "_persist_prebooking_booking_if_possible", _fake_persist, raising=False)

    # create_ticket spy
    async def _spy_create_ticket(payload):
        create_calls["n"] += 1
        return SimpleNamespace(success=True, ticket_id="TKT-NORMAL", error="")

    monkeypatch.setattr(ticketing_service, "create_ticket", _spy_create_ticket, raising=False)

    # confirmation generator (only used on the non-lead path)
    async def _fake_confirm(**kwargs):
        return ("Your request has been submitted.", "")

    monkeypatch.setattr(chat_mod, "_generate_confirmation_message", _fake_confirm, raising=False)

    # session profile capture spy
    profile_calls = {"n": 0}

    async def _spy_upsert(**kwargs):
        profile_calls["n"] += 1
        return None

    import services.session_profile_service as sps
    monkeypatch.setattr(sps, "upsert_session_profile", _spy_upsert, raising=False)

    # dashboard writes off (force _is_kebo=False so kebo-only writes are skipped)
    import services.customer_details_service as cds
    monkeypatch.setattr(cds.customer_details_service, "resolve_widget_group", lambda code: ("", ""), raising=False)

    # step trace = no-op (best-effort logger that may touch DB)
    monkeypatch.setattr(chat_mod.step_trace_service, "log_event", lambda *a, **k: None, raising=False)

    return ctx, profile_calls


@pytest.mark.asyncio
async def test_lead_capture_form_skips_ticket(monkeypatch):
    create_calls = {"n": 0}
    service = {
        "id": "guest_details",
        "name": "Guest Details",
        # ticketing DISABLED — the lead form must still be allowed through.
        "ticketing_enabled": False,
        "form_config": {"fields": [], "lead_capture_only": True},
    }
    _ctx, profile_calls = _install_common_patches(monkeypatch, service=service, create_calls=create_calls)

    req = FormSubmitRequest(
        session_id="sess-lead",
        hotel_code="TESTHOTEL",
        service_id="guest_details",
        form_data={"guest_name": "Sana", "location": "Coimbatore"},
        metadata={"channel": "web_widget"},
    )
    resp = await submit_form(req, _FakeRequest(), db=object())

    assert resp["success"] is True
    assert resp["ticket_id"] == "", "lead-capture form must not produce a ticket id"
    assert create_calls["n"] == 0, "create_ticket must NOT be called for a lead-capture form"
    assert profile_calls["n"] == 1, "the session lead profile must still be captured"


@pytest.mark.asyncio
async def test_opening_lead_form_skips_ticket_even_when_service_is_ticketing_enabled(monkeypatch):
    """Real-world bug: the session-start lead form is submitted under a
    TICKETING-ENABLED service (room_booking_request) but the widget tags it with
    `lead_source` in metadata. It must be treated as lead-capture and create NO
    ticket — previously it produced a spurious 'room booking request' ticket
    (#7735) at the very start of every new session."""
    create_calls = {"n": 0}
    service = {
        "id": "room_booking_request",
        "name": "room booking request",
        # Ticketing ENABLED — the opening lead form must STILL be suppressed,
        # detected purely from the `lead_source` metadata marker.
        "ticketing_enabled": True,
        "form_config": {"fields": []},  # no lead_capture_only flag on the service
    }
    _ctx, profile_calls = _install_common_patches(monkeypatch, service=service, create_calls=create_calls)

    req = FormSubmitRequest(
        session_id="sess-lead",
        hotel_code="TESTHOTEL",
        service_id="room_booking_request",
        form_data={"guest_name": "venkat", "location": "Jammu"},
        metadata={
            "channel": "web_widget",
            "lead_source": "widget_opening_form",
            "booking_location": "Jammu",
        },
    )
    resp = await submit_form(req, _FakeRequest(), db=object())

    assert resp["success"] is True
    assert resp["ticket_id"] == "", "opening lead form must not produce a ticket id"
    assert create_calls["n"] == 0, "create_ticket must NOT be called for the opening lead form"
    assert profile_calls["n"] == 1, "the session lead profile must still be captured"


@pytest.mark.asyncio
async def test_normal_service_still_creates_ticket(monkeypatch):
    create_calls = {"n": 0}
    service = {
        "id": "support_request",
        "name": "Support Request",
        "ticketing_enabled": True,
        "form_config": {"fields": []},  # no lead_capture_only flag
    }
    _ctx, _profile_calls = _install_common_patches(monkeypatch, service=service, create_calls=create_calls)

    req = FormSubmitRequest(
        session_id="sess-lead",
        hotel_code="TESTHOTEL",
        service_id="support_request",
        form_data={"email": "san@gmail.com", "issue_details": "wedding info"},
        metadata={"channel": "web_widget"},
    )
    resp = await submit_form(req, _FakeRequest(), db=object())

    assert resp["success"] is True
    assert create_calls["n"] == 1, "a normal service must still create a ticket"
    assert resp["ticket_id"] == "TKT-NORMAL"
