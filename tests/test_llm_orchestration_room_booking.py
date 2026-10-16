import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas.orchestration import OrchestrationDecision
from services.llm_orchestration_service import llm_orchestration_service


def test_extract_confirmable_room_from_last_assistant_requires_single_room_booking_prompt() -> None:
    assert (
        llm_orchestration_service._extract_confirmable_room_from_last_assistant(
            "Would you like me to proceed with booking the Lux Suite?"
        )
        == "Lux Suite"
    )
    assert (
        llm_orchestration_service._extract_confirmable_room_from_last_assistant(
            "Would you like assistance with booking a room now?"
        )
        == ""
    )
    assert (
        llm_orchestration_service._extract_confirmable_room_from_last_assistant(
            "The Lux Suite offers panoramic city views and a separate seating area."
        )
        == ""
    )


def test_assistant_message_invites_room_booking_without_forcing_room_choice() -> None:
    assert llm_orchestration_service._assistant_message_invites_room_booking(
        "Would you like assistance with booking a room now?"
    )
    assert not llm_orchestration_service._is_generic_affirmation("yes, the Lux Suite")


def test_enforce_explicit_room_type_selection_rejects_property_only_choice() -> None:
    decision = OrchestrationDecision(
        action="collect_info",
        pending_action="collect_form_details",
        response_text="Please fill in the booking details below.",
        pending_data_updates={"room_type": "IRA by Orchid Mumbai"},
    )

    updated = llm_orchestration_service._enforce_explicit_room_type_selection(
        decision=decision,
        user_message="I'd like to go ahead with IRA by Orchid Mumbai.",
        history_bundle={
            "last_assistant_message": (
                "You've chosen to book a room at IRA by Orchid Mumbai. "
                "Here are the room options available: Deluxe Room, Executive Room, Suite. "
                "Please confirm which specific room type you'd like to book."
            )
        },
        extracted_knowledge=(
            "Deluxe Room\n"
            "Executive Room\n"
            "Suite"
        ),
        trigger_field_id="room_type",
        trigger_field_label="Room Type",
    )

    assert updated.pending_action == "select_room_type"
    assert updated.pending_data_updates.get("room_type") in (None, "")
    assert updated.metadata["room_booking_requires_explicit_room_type"] is True
    assert "choose a room type" in updated.response_text.lower()


def test_build_service_runtime_json_contract_accepts_room_booking_service_id() -> None:
    contract = llm_orchestration_service._build_service_runtime_json_contract(
        service_id="room_booking",
        confirmation_pending_action="confirm_booking",
        current_phase_id="pre_booking",
        ticketing_mode="form",
        trigger_field_id="room_type",
        trigger_field_label="Room Type",
        form_field_names=["guest_name", "phone"],
        service_type="booking",
        service_cuisine="",
        service_description="Room booking",
    )

    assert "ROOM BOOKING SPECIAL RULE" in contract


def test_no_deterministic_sanitizer_runs_on_book_answer_decisions() -> None:
    """
    Service-offer policy used to be enforced by `_sanitize_main_informational_decision`,
    which regex-stripped lines containing offers and filtered chip actions.
    That stripper bug caused live answers to be truncated to "Currently,". The
    rule now lives in the book_answerer prompt; the stripper must stay deleted.
    """
    assert not hasattr(llm_orchestration_service, "_sanitize_main_informational_decision")
    assert not hasattr(llm_orchestration_service, "_strip_unsolicited_service_offers")
