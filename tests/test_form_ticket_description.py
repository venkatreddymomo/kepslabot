import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.routes.chat import (
    _build_form_ticket_description_fallback,
    _extract_guest_request_text,
    _generate_confirmation_message,
    _generate_form_ticket_description,
    _sanitize_form_ticket_fields,
)
from config.settings import settings


class _DummyContext:
    def __init__(self) -> None:
        self.pending_data = {"_selected_property_scope_name": "Rohl Test Property"}
        self.booking_property_name = "Rohl Test Property"


def test_sanitize_form_ticket_fields_excludes_internal_and_empty() -> None:
    pairs = _sanitize_form_ticket_fields(
        {
            "flight_number": "aa178",
            "_internal": "x",
            "arrival_time": "",
            "number_of_passengers": 2,
        }
    )
    assert ("Flight Number", "aa178") in pairs
    assert ("Number Of Passengers", "2") in pairs
    assert all(label != "_internal" for label, _ in pairs)


def test_build_form_ticket_description_fallback_contains_context() -> None:
    issue, message = _build_form_ticket_description_fallback(
        service_name="airport transfer",
        service_purpose="Pickup from airport to hotel",
        property_name="Rohl Test Property",
        trigger_label="Terminal",
        trigger_value="Terminal 1",
        merged_form_data={
            "flight_number": "aa178",
            "arrival_time": "00:45",
            "number_of_passengers": "2",
            "_internal": "ignore",
        },
    )
    assert "airport transfer request" in issue.lower()
    assert "terminal 1" in issue.lower()
    assert "rohl test property" in message.lower()
    assert "selected terminal: terminal 1" in message.lower()
    assert "flight number: aa178" in message.lower()


def test_extract_guest_request_text_prefers_freeform_textarea() -> None:
    form_config = {
        "fields": [
            {"id": "room_number", "label": "Room Number", "type": "text"},
            {"id": "issue_details", "label": "Issue Details", "type": "textarea"},
        ]
    }
    text = _extract_guest_request_text(
        form_config=form_config,
        merged_form_data={
            "room_number": "204",
            "issue_details": "The AC in my room is not cooling and it's very hot.",
            "_internal": "ignore",
        },
    )
    assert text == "The AC in my room is not cooling and it's very hot."


def test_extract_guest_request_text_returns_empty_without_freeform() -> None:
    form_config = {
        "fields": [
            {"id": "flight_number", "label": "Flight Number", "type": "text"},
            {"id": "number_of_passengers", "label": "Passengers", "type": "number"},
        ]
    }
    assert (
        _extract_guest_request_text(
            form_config=form_config,
            merged_form_data={"flight_number": "AA178", "number_of_passengers": "2"},
        )
        == ""
    )


def test_build_form_ticket_description_fallback_uses_guest_words_as_issue() -> None:
    issue, message = _build_form_ticket_description_fallback(
        service_name="Support Request",
        service_purpose="",
        property_name="Zone Park Pathankot",
        trigger_label="",
        trigger_value="",
        merged_form_data={"issue_details": "AC not working in room 204"},
        guest_request_text="AC not working in room 204",
    )
    # The dashboard "Description" (issue) is the guest's real words, not
    # the generic "Support Request request" boilerplate.
    assert issue == "AC not working in room 204"
    assert "support request request" not in issue.lower()
    assert message


@pytest.mark.asyncio
async def test_generate_form_ticket_description_promotes_freeform_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)

    issue, message = await _generate_form_ticket_description(
        service={
            "id": "support_request",
            "name": "Support Request",
            "description": "General support",
            "form_config": {
                "fields": [
                    {"id": "guest_name", "label": "Name", "type": "text"},
                    {"id": "issue_details", "label": "Issue Details", "type": "textarea"},
                ]
            },
        },
        service_name="Support Request",
        service_id="support_request",
        merged_form_data={
            "guest_name": "Sana",
            "issue_details": "Hot water is not available in the bathroom since morning.",
        },
        req_meta={"booking_property_name": "Zone Park Pathankot"},
        context=_DummyContext(),
    )

    assert "hot water is not available" in issue.lower()
    assert "support request request" not in issue.lower()
    assert message


@pytest.mark.asyncio
async def test_generate_form_ticket_description_falls_back_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)

    issue, message = await _generate_form_ticket_description(
        service={
            "id": "airport_transfer",
            "name": "airport transfer",
            "description": "Arrange airport pickup",
            "form_config": {
                "trigger_field": {"id": "terminal", "label": "Terminal"},
            },
        },
        service_name="airport transfer",
        service_id="airport_transfer",
        merged_form_data={
            "terminal": "Terminal 1",
            "flight_number": "aa178",
            "arrival_time": "00:45",
            "number_of_passengers": "2",
        },
        req_meta={"booking_property_name": "Rohl Test Property"},
        context=_DummyContext(),
    )

    assert issue
    assert message
    assert "terminal 1" in message.lower()
    assert "flight number: aa178" in message.lower()


@pytest.mark.asyncio
async def test_generate_confirmation_message_fallback_hides_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(
        "api.routes.chat.llm_client.raw_chat_completion",
        _boom,
    )

    ack, closer = await _generate_confirmation_message(
        service_name="birthday planning",
        form_data={"occasion": "Birthday", "guest_name": "Sana"},
        property_name="Zone Park Pathankot",
    )

    assert ack
    assert "reference" not in ack.lower()
    assert "zone park pathankot" in closer.lower()
