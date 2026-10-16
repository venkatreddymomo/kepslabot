import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.llm_orchestration_service as orchestration_module
from api.routes import chat as chat_routes
from schemas.chat import ConversationContext
from schemas.orchestration import OrchestrationDecision, SuggestionChip
from services.llm_orchestration_service import LLMOrchestrationService


@pytest.mark.asyncio
async def test_schedule_post_response_chip_job_regenerates_visible_chips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    session_id = "sess-canonical-chip-job"
    context = ConversationContext(session_id=session_id, hotel_code="TEST")
    decision = OrchestrationDecision(
        action="respond_only",
        response_text="The pool is open from 8 AM to 8 PM.",
        suggested_actions=["Old inline chip"],
    )
    captured: dict[str, object] = {}

    async def fake_run_next_action_suggestion_agent(**kwargs):
        captured["book_route_context"] = kwargs.get("book_route_context")
        captured["main_router_book_ids"] = kwargs.get("main_router_book_ids")
        return ["What spa facilities do you have?"]

    monkeypatch.setattr(service, "_run_next_action_suggestion_agent", fake_run_next_action_suggestion_agent)

    service.schedule_post_response_chip_job(
        session_id=session_id,
        user_message="Tell me about the pool.",
        context=context,
        decision=decision,
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        book_route_context={"context_text": "Pool and spa details."},
        main_router_book_ids=["book_1"],
    )

    chips = await service.await_post_response_chips(session_id=session_id, wait_ms=20)

    assert chips == ["What spa facilities do you have?"]
    assert captured["book_route_context"] == {"context_text": "Pool and spa details."}
    assert captured["main_router_book_ids"] == ["book_1"]


def test_schedule_post_response_chip_job_logs_form_collect_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = LLMOrchestrationService()

    with caplog.at_level(logging.INFO):
        service.schedule_post_response_chip_job(
            session_id="sess-form-skip",
            assistant_turn_id="turn_form_skip",
            user_message="I need support.",
            context=ConversationContext(session_id="sess-form-skip", hotel_code="TEST"),
            decision=OrchestrationDecision(
                action="collect_info",
                pending_action="collect_form_details",
                response_text="Please fill in the details below.",
                suggested_actions=[],
            ),
            selected_phase_id="pre_booking",
            selected_phase_name="Pre Booking",
        )

    assert "reason=form_collect_no_seeded" in caplog.text
    assert "[CHIP_TURN] skipped" in caplog.text


@pytest.mark.asyncio
async def test_next_action_suggestion_agent_scopes_service_kb_and_stays_in_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-service-chip-scope",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": ["goa_north"],
            "_active_property_names": ["Goa North"],
        },
    )
    decision = OrchestrationDecision(
        action="respond_only",
        response_text="The Premier King Room has a balcony and work desk.",
    )
    captured: dict[str, object] = {}

    async def fake_prompt_get(_key: str) -> str:
        return "Return JSON only."

    async def fake_chat_with_json(**kwargs):
        captured["payload"] = json.loads(kwargs["messages"][1]["content"])
        return {"suggested_actions": ["Tell me more about the Premier King Room"]}

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    target_service = {
        "id": "room_booking",
        "name": "Room Booking",
        "profile": "rooms",
        "service_prompt_pack": {
            "required_slots": [],
            "extracted_knowledge": (
                "=== PROPERTY: Goa North ===\n"
                "Premier King Room has a balcony and work desk.\n\n"
                "=== PROPERTY: Goa South ===\n"
                "Reserve Suite has a plunge pool."
            ),
        },
    }

    suggestions = await service._run_next_action_suggestion_agent(
        user_message="Tell me about that room.",
        context=context,
        decision=decision,
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        target_service=target_service,
        prefetched_services=[
            {
                "id": "spa_booking",
                "name": "Spa Booking",
                "is_active": True,
                "phase_id": "pre_booking",
                "service_prompt_pack": {
                    "extracted_knowledge": "=== PROPERTY: Goa North ===\nSpa open 9-9."
                },
            }
        ],
    )

    payload = captured["payload"]
    assert [chip.text for chip in suggestions] == ["Tell me more about the Premier King Room"]
    assert suggestions[0].origin_responder == "service"
    assert suggestions[0].origin_service_id == "room_booking"
    assert suggestions[0].target_service_id == "room_booking"
    assert payload["answering_llm"] == "service_agent"
    assert "Goa North" in payload["answering_service_kb"]["extracted_knowledge"]
    assert "Goa South" not in payload["answering_service_kb"]["extracted_knowledge"]
    assert payload["allowed_services"] == []


