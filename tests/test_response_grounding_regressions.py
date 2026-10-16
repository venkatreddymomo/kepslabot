import sys
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.llm_orchestration_service as orchestration_module
from schemas.chat import ConversationContext, ConversationState, IntentResult, IntentType, MessageRole
from schemas.orchestration import OrchestrationDecision
from services.chat_service import ChatService
from services.conversation_memory_service import conversation_memory_service
from services.llm_orchestration_service import LLMOrchestrationService
from services.response_validator import ResponseValidator


def test_room_slot_extraction_does_not_infer_cheapest_room_without_price_data() -> None:
    service = ChatService()
    context = ConversationContext(
        session_id="sess-room-grounding",
        hotel_code="TEST",
    )

    updates = service._extract_room_booking_slot_updates(
        message="which is the cheapest room",
        current_pending_action="collect_room_booking_details",
        state=ConversationState.AWAITING_INFO,
        pending_data={"room_type_candidates": ["Deluxe Room", "Presidential Suite"]},
        memory_facts={},
        conversation_context=context,
    )

    assert "room_type" not in updates
    assert updates["room_type_candidates"] == ["Deluxe Room", "Presidential Suite"]


def test_answer_first_knowledge_base_uses_scoped_service_kb() -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-answer-first-service-kb",
        hotel_code="TEST",
        pending_data={"_active_property_ids": ["orchid_hotel_manali"]},
    )
    target_service = {
        "id": "room_booking",
        "name": "Room Booking",
        "extracted_knowledge": (
            "=== COMMON KNOWLEDGE ===\n"
            "Shared room-booking guidance.\n"
            "=== PROPERTY: The Orchid Hotel Manali | ALIASES: orchid_hotel_manali ===\n"
            "Presidential Suite - Size: 850 sq ft\n"
            "=== PROPERTY: The Orchid Hotel Shimla | ALIASES: orchid_hotel_shimla ===\n"
            "Presidential Suite - Size: 700 sq ft\n"
        ),
    }

    combined_kb, scoped_service_kb = service._build_answer_first_knowledge_base(
        user_message="which is the largest room",
        context=context,
        target_service=target_service,
        full_kb_text="",
    )

    assert "Shared room-booking guidance." in combined_kb
    assert "850 sq ft" in scoped_service_kb
    assert "700 sq ft" not in scoped_service_kb


def test_book_answerer_response_is_not_truncated_by_post_processing() -> None:
    """
    Regression: a book-answerer reply explaining what the bot CAN'T do
    (e.g. "Currently, I can assist with room booking but am unable to
    place food orders…") used to get stripped to "Currently," by a regex
    that matched any sentence with `i can assist` + a transactional verb
    — even when the sentence was a denial. Service-offer policy now lives
    in the prompt; the deterministic strippers must stay deleted.
    """
    service = LLMOrchestrationService()

    # The exact LLM output from trace trc-c48f491ef62248eb that triggered
    # the "Currently," truncation in production (FOCUS suffix included).
    raw_answer = (
        "Currently, I can assist with room booking requests or support "
        "inquiries, but I am unable to place food orders or arrange "
        "breakfast delivery through the bot.\n\nFOCUS: orchid_hotel_manali"
    )
    cleaned, _chips, focus_ids = service._split_answer_chips_and_focus(raw_answer)

    assert focus_ids == ["orchid_hotel_manali"]
    assert cleaned.startswith("Currently,")
    assert "I am unable to place food orders" in cleaned
    assert "FOCUS:" not in cleaned

    # And the strippers themselves must not exist — they were the bug.
    assert not hasattr(service, "_strip_unsolicited_service_offers")
    assert not hasattr(service, "_is_unsolicited_service_offer_line")
    assert not hasattr(service, "_is_service_overview_request")
    assert not hasattr(service, "_sanitize_main_informational_decision")


def test_response_validator_no_longer_rewrites_phase_unavailable_cta() -> None:
    """
    Regression: the response_validator used to detect "would you like to book"
    style CTAs and rewrite them whole when the current phase had no matching
    transactional service. That path is now policy-driven by the book_answerer
    prompt; the deterministic rewriter must not exist.
    """
    validator = ResponseValidator()
    assert not hasattr(validator, "_build_phase_safe_info_response_if_needed")
    assert not hasattr(validator, "_strip_transaction_cta_sentences")


