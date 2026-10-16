import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.suggestion_prefetch_service as prefetch_module
from schemas.chat import ChatResponse, ConversationContext, ConversationState
from schemas.orchestration import SuggestionChip
from services.llm_orchestration_service import LLMOrchestrationService
from services.suggestion_prefetch_service import PrefetchBatch, PrefetchEntry, SuggestionPrefetchService


def test_sync_property_scope_pending_updates_all_scope_fields() -> None:
    pending_data: dict[str, object] = {}

    scope = LLMOrchestrationService._sync_property_scope_pending(
        pending_data=pending_data,
        property_ids=["goa_north", "goa_south"],
        active_property_names=["Ira Orchid Porvorim", "Orchid Passaros Goa South Goa"],
        selected_property_name="Ira Orchid Porvorim",
    )

    assert scope["active_property_ids"] == ["goa_north", "goa_south"]
    assert pending_data["_active_property_ids"] == ["goa_north", "goa_south"]
    assert pending_data["active_property_ids"] == ["goa_north", "goa_south"]
    assert pending_data["_selected_property_scope_ids"] == ["goa_north", "goa_south"]
    assert pending_data["_selected_property_scope_id"] == "goa_north"
    assert pending_data["_selected_property_scope_name"] == "Ira Orchid Porvorim"


def test_rehydrate_pending_data_public_prefers_active_scope_over_stale_legacy_scope() -> None:
    service = SuggestionPrefetchService()

    restored = service._rehydrate_pending_data_public(
        {
            "active_property_ids": ["goa_north", "goa_south"],
            "active_property_names": ["Ira Orchid Porvorim", "Orchid Passaros Goa South Goa"],
            "selected_property_scope_id": "old_manali",
            "selected_property_scope_ids": ["old_manali"],
            "selected_property_scope_name": "Old Manali Property",
        }
    )

    assert restored["_active_property_ids"] == ["goa_north", "goa_south"]
    assert restored["_selected_property_scope_ids"] == ["goa_north", "goa_south"]
    assert restored["_selected_property_scope_id"] == "goa_north"
    assert restored["_selected_property_scope_name"] == "Ira Orchid Porvorim"


