import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas.orchestration import OrchestrationDecision
from services.llm_orchestration_service import llm_orchestration_service


def test_room_missing_info_handoff_clause_pre_booking_is_judgment_based() -> None:
    clause = llm_orchestration_service._build_room_missing_info_handoff_clause("pre_booking")
    lower = clause.lower()
    # Offers the staff callback...
    assert "would you like our team to reach out" in lower
    # ...for genuine booking-relevant facts beyond the old keyword list (occupancy).
    assert "occupancy" in lower
    # ...but explicitly NOT for trivial in-room amenity trivia.
    assert "telephone" in lower
    assert "judgment" in lower


def test_room_missing_info_handoff_clause_other_phases_no_callback() -> None:
    for phase in ("during_stay", "post_checkout", "pre_checkin", ""):
        clause = llm_orchestration_service._build_room_missing_info_handoff_clause(phase)
        lower = clause.lower()
        assert "would you like our team to reach out" not in lower
        assert "never offer a staff callback for a missing detail in this phase" in lower


def test_room_missing_info_handoff_clause_accepts_phase_aliases() -> None:
    # "prebooking" / "pre booking" normalize to pre_booking and still offer the callback.
    for alias in ("prebooking", "pre booking", "PRE_BOOKING"):
        clause = llm_orchestration_service._build_room_missing_info_handoff_clause(alias)
        assert "would you like our team to reach out" in clause.lower()


def test_room_booking_missing_price_or_size_offers_staff_handoff() -> None:
    decision = OrchestrationDecision(
        action="respond_only",
        response_text=(
            "- Standard Room - no public details on size or price.\n"
            "- Deluxe Room - no public details on size or price.\n"
            "Is there a particular room type you'd like to know more about?"
        ),
    )

    updated = llm_orchestration_service._normalize_room_booking_missing_detail_handoff(
        decision=decision,
        user_message="i need the prices and sizes",
        service_id="room_booking_request",
        service_name="Room Booking Request",
        description="Help guests explore and book rooms.",
        trigger_field_id="room_type",
    )

    assert updated.response_text == (
        "- I don't have that detail right now.\n"
        "- Would you like our team to reach out to share that with you?"
    )
    assert updated.metadata["room_booking_missing_detail_handoff_forced"] is True
    assert updated.suggested_actions == []


def test_room_booking_plain_browsing_does_not_force_staff_handoff() -> None:
    decision = OrchestrationDecision(
        action="respond_only",
        response_text=(
            "- Standard Room - a comfortable stay.\n"
            "- Deluxe Room - extra space and city views.\n"
            "- Suite - a premium option with a separate living area."
        ),
    )

    updated = llm_orchestration_service._normalize_room_booking_missing_detail_handoff(
        decision=decision,
        user_message="what rooms do u have",
        service_id="room_booking_request",
        service_name="Room Booking Request",
        description="Help guests explore and book rooms.",
        trigger_field_id="room_type",
    )

    assert updated.response_text == decision.response_text
    assert "room_booking_missing_detail_handoff_forced" not in updated.metadata


def test_support_form_guard_requires_submit_even_with_prefill() -> None:
    decision = OrchestrationDecision(
        action="collect_info",
        response_text="Lovely choice - just a few quick details below and our team will take it from there. Reference: #7349.",
        ticket={"required": True, "ready_to_create": True},
    )

    updated = llm_orchestration_service._normalize_support_form_open_decision(
        decision=decision,
        service_id="support_request",
        service_name="Support Request",
        description="Support intake form for staff callbacks.",
        known_context={
            "guest_name": "Sna",
            "guest_phone": "+919899789798",
            "guest_email": "sa@gmail.com",
        },
    )

    assert updated.action == "collect_info"
    assert updated.pending_action == "collect_form_details"
    assert updated.missing_fields == []
    assert updated.ticket.required is True
    assert updated.ticket.ready_to_create is False
    assert updated.requires_human_handoff is False
    assert updated.metadata["support_form_submit_required"] is True
    assert "review the details below and submit" in updated.response_text.lower()
    assert "reference" not in updated.response_text.lower()


def test_support_form_guard_recovers_from_stripped_create_ticket_without_prefill() -> None:
    decision = OrchestrationDecision(
        action="respond_only",
        response_text="Noted.",
        ticket={"required": True, "ready_to_create": False},
        metadata={"form_mode_stripped_create_ticket": True},
    )

    updated = llm_orchestration_service._normalize_support_form_open_decision(
        decision=decision,
        service_id="support_request",
        service_name="Support Request",
        description="Support intake form for staff callbacks.",
        known_context={},
    )

    assert updated.action == "collect_info"
    assert updated.pending_action == "collect_form_details"
    assert updated.ticket.ready_to_create is False
    assert "fill in the details below and submit" in updated.response_text.lower()