def test_book_answerer_prompt_file_seeded_with_phase_aware_policy() -> None:
    """
    The book_answerer policy must live in prompts/defaults/hotel/orchestrator.book_answerer.md
    and must encode the new phase-aware service rules: in-phase impossible services
    get a flat deny (no handoff), real leads (halls, missing prices) get one staff-handoff
    offer, and sensitive-content messages get empathy + external-help redirect with no form.
    """
    prompt_path = (
        Path(__file__).resolve().parents[1]
        / "prompts" / "defaults" / "hotel" / "orchestrator.book_answerer.md"
    )
    assert prompt_path.exists(), f"missing seed file: {prompt_path}"
    body = prompt_path.read_text(encoding="utf-8")
    # Output format
    assert "HARD OUTPUT FORMAT" in body
    # Phase-aware service policy
    assert "PHASE-AWARE SERVICE POLICY" in body
    assert "services_available_now" in body
    # Phase-impossible services flat-deny rule (towel / food / drink / etc. in pre-booking)
    assert "checked in" in body.lower() and "NO staff handoff" in body
    # Real leads get the staff-handoff offer (halls, weddings, group)
    assert "hall" in body.lower() and "handoff" in body.lower()
    # Sensitive content rule
    assert "self-harm" in body.lower() and "professional" in body.lower()
    # Never mention timeframes in handoff
    assert "Never mention timeframes" in body
    # Trailing FOCUS line still present
    assert "FOCUS:" in body


def test_main_router_prompt_has_pronoun_and_sensitive_rules() -> None:
    """
    Main router prompt must encode pronoun resolution ("book one"/"do it") against
    the immediate prior topic, and route sensitive content to book (never to support).
    """
    prompt_path = (
        Path(__file__).resolve().parents[1]
        / "prompts" / "defaults" / "hotel" / "orchestrator.main_router.md"
    )
    body = prompt_path.read_text(encoding="utf-8")
    assert "PRONOUN RESOLUTION" in body
    assert "SENSITIVE CONTENT" in body
    # Pronoun rule must explicitly mention spa / hall / room cases
    assert "spa" in body.lower() and "hall" in body.lower()
    # Sensitive content must NOT route to support-intake
    assert "Do NOT route to support-intake" in body or "do not route to support-intake" in body.lower()


def test_service_router_prompt_has_sensitive_and_phase_impossible_guardrails() -> None:
    prompt_path = (
        Path(__file__).resolve().parents[1]
        / "prompts" / "defaults" / "hotel" / "orchestrator.service_router.md"
    )
    body = prompt_path.read_text(encoding="utf-8")
    assert "SENSITIVE CONTENT" in body
    assert "PHASE-IMPOSSIBLE SERVICES" in body
    # Phase-impossible services don't trigger the support form
    assert "no form" in body.lower() or "not support-intake cases" in body.lower() or "NOT support-intake cases" in body