@pytest.mark.asyncio
async def test_next_action_suggestion_agent_logs_generation_summary(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-chip-log-summary",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": ["goa_north"],
            "_active_property_names": ["Goa North"],
        },
    )
    decision = OrchestrationDecision(
        action="respond_only",
        response_text="The hotel has a spa and an infinity pool.",
    )

    async def fake_prompt_get(_key: str) -> str:
        return "Return JSON only."

    async def fake_chat_with_json(**_kwargs):
        return {
            "suggested_actions": [
                {
                    "text": "What are the spa hours?",
                    "target_service_id": "spa_booking",
                    "evidence": "Spa is open 9 AM to 9 PM.",
                }
            ]
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    with caplog.at_level(logging.INFO):
        suggestions = await service._run_next_action_suggestion_agent(
            assistant_turn_id="turn_chip_log",
            user_message="Tell me about amenities.",
            context=context,
            decision=decision,
            selected_phase_id="pre_booking",
            selected_phase_name="Pre Booking",
            prefetched_services=[
                {
                    "id": "spa_booking",
                    "name": "Spa Booking",
                    "is_active": True,
                    "phase_id": "pre_booking",
                    "description": "Spa details",
                    "service_prompt_pack": {
                        "extracted_knowledge": "=== PROPERTY: Goa North ===\nSpa is open 9 AM to 9 PM."
                    },
                }
            ],
            book_route_context={
                "context_text": "Goa North has a spa and infinity pool.",
                "books": [
                    {
                        "id": "north_amenities",
                        "title": "Goa North Amenities",
                        "property_id": "goa_north",
                        "property_name": "Goa North",
                    }
                ],
                "book_count": 1,
                "page_count": 2,
            },
            main_router_book_ids=["north_amenities"],
        )

    assert [chip.text for chip in suggestions] == ["What are the spa hours?"]
    assert "[CHIP_TURN] generated" in caplog.text
    assert "raw=1" in caplog.text
    assert "validated=1" in caplog.text
    assert "final=1" in caplog.text


@pytest.mark.asyncio
async def test_next_action_suggestion_agent_includes_scoped_book_context_for_main_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-main-chip-scope",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": ["goa_north"],
            "_active_property_names": ["Goa North"],
        },
    )
    decision = OrchestrationDecision(
        action="respond_only",
        response_text="The hotel has a spa and an infinity pool.",
    )
    captured: dict[str, object] = {}

    async def fake_prompt_get(_key: str) -> str:
        return "Return JSON only."

    async def fake_chat_with_json(**kwargs):
        captured["payload"] = json.loads(kwargs["messages"][1]["content"])
        return {
            "suggested_actions": [
                {
                    "text": "What are the spa hours?",
                    "target_service_id": "spa_booking",
                    "evidence": "Spa is open 9 AM to 9 PM.",
                }
            ]
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    suggestions = await service._run_next_action_suggestion_agent(
        user_message="Tell me about amenities.",
        context=context,
        decision=decision,
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        prefetched_services=[
            {
                "id": "spa_booking",
                "name": "Spa Booking",
                "is_active": True,
                "phase_id": "pre_booking",
                "description": "Spa details",
                "service_prompt_pack": {
                    "extracted_knowledge": (
                        "=== PROPERTY: Goa North ===\n"
                        "Spa is open 9 AM to 9 PM.\n\n"
                        "=== PROPERTY: Goa South ===\n"
                        "No spa details listed."
                    )
                },
            }
        ],
        book_route_context={
            "context_text": "Goa North has a spa and infinity pool.",
            "books": [
                {
                    "id": "north_amenities",
                    "title": "Goa North Amenities",
                    "property_id": "goa_north",
                    "property_name": "Goa North",
                }
            ],
            "book_count": 1,
            "page_count": 2,
        },
        main_router_book_ids=["north_amenities"],
    )

    payload = captured["payload"]
    assert [chip.text for chip in suggestions] == ["What are the spa hours?"]
    assert suggestions[0].origin_responder == "main"
    assert suggestions[0].active_property_ids == ["goa_north"]
    assert suggestions[0].selected_book_ids == ["north_amenities"]
    assert payload["answering_llm"] == "main_orchestrator"
    assert payload["answering_book_kb"]["book_ids"] == ["north_amenities"]
    assert "Goa North has a spa" in payload["answering_book_kb"]["context_text"]
    assert payload["property_scope"]["active_property_ids"] == ["goa_north"]
    assert payload["allowed_services"][0]["name"] == "Spa Booking"
    assert "Goa North" in payload["allowed_services"][0]["kb_hint"]
    assert "Goa South" not in payload["allowed_services"][0]["kb_hint"]


@pytest.mark.asyncio
async def test_next_action_suggestion_agent_builds_fallback_main_grounding_from_active_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-main-chip-fallback-grounding",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": ["orchid_hotel_pune", "fort_jadhavgadh"],
            "_active_property_names": ["Orchid Hotel Pune", "Fort Jadhavgadh"],
        },
    )
    decision = OrchestrationDecision(
        action="respond_only",
        response_text="Here are the Pune properties.",
        selected_property_ids=["orchid_hotel_pune", "fort_jadhavgadh"],
    )
    captured: dict[str, object] = {}

    async def fake_prompt_get(_key: str) -> str:
        return "Return JSON only."

    async def fake_chat_with_json(**kwargs):
        captured["payload"] = json.loads(kwargs["messages"][1]["content"])
        return {"suggested_actions": [{"text": "Tell me more about Fort Jadhavgadh"}]}

    async def fake_get_kb_text_for_properties(**kwargs):
        assert kwargs["property_ids"] == ["orchid_hotel_pune", "fort_jadhavgadh"]
        return {
            "scoped_kb_text": (
                "=== PROPERTY: Orchid Hotel Pune ===\n"
                "5-star eco-friendly luxury hotel near Balewadi.\n\n"
                "=== PROPERTY: Fort Jadhavgadh ===\n"
                "Heritage luxury fort hotel with royal suites, restaurants, museum, spa, and pool."
            )
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)
    monkeypatch.setattr(
        orchestration_module.config_service,
        "get_kb_text_for_properties",
        fake_get_kb_text_for_properties,
    )

    suggestions = await service._run_next_action_suggestion_agent(
        user_message="pune",
        context=context,
        decision=decision,
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        prefetched_services=[],
        book_route_context=None,
        main_router_book_ids=[],
    )

    payload = captured["payload"]
    assert [chip.text for chip in suggestions] == ["Tell me more about Fort Jadhavgadh"]
    assert payload["answering_llm"] == "main_orchestrator"
    assert "Fort Jadhavgadh" in payload["answering_book_kb"]["context_text"]
    assert payload["property_scope"]["active_property_ids"] == ["orchid_hotel_pune", "fort_jadhavgadh"]


