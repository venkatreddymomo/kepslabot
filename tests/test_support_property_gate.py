import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas.chat import ConversationContext
from schemas.orchestration import OrchestrationDecision, TicketDecision
from services import prebooking_settings_service
from services.llm_orchestration_service import llm_orchestration_service

RESIDENCY = "T24 Residency, Andheri East (Terminal 2)"
RETRO = "T24 Retro, Vile Parle East (Terminal 1)"


def _support_turn(hotel_code: str) -> tuple[OrchestrationDecision, ConversationContext]:
    # Both hotels in focus, none chosen; the scope name is filled with the first.
    decision = OrchestrationDecision(
        action="collect_info",
        target_service_id="support_request",
        response_text="Please provide the details of your request in the form below.",
        pending_action="collect_form_details",
        ticket=TicketDecision(required=True),
    )
    context = ConversationContext(
        session_id="sess-gate",
        hotel_code=hotel_code,
        pending_data={
            "_active_property_ids": ["t24_residency", "t24_retro"],
            "_selected_property_scope_name": RESIDENCY,
            "_property_name_map": {"t24_residency": RESIDENCY, "t24_retro": RETRO},
        },
    )
    return decision, context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row, expected",
    [
        ("Which hotel is this about — {hotels}?", f"Which hotel is this about — **{RESIDENCY}**, or **{RETRO}**?"),
        ("", f"Sorry to hear that. Which hotel is this about — **{RESIDENCY}**, or **{RETRO}**?"),
    ],
)
async def test_admin_hotel_asks_which_hotel_in_its_db_wording(
    monkeypatch: pytest.MonkeyPatch, row: str, expected: str
) -> None:
    monkeypatch.setattr(prebooking_settings_service, "is_admin_managed", lambda code: code == "t24")
    asked: list[str] = []

    async def fake_db_prompt_text(key: str) -> str:
        asked.append(key)
        return row

    monkeypatch.setattr(llm_orchestration_service, "_db_prompt_text", fake_db_prompt_text)
    decision, context = _support_turn("t24")

    out = await llm_orchestration_service._enforce_support_property_choice(
        decision=decision, target_service={"id": "support_request", "name": "Support Request"}, context=context,
    )

    assert asked == ["ticketing.support_property_ask"]
    assert out.response_text == expected
    assert out.action == "respond_only"
    assert out.pending_action is None
    assert out.metadata["support_property_required"] is True
    assert context.pending_data["_awaiting_hotel_for"] == "support_request"


@pytest.mark.asyncio
async def test_other_hotels_keep_the_first_scope_hotel_and_read_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prebooking_settings_service, "is_admin_managed", lambda code: code == "t24")

    async def fail_db_prompt_text(key: str) -> str:
        raise AssertionError("only Pre-booking tab hotels read the wording row")

    monkeypatch.setattr(llm_orchestration_service, "_db_prompt_text", fail_db_prompt_text)
    decision, context = _support_turn("zone")

    out = await llm_orchestration_service._enforce_support_property_choice(
        decision=decision, target_service={"id": "support_request", "name": "Support Request"}, context=context,
    )

    assert out.action == "collect_info"
    assert out.pending_action == "collect_form_details"
    assert "support_property_required" not in out.metadata
    assert "_awaiting_hotel_for" not in context.pending_data


@pytest.mark.asyncio
@pytest.mark.parametrize("hotel_code, admin_managed", [("t24", True), ("zone", False)])
async def test_admin_hotel_question_without_form_step_opens_no_form(
    monkeypatch: pytest.MonkeyPatch, hotel_code: str, admin_managed: bool
) -> None:
    # The complaint agent asks which T24 hotel ("rise complaint on bangalore hotel")
    # with collect_info but no form step; the widget must not open the form under it.
    monkeypatch.setattr(prebooking_settings_service, "is_admin_managed", lambda code: code == "t24")

    async def no_row(key: str) -> str:
        return ""

    monkeypatch.setattr(llm_orchestration_service, "_db_prompt_text", no_row)
    question = (
        "I understand you want to raise a complaint, but T24 has only two hotels in Mumbai. "
        "Could you please confirm which one your complaint is about?"
    )
    decision = OrchestrationDecision(
        action="collect_info",
        target_service_id="guest_complaint",
        response_text=question,
        pending_action=None,
        ticket=TicketDecision(required=False),
    )
    context = ConversationContext(
        session_id="sess-gate",
        hotel_code=hotel_code,
        pending_data={
            "_active_property_ids": ["t24_retro"],
            "_selected_property_scope_name": RETRO,
            "_property_name_map": {"t24_residency": RESIDENCY, "t24_retro": RETRO},
        },
    )

    out = await llm_orchestration_service._enforce_support_property_choice(
        decision=decision, target_service={"id": "guest_complaint", "name": "Complaint"}, context=context,
    )

    assert out.response_text == question
    if admin_managed:
        assert out.action == "respond_only"
        assert context.pending_data["_awaiting_hotel_for"] == "guest_complaint"
    else:
        assert out.action == "collect_info"
        assert "_awaiting_hotel_for" not in context.pending_data


@pytest.mark.asyncio
@pytest.mark.parametrize("hotel_code, admin_managed", [("t24", True), ("zone", False)])
async def test_admin_hotel_form_with_no_hotel_named_asks_even_with_one_in_focus(
    monkeypatch: pytest.MonkeyPatch, hotel_code: str, admin_managed: bool
) -> None:
    # "mysore hotel staff is too rude" while T24 Retro is in focus: the agent opens the
    # complaint form but names no hotel; filing it to Retro would be wrong.
    monkeypatch.setattr(prebooking_settings_service, "is_admin_managed", lambda code: code == "t24")

    async def ask_row(key: str) -> str:
        return "Which hotel is this about — {hotels}?"

    monkeypatch.setattr(llm_orchestration_service, "_db_prompt_text", ask_row)
    decision = OrchestrationDecision(
        action="collect_info",
        target_service_id="guest_complaint",
        response_text="I'm sorry to hear about your experience with the Mysore hotel staff. Please fill in the form below.",
        pending_action="collect_form_details",
        selected_property_ids=[],
        ticket=TicketDecision(required=False),
    )
    context = ConversationContext(
        session_id="sess-gate",
        hotel_code=hotel_code,
        pending_data={
            "_active_property_ids": ["t24_retro"],
            "_selected_property_scope_name": RETRO,
            "_property_name_map": {"t24_residency": RESIDENCY, "t24_retro": RETRO},
        },
    )

    out = await llm_orchestration_service._enforce_support_property_choice(
        decision=decision, target_service={"id": "guest_complaint", "name": "Complaint"}, context=context,
    )

    if admin_managed:
        assert out.action == "respond_only"
        assert out.pending_action is None
        assert out.response_text == f"Which hotel is this about — **{RESIDENCY}**, or **{RETRO}**?"
        assert context.pending_data["_awaiting_hotel_for"] == "guest_complaint"
    else:
        assert out.action == "collect_info"
        assert out.pending_action == "collect_form_details"
