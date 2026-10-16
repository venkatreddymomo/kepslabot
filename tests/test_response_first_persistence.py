import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from core.context_manager import ContextManager
from schemas.chat import ConversationContext


@pytest.mark.asyncio
async def test_save_context_keeps_live_state_in_memory_and_skips_db_when_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ContextManager(local_store_enabled=False)
    context = ConversationContext(
        session_id="sess-response-first",
        hotel_code="TEST",
        pending_data={"_defer_summary_refresh": True, "_active_property_ids": ["orchid_hotel_manali"]},
    )
    db_calls: list[str] = []

    async def fake_save_context_to_db(saved_context: ConversationContext, _db_session) -> None:
        db_calls.append(saved_context.session_id)

    monkeypatch.setattr(settings, "chat_db_first_mode", True)
    monkeypatch.setattr(settings, "chat_response_first_persistence_enabled", True)
    monkeypatch.setattr(manager, "_save_context_to_db", fake_save_context_to_db)

    await manager.save_context(context, db_session=object())

    assert db_calls == []
    restored = await manager.get_context("sess-response-first", db_session=object())
    assert restored is not None
    assert restored.pending_data["_defer_summary_refresh"] is True
    assert restored.pending_data["_active_property_ids"] == ["orchid_hotel_manali"]


@pytest.mark.asyncio
async def test_save_context_still_persists_to_db_when_response_first_mode_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ContextManager(local_store_enabled=False)
    context = ConversationContext(
        session_id="sess-response-first-disabled",
        hotel_code="TEST",
        pending_data={"_defer_summary_refresh": True},
    )
    db_calls: list[str] = []

    async def fake_save_context_to_db(saved_context: ConversationContext, _db_session) -> None:
        db_calls.append(saved_context.session_id)

    monkeypatch.setattr(settings, "chat_db_first_mode", True)
    monkeypatch.setattr(settings, "chat_response_first_persistence_enabled", False)
    monkeypatch.setattr(manager, "_save_context_to_db", fake_save_context_to_db)

    await manager.save_context(context, db_session=object())

    assert db_calls == ["sess-response-first-disabled"]