@pytest.mark.asyncio
async def test_suggestions_endpoint_returns_ready_chips_with_prefetch_batch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_await_post_response_chip_objects(*, session_id: str, assistant_turn_id: str, wait_ms: int):
        assert session_id == "sess-ready-chips"
        assert assistant_turn_id == "turn_1"
        assert wait_ms >= 40
        return [
            SuggestionChip(
                text="What are the spa hours?",
                turn_trace_id="turn_1",
                origin_responder="main",
                active_property_ids=["goa_north"],
                selected_book_ids=["amenities_book"],
                evidence_excerpt="Spa is open 9 AM to 9 PM.",
                evidence_fingerprint="fp_turn_1",
            )
        ]

    async def fake_load_runtime_snapshot(*, session_id: str, db):
        assert session_id == "sess-ready-chips"
        return {
            "conversation_history": [{"role": "assistant", "content": "The hotel has a spa."}],
            "conversation_state": "idle",
            "pending_action": "",
            "pending_data_public": {},
            "known_context": {},
            "memory_snapshot": {},
            "active_service": {},
        }

    async def fake_schedule_prefetch(*, request, hotel_code: str, runtime_snapshot, suggestions):
        assert hotel_code == "TEST"
        assert [chip.text for chip in suggestions] == ["What are the spa hours?"]
        assert suggestions[0].turn_trace_id == "turn_1"
        assert runtime_snapshot["pending_data_public"]["active_property_ids"] == ["goa_south"]
        return "pf_ready"

    monkeypatch.setattr(chat_routes.config_service, "resolve_hotel_code", lambda code: "TEST")
    monkeypatch.setattr(chat_routes.db_config_service, "set_hotel_context", lambda _code: object())
    monkeypatch.setattr(chat_routes.db_config_service, "reset_hotel_context", lambda _token: None)
    monkeypatch.setattr(
        chat_routes.llm_orchestration_service,
        "get_post_response_chip_runtime_snapshot",
        lambda **_kwargs: {
            "conversation_history": [{"role": "assistant", "content": "The hotel has a spa."}],
            "conversation_state": "idle",
            "pending_action": "",
            "pending_data_public": {"active_property_ids": ["goa_south"]},
            "known_context": {},
            "memory_snapshot": {},
            "active_service": {},
            "phase_id_hint": "pre_booking",
        },
    )
    monkeypatch.setattr(
        chat_routes.llm_orchestration_service,
        "await_post_response_chip_objects",
        fake_await_post_response_chip_objects,
    )
    monkeypatch.setattr(chat_routes, "_load_suggestions_runtime_snapshot", fake_load_runtime_snapshot)
    monkeypatch.setattr(chat_routes, "_schedule_prefetch_for_visible_suggestions", fake_schedule_prefetch)

    result = await chat_routes.get_contextual_suggestions(
        chat_routes.SuggestionsRequest(
            last_bot_message="The hotel has a spa.",
            user_message="Tell me about amenities.",
            hotel_code="TEST",
            current_phase="pre_booking",
            session_id="sess-ready-chips",
            assistant_turn_id="turn_1",
            fallback_suggestions=["Bad fallback"],
        ),
        db=None,
    )

    assert result == {
        "suggestions": ["What are the spa hours?"],
        "prefetch_batch_id": "pf_ready",
    }


