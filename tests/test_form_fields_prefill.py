"""Tests for form-field prefill in services.form_fields_service.

These tests pin two behaviors:

1. Deterministic prefill from request metadata + conversation context never
   touches an LLM, and fills well-known fields (name, phone, email, room,
   property) when matching aliases are present.
2. Freeform textarea prefill only fires when allow_llm=True AND a textarea
   field whose id/label hints at "issue/description/notes" is present. When
   allow_llm=False, no LLM call is made.

Both paths must be additive: when no source matches, the field dict is
returned unchanged so the frontend renders the form blank (today's behavior).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import form_fields_service
from services.form_fields_service import apply_prefill, _extract_freeform_with_llm


def _mock_completion(value_json: str):
    """Build a minimal object shaped like an OpenAI chat completion."""
    msg = SimpleNamespace(content=value_json)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


@pytest.mark.asyncio
async def test_prefill_guard_drops_offtopic_value_anchored_on_current_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bot's current reply names "Dighashree Auditorium". A hallucinated
    # "conference room" value shares no distinctive token with it, so the
    # guard must drop it (field stays blank rather than showing a wrong value).
    async def _fake_completion(**_kwargs):
        return _mock_completion('{"value": "I would like assistance with booking a conference room."}')

    monkeypatch.setattr(
        form_fields_service.llm_client, "raw_chat_completion", _fake_completion
    )

    value = await _extract_freeform_with_llm(
        field={"id": "issue_details", "label": "Issue Details", "type": "textarea"},
        triggering_user_message="yes",
        service={"id": "support_request", "name": "Support Request"},
        bot_response_text=(
            "Thank you for confirming your interest in booking the Dighashree "
            "Auditorium. Please fill in the details below."
        ),
    )
    assert value == ""


@pytest.mark.asyncio
async def test_prefill_guard_keeps_ontopic_value_from_current_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_completion(**_kwargs):
        return _mock_completion('{"value": "I would like help booking the Dighashree Auditorium."}')

    monkeypatch.setattr(
        form_fields_service.llm_client, "raw_chat_completion", _fake_completion
    )

    value = await _extract_freeform_with_llm(
        field={"id": "issue_details", "label": "Issue Details", "type": "textarea"},
        triggering_user_message="yes",
        service={"id": "support_request", "name": "Support Request"},
        bot_response_text=(
            "Thank you for confirming your interest in booking the Dighashree "
            "Auditorium. Please fill in the details below."
        ),
    )
    assert "Dighashree Auditorium" in value


@pytest.mark.asyncio
async def test_prefill_guard_stemming_matches_plurals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "event" (value) vs "events" (reply) must count as overlap thanks to
    # light stemming, so an on-topic value is not dropped on a plural mismatch.
    async def _fake_completion(**_kwargs):
        return _mock_completion('{"value": "Booking a venue for an event."}')

    monkeypatch.setattr(
        form_fields_service.llm_client, "raw_chat_completion", _fake_completion
    )

    value = await _extract_freeform_with_llm(
        field={"id": "issue_details", "label": "Issue Details", "type": "textarea"},
        triggering_user_message="yes",
        service={"id": "support_request", "name": "Support Request"},
        bot_response_text=(
            "The Dighashree Auditorium is ideal for large-scale events such as weddings."
        ),
    )
    assert value == "Booking a venue for an event."


@pytest.mark.asyncio
async def test_apply_prefill_deterministic_fills_name_phone_room_from_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even if the LLM is allowed, deterministic fields must not need it.
    async def _fail_llm(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("LLM must not be called for deterministic prefill")

    monkeypatch.setattr(form_fields_service, "_extract_freeform_with_llm", _fail_llm)

    fields = [
        {"id": "full_name", "label": "Full Name", "type": "text", "required": True},
        {"id": "phone", "label": "Phone Number", "type": "tel", "required": True},
        {"id": "room_number", "label": "Room Number", "type": "text", "required": True},
        {"id": "email", "label": "Email", "type": "email", "required": False},
    ]

    out = await apply_prefill(
        fields,
        req_meta={
            "guest_name": "Asha Verma",
            "guest_phone": "+91 9876543210",
            "room_number": "412",
            "guest_email": "asha@example.com",
        },
        context=None,
        triggering_user_message="",
        service={"id": "ac_complaint"},
        allow_llm=True,
    )

    by_id = {f["id"]: f for f in out}
    assert by_id["full_name"]["default_value"] == "Asha Verma"
    assert by_id["full_name"]["prefill_source"] == "session_metadata"
    assert by_id["phone"]["default_value"] == "+91 9876543210"
    assert by_id["room_number"]["default_value"] == "412"
    assert by_id["email"]["default_value"] == "asha@example.com"


@pytest.mark.asyncio
async def test_apply_prefill_falls_back_to_context_when_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_llm(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("LLM must not be called for deterministic prefill")

    monkeypatch.setattr(form_fields_service, "_extract_freeform_with_llm", _fail_llm)

    fields = [
        {"id": "full_name", "label": "Full Name", "type": "text", "required": True},
        {"id": "room_number", "label": "Room Number", "type": "text", "required": True},
    ]
    context = SimpleNamespace(
        guest_name="Rahul",
        guest_phone="",
        guest_email="",
        room_number="207",
        booking_property_name="Ira Orchid Porvorim",
        pending_data={"_selected_property_scope_name": "Ira Orchid Porvorim"},
    )

    out = await apply_prefill(
        fields,
        req_meta={},
        context=context,
        triggering_user_message="",
        service={"id": "ac_complaint"},
        allow_llm=False,
    )

    by_id = {f["id"]: f for f in out}
    assert by_id["full_name"]["default_value"] == "Rahul"
    assert by_id["room_number"]["default_value"] == "207"


@pytest.mark.asyncio
async def test_apply_prefill_textarea_uses_llm_when_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def _fake_llm(*, field, triggering_user_message, service, **_kwargs):
        calls.append({
            "field_id": field.get("id"),
            "msg": triggering_user_message,
            "svc": (service or {}).get("id"),
        })
        return "AC stopped cooling around midnight."

    monkeypatch.setattr(form_fields_service, "_extract_freeform_with_llm", _fake_llm)

    fields = [
        {"id": "issue_details", "label": "Issue Details", "type": "textarea", "required": True},
    ]
    out = await apply_prefill(
        fields,
        req_meta={},
        context=None,
        triggering_user_message="The AC isn't cooling since midnight, please help.",
        service={"id": "ac_complaint", "name": "AC Complaint"},
        allow_llm=True,
    )

    assert calls and calls[0]["field_id"] == "issue_details"
    assert out[0]["default_value"] == "AC stopped cooling around midnight."
    assert out[0]["prefill_source"] == "llm_extracted"


@pytest.mark.asyncio
async def test_apply_prefill_textarea_skipped_when_allow_llm_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_llm(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("LLM must not be called when allow_llm=False")

    monkeypatch.setattr(form_fields_service, "_extract_freeform_with_llm", _fail_llm)

    fields = [
        {"id": "issue_details", "label": "Issue Details", "type": "textarea", "required": True},
    ]
    out = await apply_prefill(
        fields,
        req_meta={},
        context=None,
        triggering_user_message="AC issue",
        service={"id": "ac_complaint"},
        allow_llm=False,
    )

    assert "default_value" not in out[0]
    assert "prefill_source" not in out[0]


@pytest.mark.asyncio
async def test_apply_prefill_textarea_skipped_when_field_is_not_freeform_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A textarea that's clearly not the 'issue/notes' kind must not be guessed at."""
    async def _fail_llm(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("LLM must not be called for non-freeform textarea")

    monkeypatch.setattr(form_fields_service, "_extract_freeform_with_llm", _fail_llm)

    fields = [
        # textarea but the id/label give no "issue/description/notes" hint
        {"id": "delivery_address", "label": "Delivery Address", "type": "textarea", "required": True},
    ]
    out = await apply_prefill(
        fields,
        req_meta={},
        context=None,
        triggering_user_message="Need someone to come fix the lock.",
        service={"id": "maintenance"},
        allow_llm=True,
    )

    assert "default_value" not in out[0]


@pytest.mark.asyncio
async def test_apply_prefill_is_noop_when_nothing_matches() -> None:
    """When no source provides a value, fields must come back byte-equivalent."""
    fields = [
        {"id": "some_obscure_field", "label": "Obscure", "type": "text", "required": False},
    ]
    original = [dict(f) for f in fields]

    out = await apply_prefill(
        fields,
        req_meta={"guest_name": "Asha"},  # no field matches "name" aliases here
        context=None,
        triggering_user_message="",
        service={"id": "x"},
        allow_llm=False,
    )

    assert out == original


@pytest.mark.asyncio
async def test_apply_prefill_clears_stale_default_value_and_recomputes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `fields` may be the cached schema list returned by extract_form_fields,
    # so a default_value left on it from a PRIOR turn / different session must
    # not bleed through. apply_prefill clears prefill keys on entry and
    # recomputes this turn's value from the current request metadata/context.
    async def _fail_llm(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("LLM must not be called for deterministic prefill")

    monkeypatch.setattr(form_fields_service, "_extract_freeform_with_llm", _fail_llm)

    fields = [
        {
            "id": "full_name",
            "label": "Full Name",
            "type": "text",
            "required": True,
            "default_value": "Stale From Other Session",
        },
    ]

    out = await apply_prefill(
        fields,
        req_meta={"guest_name": "Asha Verma"},
        context=None,
        triggering_user_message="",
        service={"id": "x"},
        allow_llm=False,
    )

    # Stale value cleared; recomputed from this turn's metadata.
    assert out[0]["default_value"] == "Asha Verma"
    assert out[0]["prefill_source"] == "session_metadata"
