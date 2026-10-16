"""Regression: the bot's own replies must stay in the chat thread.

Repro of the Zone "history shows only what I typed" bug. Every message is
stamped with the active ``conversation_uid`` by
``ConversationContext.add_message``, which reads it out of ``pending_data`` — and
the widget replays only the rows whose uid matches the thread it is resuming.

``retain_safe_internal_entries`` runs whenever a flow completes, which happens
MID-TURN: after the guest's message is appended and before the reply is. It
dropped ``_active_conversation_uid`` along with the flow state, so the guest's
row carried the thread id and the assistant's row was written with
``conversation_uid`` NULL. On the next load the widget filtered every one of the
bot's answers out and replayed a conversation of the guest talking to nobody.

Live evidence (GHN_PROD_BAK, session of 2026-09-09): a single thread held five
user rows all tagged ``1a0841aca8d-…`` interleaved with assistant rows tagged
NULL, and 2023 of the assistant rows in the table had no uid at all.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemas.chat import ConversationContext, MessageRole
from services.conversation_memory_service import conversation_memory_service as cm


def _uid(msg) -> str:
    return str((msg.metadata or {}).get("conversation_uid") or "")


def test_reply_keeps_the_uid_after_a_flow_completes_mid_turn() -> None:
    ctx = ConversationContext(session_id="sess-uid", hotel_code="zone")
    ctx.pending_data["_active_conversation_uid"] = "convA"
    ctx.pending_data["_task_state"] = {"step": "collecting"}

    ctx.add_message(MessageRole.USER, "Give me 10 biryanis")
    # A flow finishes between the guest's message and the answer to it.
    ctx.pending_data = cm.retain_safe_internal_entries(ctx.pending_data)
    ctx.add_message(MessageRole.ASSISTANT, "We do serve biryanis, but ...")

    assert _uid(ctx.messages[0]) == "convA"
    assert _uid(ctx.messages[1]) == "convA", (
        "the bot's reply lost its thread id — chat history will replay the "
        "guest's messages with no answers"
    )


def test_the_cleanup_still_forgets_the_flow_it_meant_to_forget() -> None:
    ctx = ConversationContext(session_id="sess-uid", hotel_code="zone")
    ctx.pending_data = {
        "_active_conversation_uid": "convA",
        "_task_state": {"step": "collecting"},
        "_last_service_id": "98",
    }
    retained = cm.retain_safe_internal_entries(ctx.pending_data)
    assert retained["_active_conversation_uid"] == "convA"
    assert "_task_state" not in retained
    assert "_last_service_id" not in retained


def test_history_scoping_sees_both_sides_of_the_turn() -> None:
    """get_recent_messages scopes by the same uid the widget filters on."""
    ctx = ConversationContext(session_id="sess-uid", hotel_code="zone")
    ctx.pending_data["_active_conversation_uid"] = "convA"
    ctx.add_message(MessageRole.USER, "Give me 10 biryanis")
    ctx.pending_data = cm.retain_safe_internal_entries(ctx.pending_data)
    ctx.add_message(MessageRole.ASSISTANT, "We do serve biryanis, but ...")

    roles = [m.role.value for m in ctx.get_recent_messages(10)]
    assert roles == ["user", "assistant"]


def test_form_and_ticket_messages_keep_the_uid_and_their_payload() -> None:
    """The form bubble and its confirmations are the same bug's worst case.

    On the form-submit path the guest's message is appended AFTER the service
    handler runs, not before — so unlike an ordinary turn, the cleanup ate the
    thread id off BOTH sides. A guest reloading saw the Jaipur chat with their
    submitted details and the confirmation both missing (live example: DB
    conversation 4498, two NULL rows in an otherwise fully tagged thread).
    """
    ctx = ConversationContext(session_id="sess-form", hotel_code="zone")
    ctx.pending_data["_active_conversation_uid"] = "convA"
    ctx.pending_data["_task_state"] = {"step": "collecting"}
    ctx.add_message(MessageRole.USER, "can someone from your team call me back")
    ctx.add_message(MessageRole.ASSISTANT, "Happy to arrange that - a few details please.")

    # submit_form: the service handler completes the flow, THEN the form bubble
    # and confirmations are appended.
    ctx.pending_data = cm.retain_safe_internal_entries(ctx.pending_data)
    form_payload = {
        "form_submission": {"Full Name": "Ravi Kumar", "Mobile Number": "9876543210"},
        "form_service_name": "Support Request",
    }
    ctx.add_message(
        MessageRole.USER,
        "Full Name: Ravi Kumar\nMobile Number: 9876543210",
        metadata={"display_payload": form_payload},
    )
    ctx.add_message(MessageRole.ASSISTANT, "Thanks Ravi Kumar - we've received your request.")
    ctx.add_message(MessageRole.ASSISTANT, "In the meantime, want to explore our rooms?")

    assert [_uid(m) for m in ctx.messages] == ["convA"] * 5, (
        "a form submission or its confirmation lost the thread id"
    )
    # The rich form bubble must keep its payload — that is what chat history
    # replays instead of the raw "Label: value" text.
    form_row = ctx.messages[2]
    assert (form_row.metadata or {}).get("display_payload") == form_payload