@pytest.mark.asyncio
async def test_suggestions_endpoint_logs_delivery_summary(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_await_post_response_chip_objects(*, session_id: str, assistant_turn_id: str, wait_ms: int):
        assert session_id == "sess-chip-log-delivery"
        assert assistant_turn_id == "turn_log_1"
        assert wait_ms >= 40
        return [
            SuggestionChip(
                text="What are the spa hours?",
                turn_trace_id="turn_log_1",
                origin_responder="main",
                active_property_ids=["goa_north"],
                selected_book_ids=["amenities_book"],
                evidence_excerpt="Spa is open 9 AM to 9 PM.",
                evidence_fingerprint="fp_turn_log_1",
            )
        ]

    async def fake_load_runtime_snapshot(*, session_id: str, db):
        assert session_id == "sess-chip-log-delivery"
        return {
            "conversation_history": [{"role": "assistant", "content": "The hotel has a spa."}],
            "conversation_state": "idle",
            "pending_action": "",
            "pending_data_public": {},
            "known_context": {},
            "memory_snapshot": {},
            "active_service": {},
        }

    async def fake_schedule_prefetch(*, request, hotel_code: str, runtime_snapshot, suggestions):
        return "pf_log"

    monkeypatch.setattr(chat_routes.config_service, "resolve_hotel_code", lambda code: "TEST")
    monkeypatch.setattr(chat_routes.db_config_service, "set_hotel_context", lambda _code: object())
    monkeypatch.setattr(chat_routes.db_config_service, "reset_hotel_context", lambda _token: None)
    monkeypatch.setattr(
        chat_routes.llm_orchestration_service,
        "get_post_response_chip_runtime_snapshot",
        lambda **_kwargs: {
            "conversation_history": [{"role": "assistant", "content": "The hotel has a spa."}],
            "conversation_state": "idle",
            "pending_action": "",
            "pending_data_public": {"active_property_ids": ["goa_north"]},
            "known_context": {},
            "memory_snapshot": {},
            "active_service": {},
            "active_service_id": "",
            "phase_id_hint": "pre_booking",
        },
    )
    monkeypatch.setattr(
        chat_routes.llm_orchestration_service,
        "await_post_response_chip_objects",
        fake_await_post_response_chip_objects,
    )
    monkeypatch.setattr(chat_routes, "_load_suggestions_runtime_snapshot", fake_load_runtime_snapshot)
    monkeypatch.setattr(chat_routes, "_schedule_prefetch_for_visible_suggestions", fake_schedule_prefetch)

    with caplog.at_level(logging.INFO):
        result = await chat_routes.get_contextual_suggestions(
            chat_routes.SuggestionsRequest(
                last_bot_message="The hotel has a spa.",
                user_message="Tell me about amenities.",
                hotel_code="TEST",
                current_phase="pre_booking",
                session_id="sess-chip-log-delivery",
                assistant_turn_id="turn_log_1",
            ),
            db=None,
        )

    assert result == {
        "suggestions": ["What are the spa hours?"],
        "prefetch_batch_id": "pf_log",
    }
    assert "[CHIP_TURN] delivered" in caplog.text
    assert "result=visible" in caplog.text
    assert "ready=1" in caplog.text
    assert "quality=1" in caplog.text
    assert "visible=1" in caplog.text


@pytest.mark.asyncio
async def test_suggestions_endpoint_returns_empty_without_ready_chips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_await_post_response_chip_objects(*, session_id: str, assistant_turn_id: str, wait_ms: int):
        assert session_id == "sess-no-chips"
        assert assistant_turn_id == ""
        return []

    monkeypatch.setattr(chat_routes.config_service, "resolve_hotel_code", lambda code: "TEST")
    monkeypatch.setattr(chat_routes.db_config_service, "set_hotel_context", lambda _code: object())
    monkeypatch.setattr(chat_routes.db_config_service, "reset_hotel_context", lambda _token: None)
    monkeypatch.setattr(
        chat_routes.llm_orchestration_service,
        "await_post_response_chip_objects",
        fake_await_post_response_chip_objects,
    )

    result = await chat_routes.get_contextual_suggestions(
        chat_routes.SuggestionsRequest(
            last_bot_message="The hotel has a spa.",
            user_message="Tell me about amenities.",
            hotel_code="TEST",
            current_phase="pre_booking",
            session_id="sess-no-chips",
            fallback_suggestions=["Do you have a pool?"],
        ),
        db=None,
    )

    assert result == {"suggestions": [], "prefetch_batch_id": None}


@pytest.mark.asyncio
async def test_suggestions_endpoint_suppresses_stale_turn_chips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_await_post_response_chip_objects(*, session_id: str, assistant_turn_id: str, wait_ms: int):
        assert session_id == "sess-stale-chips"
        assert assistant_turn_id == ""
        return [
            SuggestionChip(
                text="What are the spa hours?",
                origin_responder="main",
                evidence_excerpt="Spa is open 9 AM to 9 PM.",
                evidence_fingerprint="fp_stale",
            )
        ]

    async def fake_load_runtime_snapshot(*, session_id: str, db):
        assert session_id == "sess-stale-chips"
        return {
            "conversation_history": [{"role": "user", "content": "Actually show me rooms."}],
            "conversation_state": "idle",
            "pending_action": "",
            "pending_data_public": {},
            "known_context": {},
            "memory_snapshot": {},
            "active_service": {},
        }

    async def fake_schedule_prefetch(**_kwargs):
        raise AssertionError("stale chip turns should not schedule prefetch")

    monkeypatch.setattr(chat_routes.config_service, "resolve_hotel_code", lambda code: "TEST")
    monkeypatch.setattr(chat_routes.db_config_service, "set_hotel_context", lambda _code: object())
    monkeypatch.setattr(chat_routes.db_config_service, "reset_hotel_context", lambda _token: None)
    monkeypatch.setattr(
        chat_routes.llm_orchestration_service,
        "await_post_response_chip_objects",
        fake_await_post_response_chip_objects,
    )
    monkeypatch.setattr(chat_routes, "_load_suggestions_runtime_snapshot", fake_load_runtime_snapshot)
    monkeypatch.setattr(chat_routes, "_schedule_prefetch_for_visible_suggestions", fake_schedule_prefetch)

    result = await chat_routes.get_contextual_suggestions(
        chat_routes.SuggestionsRequest(
            last_bot_message="The hotel has a spa.",
            user_message="Tell me about amenities.",
            hotel_code="TEST",
            current_phase="pre_booking",
            session_id="sess-stale-chips",
        ),
        db=None,
    )

    assert result == {"suggestions": [], "prefetch_batch_id": None}


@pytest.mark.asyncio
async def test_await_post_response_chip_objects_returns_empty_for_turn_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()

    async def fake_run_next_action_suggestion_agent(**_kwargs):
        return [
            SuggestionChip(
                text="Tell me more about the Premier King Room",
                turn_trace_id="turn_1",
                origin_responder="main",
                active_property_ids=["goa_north"],
                selected_book_ids=["room_book"],
                evidence_excerpt="Premier King Room has a balcony and work desk.",
                evidence_fingerprint="fp_turn_1",
            )
        ]

    monkeypatch.setattr(service, "_run_next_action_suggestion_agent", fake_run_next_action_suggestion_agent)

    service.schedule_post_response_chip_job(
        session_id="sess-turn-binding",
        assistant_turn_id="turn_1",
        user_message="Tell me about that room.",
        context=ConversationContext(session_id="sess-turn-binding", hotel_code="TEST"),
        decision=OrchestrationDecision(action="respond_only", response_text="The Premier King Room has a balcony."),
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
    )

    chips = await service.await_post_response_chip_objects(
        session_id="sess-turn-binding",
        assistant_turn_id="turn_2",
        wait_ms=20,
    )

    assert chips == []


def test_validate_generated_suggestion_chips_rejects_unsupported_specific_detail() -> None:
    service = LLMOrchestrationService()

    chips = service._validate_generated_suggestion_chips(
        candidates=[{"text": "What spa treatments are available?"}],
        turn_trace_id="turn_1",
        origin_responder="main",
        origin_service_id="",
        target_service_kb_by_id={},
        primary_grounding_text="Spa is open from 9 AM to 9 PM.",
        assistant_response="The spa is open from 9 AM to 9 PM.",
        active_property_ids=["goa_north"],
        selected_book_ids=["spa_book"],
        allowed_service_ids=[],
    )

    assert chips == []


def test_validate_generated_suggestion_chips_rejects_answered_fact_repetition() -> None:
    service = LLMOrchestrationService()

    chips = service._validate_generated_suggestion_chips(
        candidates=[{"text": "What are the spa hours?"}],
        turn_trace_id="turn_1",
        origin_responder="main",
        origin_service_id="",
        target_service_kb_by_id={},
        primary_grounding_text="Spa is open from 9 AM to 9 PM.",
        assistant_response="The spa is open from 9 AM to 9 PM.",
        active_property_ids=["goa_north"],
        selected_book_ids=["spa_book"],
        allowed_service_ids=[],
    )

    assert chips == []


def test_validate_generated_suggestion_chips_builds_scoped_main_chip_metadata() -> None:
    service = LLMOrchestrationService()

    chips = service._validate_generated_suggestion_chips(
        candidates=[{"text": "Tell me more about the Premier King Room"}],
        turn_trace_id="turn_1",
        origin_responder="main",
        origin_service_id="",
        target_service_kb_by_id={},
        primary_grounding_text="Premier King Room has a balcony and work desk.",
        assistant_response="The Premier King Room has a balcony.",
        active_property_ids=["goa_north"],
        selected_book_ids=["room_book"],
        allowed_service_ids=[],
    )

    assert len(chips) == 1
    chip = chips[0]
    assert chip.origin_responder == "main"
    assert chip.active_property_ids == ["goa_north"]
    assert chip.selected_book_ids == ["room_book"]
    assert chip.evidence_fingerprint


def test_validate_generated_suggestion_chips_keeps_service_lane_target() -> None:
    service = LLMOrchestrationService()

    chips = service._validate_generated_suggestion_chips(
        candidates=[{"text": "Tell me more about the Premier King Room"}],
        turn_trace_id="turn_1",
        origin_responder="service",
        origin_service_id="room_booking",
        target_service_kb_by_id={
            "room_booking": "Premier King Room has a balcony and work desk.",
            "spa_booking": "Spa is open from 9 AM to 9 PM.",
        },
        primary_grounding_text="Premier King Room has a balcony and work desk.",
        assistant_response="The Premier King Room has a balcony and work desk.",
        active_property_ids=["goa_north"],
        selected_book_ids=[],
        allowed_service_ids=["room_booking", "spa_booking"],
    )

    assert len(chips) == 1
    chip = chips[0]
    assert chip.origin_responder == "service"
    assert chip.origin_service_id == "room_booking"
    assert chip.target_service_id == "room_booking"


def test_apply_chip_click_route_lock_preserves_service_lane() -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(session_id="sess-chip-lock-service", hotel_code="TEST", pending_data={})

    route, hint = service._apply_chip_click_route_lock(
        context=context,
        selected_phase_id="pre_booking",
        main_router_route="book",
        main_router_hint={"route": "book", "confidence": 0.4},
        chip_click_context={
            "is_chip_click_followup": True,
            "origin_responder": "service",
            "origin_service_id": "room_booking",
            "active_property_ids": ["goa_north"],
            "selected_book_ids": [],
        },
    )

    assert route == "service"
    assert hint["service_id"] == "room_booking"
    assert context.pending_data["_active_property_ids"] == ["goa_north"]


def test_apply_chip_click_route_lock_blocks_service_continuity_for_main_chip() -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(session_id="sess-chip-lock-main", hotel_code="TEST", pending_data={})

    route, hint = service._apply_chip_click_route_lock(
        context=context,
        selected_phase_id="pre_booking",
        main_router_route="auto",
        main_router_hint={"route": "auto", "confidence": 0.3, "book_ids": ["fresh_room_book"]},
        chip_click_context={
            "is_chip_click_followup": True,
            "origin_responder": "main",
            "active_property_ids": ["goa_north", "goa_south"],
            "selected_book_ids": ["stale_pool_book"],
        },
    )

    assert route == "book"
    assert hint["service_continuity_blocked"] is True
    assert hint["book_ids"] == ["fresh_room_book"]
    assert hint["chip_click_seed_book_ids"] == ["stale_pool_book"]
    assert context.pending_data["_active_property_ids"] == ["goa_north", "goa_south"]


@pytest.mark.parametrize("hotel_code, kept", [("t24", ["Yes, please have them contact me", "No, thanks"]), ("zone", ["No, thanks"])])
def test_yes_reply_to_the_bots_own_question_rests_on_that_question(
    monkeypatch: pytest.MonkeyPatch, hotel_code: str, kept: list[str]
) -> None:
    # T24 UAT, 15 Oct: the yes cited the bot's question, but "them" and "contact" are not
    # in it or in the KB, so only "No, thanks" was shown. Pre-booking tab hotels keep it;
    # every other hotel is unchanged.
    from services import prebooking_settings_service

    monkeypatch.setattr(prebooking_settings_service, "is_admin_managed", lambda code: code == "t24")
    service = LLMOrchestrationService()
    question = "Would you like our team to reach out to assist you with this?"

    chips = service._validate_generated_suggestion_chips(
        candidates=[
            {"text": "Yes, please have them contact me", "evidence": question},
            {"text": "No, thanks", "evidence": question},
        ],
        turn_trace_id="turn_1",
        origin_responder="main",
        origin_service_id="",
        target_service_kb_by_id={},
        primary_grounding_text="Workhard: 9th floor meeting room for 25 to 30 guests, projector and AV support.",
        assistant_response=(
            "- I can't book the Workhard venue directly through the chat.\n"
            "- The Workhard meeting room at T24 Residency is on the 9th floor.\n"
            f"- {question}"
        ),
        active_property_ids=["t24_residency_andheri_east"],
        selected_book_ids=["meetings_events"],
        allowed_service_ids=[],
        hotel_code=hotel_code,
    )

    assert [chip.text for chip in chips] == kept
