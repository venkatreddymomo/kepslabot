import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.chat_service as chat_service_module
from schemas.chat import ConversationContext
from schemas.orchestration import OrchestrationDecision
from services.chat_service import ChatService
from services.ticketing_service import ticketing_service


@pytest.mark.asyncio
async def test_multi_property_book_intent_requires_property_choice_before_ticket_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatService()
    context = ConversationContext(
        session_id="sess-goa-clarify",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": [
                "zone_connect_park_calangute_goa",
                "zone_connect_park_parra_goa",
            ],
            "_active_property_names": [
                "Zone Connect Park Calangute Goa",
                "Zone Connect Park Parra Goa",
            ],
            "_property_name_map": {
                "zone_connect_park_calangute_goa": "Zone Connect Park Calangute Goa",
                "zone_connect_park_parra_goa": "Zone Connect Park Parra Goa",
            },
            "_rb_bookings": [
                {
                    "property": "Zone Park Chennai Orr",
                    "entity": "zone_park_chennai_orr",
                    "scope_key": "ids:zone_park_chennai_orr",
                    "check_in": "",
                    "check_out": "",
                }
            ],
        },
    )
    decision = OrchestrationDecision(
        action="dispatch_handler",
        target_service_id="room_booking",
    )

    async def fake_classify_intent(_msg: str, _reply: str) -> str:
        return "book"

    async def fake_extract_dates(_msg: str) -> tuple[str, str]:
        return "", ""

    async def boom_create_booking(**_kwargs):
        raise AssertionError("booking should not be created before property choice")

    def boom_build_payload(**_kwargs):
        raise AssertionError("ticket payload should not be probed before property choice")

    monkeypatch.setattr(chat_service_module.settings, "ticketing_room_booking_silent_ticket_enabled", True, raising=False)
    monkeypatch.setattr(service, "_rb_classify_intent", fake_classify_intent)
    monkeypatch.setattr(service, "_rb_extract_dates", fake_extract_dates)
    monkeypatch.setattr(service, "_rb_create_booking", boom_create_booking)
    monkeypatch.setattr(ticketing_service, "build_lumira_ticket_payload", boom_build_payload)

    response_text, is_control = await service._maybe_manage_room_booking_ticket(
        context=context,
        response_text="Great choice!",
        decision=decision,
        user_message="book a room",
        selected_phase_context={"selected_phase_name": "Pre Booking"},
    )

    assert is_control is True
    assert "Which property would you like to book" in response_text
    assert "Zone Connect Park Calangute Goa" in response_text
    assert "Zone Connect Park Parra Goa" in response_text
    assert "_rb_pending_switch" not in context.pending_data


@pytest.mark.asyncio
async def test_multi_property_book_intent_ignores_legacy_selected_scope_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatService()
    context = ConversationContext(
        session_id="sess-goa-clarify-legacy-selected",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": [
                "zone_connect_park_calangute_goa",
                "zone_connect_park_parra_goa",
            ],
            "_active_property_names": [
                "Zone Connect Park Calangute Goa",
                "Zone Connect Park Parra Goa",
            ],
            "_selected_property_scope_id": "zone_connect_park_calangute_goa",
            "_selected_property_scope_ids": [
                "zone_connect_park_calangute_goa",
                "zone_connect_park_parra_goa",
            ],
            "_selected_property_scope_name": "Zone Connect Park Calangute Goa",
            "_property_name_map": {
                "zone_connect_park_calangute_goa": "Zone Connect Park Calangute Goa",
                "zone_connect_park_parra_goa": "Zone Connect Park Parra Goa",
            },
        },
    )
    decision = OrchestrationDecision(
        action="dispatch_handler",
        target_service_id="room_booking",
    )

    async def fake_classify_intent(_msg: str, _reply: str) -> str:
        return "book"

    async def fake_extract_dates(_msg: str) -> tuple[str, str]:
        return "", ""

    async def boom_create_booking(**_kwargs):
        raise AssertionError("legacy selected scope must not bypass property choice")

    def boom_build_payload(**_kwargs):
        raise AssertionError("ticket payload should not be probed before property choice")

    monkeypatch.setattr(chat_service_module.settings, "ticketing_room_booking_silent_ticket_enabled", True, raising=False)
    monkeypatch.setattr(service, "_rb_classify_intent", fake_classify_intent)
    monkeypatch.setattr(service, "_rb_extract_dates", fake_extract_dates)
    monkeypatch.setattr(service, "_rb_create_booking", boom_create_booking)
    monkeypatch.setattr(ticketing_service, "build_lumira_ticket_payload", boom_build_payload)

    response_text, is_control = await service._maybe_manage_room_booking_ticket(
        context=context,
        response_text="Great choice!",
        decision=decision,
        user_message="book a room",
        selected_phase_context={"selected_phase_name": "Pre Booking"},
    )

    assert is_control is True
    assert "Which property would you like to book" in response_text
    assert "Zone Connect Park Calangute Goa" in response_text
    assert "Zone Connect Park Parra Goa" in response_text


@pytest.mark.asyncio
async def test_room_booking_proceeds_once_active_scope_is_one_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatService()
    context = ConversationContext(
        session_id="sess-goa-selected",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": [
                "zone_connect_park_calangute_goa",
            ],
            "_active_property_names": [
                "Zone Connect Park Calangute Goa",
            ],
            "_property_name_map": {
                "zone_connect_park_calangute_goa": "Zone Connect Park Calangute Goa",
                "zone_connect_park_parra_goa": "Zone Connect Park Parra Goa",
            },
        },
    )
    decision = OrchestrationDecision(
        action="dispatch_handler",
        target_service_id="room_booking",
    )
    captured: dict[str, object] = {}

    async def fake_classify_intent(_msg: str, _reply: str) -> str:
        return "book"

    async def fake_extract_dates(_msg: str) -> tuple[str, str]:
        return "", ""

    def fake_build_payload(**_kwargs):
        return {"organisation_id": "goa_calangute_org"}

    async def fake_create_booking(**kwargs):
        captured.update(kwargs)
        return "goa_calangute_org"

    monkeypatch.setattr(chat_service_module.settings, "ticketing_room_booking_silent_ticket_enabled", True, raising=False)
    monkeypatch.setattr(service, "_rb_classify_intent", fake_classify_intent)
    monkeypatch.setattr(service, "_rb_extract_dates", fake_extract_dates)
    monkeypatch.setattr(service, "_rb_create_booking", fake_create_booking)
    monkeypatch.setattr(ticketing_service, "build_lumira_ticket_payload", fake_build_payload)

    response_text, is_control = await service._maybe_manage_room_booking_ticket(
        context=context,
        response_text="Great choice!",
        decision=decision,
        user_message="book a room",
        selected_phase_context={"selected_phase_name": "Pre Booking"},
    )

    assert is_control is True
    assert captured["prop"] == "Zone Connect Park Calangute Goa"
    assert captured["scope_ids"] == ["zone_connect_park_calangute_goa"]
    assert captured["scope_key"] == "ids:zone_connect_park_calangute_goa"
    assert "<<SHOW_BOOKING_BUTTON: Zone Connect Park Calangute Goa>>" in response_text
    assert "_rb_pending_switch" not in context.pending_data
