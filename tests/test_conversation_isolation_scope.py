"""Regression: a fresh conversation_id must start clean.

Repro of the Coimbatore "locks to one property + jumps to Goa" bug — a stale
single-property scope (``_active_property_ids``) from a PRIOR conversation under
the same session_id leaked into a new chat. Message history was already scoped by
``_active_conversation_uid``; this pins the same isolation for pending_data + flow
state so old threads never bleed into the new one.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas.chat import ConversationContext, ConversationState, MessageRole
from services.conversation_memory_service import conversation_memory_service as cm


def _locked_context() -> ConversationContext:
    ctx = ConversationContext(session_id="sess-iso", hotel_code="zone")
    ctx.pending_data = {
        "_active_conversation_uid": "convA",
        "_active_property_ids": ["zone_connect_park_coimbatore"],
        "_active_property_names": ["Zone Connect Park Coimbatore"],
        "_selected_property_scope_id": "zone_connect_park_coimbatore",
        "_memory": {"summary": "asked about coimbatore amenities"},
        "_past_property_context": {"zone_connect_park_coimbatore": "Previously discussed"},
        "_integration": {"widget_id": 7, "group_id": 3},
    }
    ctx.pending_action = "awaiting_form"
    ctx.suspended_services = [{"service_id": "support_intake"}]
    ctx.resume_prompt_sent = True
    return ctx


def test_same_conversation_keeps_scope() -> None:
    ctx = _locked_context()
    assert cm.reset_pending_for_conversation_rotation(ctx, "convA") is False
    assert ctx.pending_data["_active_property_ids"] == ["zone_connect_park_coimbatore"]
    assert "_memory" in ctx.pending_data


def test_new_conversation_wipes_scope_and_memory() -> None:
    ctx = _locked_context()
    assert cm.reset_pending_for_conversation_rotation(ctx, "convB") is True
    pd = ctx.pending_data
    assert pd["_active_conversation_uid"] == "convB"
    # The leaky scalars that caused the single-property lock are gone.
    for leaked in (
        "_active_property_ids",
        "_active_property_names",
        "_selected_property_scope_id",
        "_memory",
        "_past_property_context",
    ):
        assert leaked not in pd, f"{leaked} leaked into the fresh conversation"
    # Durable visitor linkage survives so delivery/routing still works.
    assert pd["_integration"] == {"widget_id": 7, "group_id": 3}
    # Flow state on the context object is reset too.
    assert ctx.state == ConversationState.IDLE
    assert ctx.pending_action is None
    assert ctx.suspended_services == []
    assert ctx.resume_prompt_sent is False


def test_first_turn_of_session_is_noop() -> None:
    ctx = ConversationContext(session_id="sess-fresh", hotel_code="zone")
    ctx.pending_data = {}
    assert cm.reset_pending_for_conversation_rotation(ctx, "convX") is False


def test_history_already_scoped_then_new_scope_clean() -> None:
    """End-to-end-ish: old-conversation message is excluded after rotation."""
    ctx = _locked_context()
    ctx.add_message(MessageRole.USER, "old coimbatore msg")  # tagged convA
    cm.reset_pending_for_conversation_rotation(ctx, "convB")
    ctx.pending_data["_active_conversation_uid"] = "convB"
    ctx.add_message(MessageRole.USER, "new msg")  # tagged convB
    recent = ctx.get_recent_messages(10)
    contents = [m.content for m in recent]
    assert "new msg" in contents
    assert "old coimbatore msg" not in contents