def test_response_validator_replaces_unsupported_execution_info_dump() -> None:
    validator = ResponseValidator()
    context = ConversationContext(
        session_id="sess-unsupported-execution",
        hotel_code="TEST",
    )
    context.add_message(MessageRole.USER, "book chamber hall")

    result = validator.validate(
        response_text=(
            "The Chamber Hall is a spacious indoor banquet hall with AV equipment "
            "and catering service, suitable for grand weddings and conferences."
        ),
        intent_result=IntentResult(
            intent=IntentType.GENERAL_SERVICE,
            confidence=0.95,
            entities={},
        ),
        context=context,
        capabilities_summary={
            "service_catalog": [
                {
                    "id": "room_booking_request",
                    "name": "Room Booking Request",
                    "description": "Room booking service",
                    "is_active": True,
                }
            ]
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert result.valid is False
    assert result.action == "replace"
    assert result.replacement_response == "I can't do that through the bot right now."


def test_retain_safe_internal_entries_drops_stale_flow_markers_after_completion() -> None:
    retained = conversation_memory_service.retain_safe_internal_entries(
        {
            "_memory": {"summary": "ctx"},
            "_active_property_ids": ["orchid_hotel_shimla"],
            "_selected_property_scope_name": "Orchid Hotel Shimla",
            "_integration": {"phase": "pre_booking"},
            "_latest_booking_id": 42,
            "_last_service_id": "room_booking_request",
            "_last_service_name": "Room Booking Request",
            "_task_state": {"active_task": {"pending_action": "collect_form_details"}},
            "_defer_summary_refresh": True,
        },
        preserve_deferred_refresh=False,
    )

    assert retained["_memory"] == {"summary": "ctx"}
    assert retained["_active_property_ids"] == ["orchid_hotel_shimla"]
    assert retained["_selected_property_scope_name"] == "Orchid Hotel Shimla"
    assert retained["_integration"] == {"phase": "pre_booking"}
    assert retained["_latest_booking_id"] == 42
    assert "_last_service_id" not in retained
    assert "_last_service_name" not in retained
    assert "_task_state" not in retained
    assert "_defer_summary_refresh" not in retained


@pytest.mark.asyncio
async def test_service_agent_gets_multi_property_scope_for_llm_switch_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-service-property-scope",
        hotel_code="TEST",
        pending_action="collect_room_booking_details",
        pending_data={
            "_known_property_scope_count": 2,
            "_active_property_ids": ["orchid_hotel_manali"],
            "_active_property_names": ["The Orchid Hotel Manali"],
            "_selected_property_scope_id": "orchid_hotel_manali",
            "_selected_property_scope_name": "The Orchid Hotel Manali",
            "service_id": "room_booking",
            "room_type": "Presidential Suite",
        },
    )

    async def fake_get_property_catalog_db(*, hotel_code):
        assert hotel_code == "TEST"
        return [
            {
                "id": "orchid_hotel_manali",
                "name": "The Orchid Hotel Manali",
                "aliases": ["Manali"],
                "location": {"label": "Manali", "city": "Manali"},
            },
            {
                "id": "orchid_hotel_shimla",
                "name": "The Orchid Hotel Shimla",
                "aliases": ["Shimla"],
                "location": {"label": "Shimla", "city": "Shimla"},
            },
        ]

    async def fake_chat_with_json(*, messages, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        property_scope = payload["property_scope"]
        assert property_scope["allow_mid_flow_property_switch"] is True
        assert property_scope["active_property_ids"] == ["orchid_hotel_manali"]
        assert [row["id"] for row in property_scope["detected_properties"]] == [
            "orchid_hotel_manali",
            "orchid_hotel_shimla",
        ]
        system_prompt = messages[0]["content"]
        assert "selected_property_ids" in system_prompt
        assert "The Orchid Hotel Manali" in system_prompt
        assert "The Orchid Hotel Shimla" in system_prompt
        return {
            "action": "respond_only",
            "response_text": "The Orchid Hotel Shimla has Deluxe and Premier rooms.",
            "relevant_to_service": True,
            "pending_action": None,
            "pending_data_updates": {},
            "missing_fields": [],
            "suggested_actions": [],
            "requires_human_handoff": False,
            "selected_property_name": "The Orchid Hotel Shimla",
            "selected_property_ids": ["orchid_hotel_shimla"],
            "ticket": {"required": False, "ready_to_create": False},
            "metadata": {},
        }

    monkeypatch.setattr(
        "services.kb_table_reader.get_property_catalog_db",
        fake_get_property_catalog_db,
    )
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    decision = await service._run_service_agent(
        user_message="actually shimla instead",
        context=context,
        memory_snapshot={},
        service={
            "id": "room_booking",
            "name": "Room Booking",
            "phase_id": "pre_booking",
            "required_slots": [{"id": "room_type"}],
            "extracted_knowledge": (
                "=== PROPERTY: The Orchid Hotel Manali | ALIASES: orchid_hotel_manali ===\n"
                "Presidential Suite\n"
                "=== PROPERTY: The Orchid Hotel Shimla | ALIASES: orchid_hotel_shimla ===\n"
                "Deluxe Room\n"
            ),
        },
        base_decision=OrchestrationDecision(
            action="dispatch_handler",
            target_service_id="room_booking",
            selected_property_ids=["orchid_hotel_manali"],
            selected_property_name="The Orchid Hotel Manali",
        ),
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
    )

    assert decision.selected_property_ids == ["orchid_hotel_shimla"]
    assert decision.selected_property_name == "The Orchid Hotel Shimla"