@pytest.mark.asyncio
async def test_prefetch_generation_uses_chat_service_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SuggestionPrefetchService()
    batch = PrefetchBatch(
        batch_id="pf_chat_service_flow",
        session_id="sess-origin",
        hotel_code="TEST",
        assistant_turn_id="turn_1",
        phase_id="pre_booking",
        conversation_state="idle",
        last_bot_message="How can I help?",
        triggering_user_message="Tell me about Goa.",
        services_allowed_in_phase=[{"id": "room_booking", "name": "Room Booking", "is_active": True}],
        pending_data_public={"active_property_ids": ["goa_north"]},
    )

    saved_contexts: dict[str, ConversationContext] = {}
    deleted_session_ids: list[str] = []

    async def fake_save_context(context: ConversationContext, db_session=None) -> None:
        saved_contexts[context.session_id] = context

    async def fake_get_context(session_id: str, db_session=None):
        return saved_contexts.get(session_id)

    async def fake_delete_context(session_id: str, db_session=None) -> bool:
        deleted_session_ids.append(session_id)
        saved_contexts.pop(session_id, None)
        return True

    async def fake_process_message(request, db_session=None, prefetched_services=None, defer_summary_refresh=False):
        assert request.session_id.startswith("prefetch::")
        assert request.message == "Tell me about the pool"
        assert defer_summary_refresh is True
        assert prefetched_services == [{"id": "room_booking", "name": "Room Booking", "is_active": True}]
        assert request.metadata["source_type"] == "suggested_action"
        assert request.metadata["chip_origin_responder"] == "service"
        assert request.metadata["chip_origin_service_id"] == "room_booking"
        assert request.metadata["chip_target_service_id"] == "room_booking"
        assert request.metadata["chip_active_property_ids"] == ["goa_north"]
        assert request.metadata["chip_selected_book_ids"] == ["pool_book"]
        return ChatResponse(
            session_id=request.session_id,
            message="The pool is open-air and part of the wellness area.",
            state=ConversationState.IDLE,
            suggested_actions=["Tell me about the spa"],
            metadata={"selected_property_ids": ["goa_north"]},
        )

    monkeypatch.setattr(prefetch_module.context_manager, "save_context", fake_save_context)
    monkeypatch.setattr(prefetch_module.context_manager, "get_context", fake_get_context)
    monkeypatch.setattr(prefetch_module.context_manager, "delete_context", fake_delete_context)
    monkeypatch.setattr(prefetch_module.chat_service, "process_message", fake_process_message)
    monkeypatch.setattr(
        prefetch_module.llm_orchestration_service,
        "_cancel_session_background_work",
        lambda _session_id: 0,
    )
    chip = SuggestionChip(
        text="Tell me about the pool",
        turn_trace_id="turn_1",
        origin_responder="service",
        origin_service_id="room_booking",
        target_service_id="room_booking",
        active_property_ids=["goa_north"],
        selected_book_ids=["pool_book"],
        evidence_excerpt="The pool is open-air and part of the wellness area.",
        evidence_fingerprint="fp_pool",
    )

    response, context = await service._generate_with_orchestration_flow(
        batch=batch,
        entry=PrefetchEntry(
            entry_id="e_1",
            chip_text=chip.text,
            chip_data=chip.model_dump(mode="json"),
        ),
    )

    assert isinstance(response, ChatResponse)
    assert response.message.startswith("The pool is open-air")
    assert context.session_id.startswith("prefetch::")
    assert deleted_session_ids == [context.session_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("hotel_code, admin_managed", [("t24", True), ("zone", False)])
async def test_prefetch_drops_collect_info_answer_for_admin_hotels(
    monkeypatch: pytest.MonkeyPatch, hotel_code: str, admin_managed: bool
) -> None:
    # chat_service puts orchestration_action under metadata["entities"]; the
    # guard must read it there, or "Yes, please have the team contact me" is
    # cached and replayed with no form (T24 prod, 15 Oct).
    service = SuggestionPrefetchService()
    batch = PrefetchBatch(
        batch_id="pf_collect_info",
        session_id="sess-origin",
        conversation_id="conv-origin",
        hotel_code=hotel_code,
        assistant_turn_id="turn_1",
        phase_id="pre_booking",
        conversation_state="idle",
        last_bot_message="Would you like our team to reach out to help with the booking?",
        triggering_user_message="book one workhard venue",
        services_allowed_in_phase=[{"id": "support_request", "name": "Support Request", "is_active": True}],
        pending_data_public={},
    )
    saved_contexts: dict[str, ConversationContext] = {}

    async def fake_save_context(context: ConversationContext, db_session=None) -> None:
        saved_contexts[context.session_id] = context

    async def fake_get_context(session_id: str, db_session=None):
        return saved_contexts.get(session_id)

    async def fake_delete_context(session_id: str, db_session=None) -> bool:
        saved_contexts.pop(session_id, None)
        return True

    async def fake_process_message(request, db_session=None, prefetched_services=None, defer_summary_refresh=False):
        return ChatResponse(
            session_id=request.session_id,
            message="Thank you for confirming. Please provide details about your request so our team can assist you promptly.",
            state=ConversationState.AWAITING_INFO,
            metadata={
                "entities": {
                    "orchestration_action": "collect_info",
                    "target_service_id": "support_request",
                    "pending_action": "collect_form_details",
                },
                "response_source": "llm_orchestration",
            },
        )

    monkeypatch.setattr(prefetch_module.context_manager, "save_context", fake_save_context)
    monkeypatch.setattr(prefetch_module.context_manager, "get_context", fake_get_context)
    monkeypatch.setattr(prefetch_module.context_manager, "delete_context", fake_delete_context)
    monkeypatch.setattr(prefetch_module.chat_service, "process_message", fake_process_message)
    monkeypatch.setattr(prefetch_module.ticketing_service, "consume_prefetch_suppression", lambda _sid: False)
    monkeypatch.setattr(
        prefetch_module.llm_orchestration_service,
        "_cancel_session_background_work",
        lambda _session_id: 0,
    )
    from services import prebooking_settings_service

    monkeypatch.setattr(prebooking_settings_service, "is_admin_managed", lambda code: code == "t24")

    response, _context = await service._generate_with_orchestration_flow(
        batch=batch,
        entry=PrefetchEntry(entry_id="e_1", chip_text="Yes, please have the team contact me", chip_data={}),
    )

    assert bool(response.metadata.get("prefetch_ticket_suppressed")) is admin_managed


@pytest.mark.asyncio
async def test_resolve_chip_click_context_returns_structured_chip_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SuggestionPrefetchService()
    monkeypatch.setattr(prefetch_module.settings, "openai_api_key", "test-key", raising=False)

    async def fake_generate_answer_for_entry(_batch_id, _entry_id):
        return None

    monkeypatch.setattr(service, "_generate_answer_for_entry", fake_generate_answer_for_entry)
    chip = SuggestionChip(
        text="Tell me about the pool",
        turn_trace_id="turn_1",
        origin_responder="main",
        origin_service_id="",
        target_service_id="spa_booking",
        active_property_ids=["goa_north"],
        selected_book_ids=["pool_book"],
        evidence_excerpt="The pool is open from 7 AM to 10 PM.",
        evidence_fingerprint="fp_pool",
    )

    batch_id = await service.schedule_batch(
        session_id="sess-chip-context",
        hotel_code="TEST",
        assistant_turn_id="turn_1",
        phase_id="pre_booking",
        conversation_state="idle",
        last_bot_message="The pool is open from 7 AM to 10 PM.",
        triggering_user_message="Tell me about the pool.",
        suggestions=[chip],
    )

    assert batch_id

    chip_context = await service.resolve_chip_click_context(
        session_id="sess-chip-context",
        user_message="Tell me about the pool",
        batch_id=batch_id,
    )

    assert chip_context["turn_trace_id"] == "turn_1"
    assert chip_context["origin_responder"] == "main"
    assert chip_context["target_service_id"] == "spa_booking"
    assert chip_context["active_property_ids"] == ["goa_north"]
    assert chip_context["selected_book_ids"] == ["pool_book"]


@pytest.mark.asyncio
async def test_exact_chip_priority_waits_for_prefetched_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SuggestionPrefetchService()
    batch_id = "pf_waits"
    session_id = "sess-waits"
    entry_id = "e_1"
    entry = PrefetchEntry(
        entry_id=entry_id,
        chip_text="Tell me about the pool",
        status="running",
    )
    entry.task_key = service._entry_task_key(batch_id=batch_id, entry_id=entry_id)
    batch = PrefetchBatch(
        batch_id=batch_id,
        session_id=session_id,
        hotel_code="TEST",
        assistant_turn_id="turn_1",
        phase_id="pre_booking",
        conversation_state="idle",
        last_bot_message="How can I help?",
        triggering_user_message="Tell me about Goa.",
        expires_at=prefetch_module._utc_now() + timedelta(seconds=30),
        entries={entry_id: entry},
    )

    async with service._lock:
        service._batches[batch_id] = batch
        service._session_batches[session_id] = [batch_id]

    async def complete_entry() -> None:
        await asyncio.sleep(0.02)
        async with service._lock:
            current_entry = service._batches[batch_id].entries[entry_id]
            current_entry.answer_payload = {"message": "Cached pool answer", "metadata": {}}
            current_entry.status = "ready"
            current_entry.updated_at = prefetch_module._utc_now()

    task = asyncio.create_task(complete_entry())
    service._entry_tasks[entry.task_key] = task
    monkeypatch.setattr(prefetch_module.settings, "chat_chip_speculation_click_wait_ms", 250, raising=False)

    payload = await service._try_resolve_exact_chip_priority(
        session_id=session_id,
        user_message="Tell me about the pool",
        batch_id=batch_id,
    )

    assert payload is not None
    assert payload["message"] == "Cached pool answer"
    assert payload["metadata"]["prefetch_batch_id"] == batch_id
