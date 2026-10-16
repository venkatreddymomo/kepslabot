"""Session-centric leads + ticketing changes (senior's direction).

Covers:
- Lead identity is SET-ONCE / frozen (location stays latest-wins).
- Ticket entity resolves from the session LOCATION when no property scope is set.
- Unmapped location -> ticket is created LOCAL-ONLY (KePSLA), external API skipped.
- Ticket message carries the sanitized transcript of the active conversation
  thread (emoji / markdown / markers / internal form-dumps stripped), gated by
  ticketing_external_transcript_enabled.

Pure unit tests — no live DB or network (create_ticket runs in local mode).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from schemas.chat import ConversationContext
from services.ticketing_service import TicketingService

_ZONE_MAP = '{"zone": {"kolkata": "6290", "jaipur": "6295"}}'


# ── Lead freeze (set-once) ────────────────────────────────────────────────────

class _FakeRow:
    def __init__(self, **kw):
        self.session_id = kw.get("session_id")
        self.conversation_id = kw.get("conversation_id")
        self.hotel_id = kw.get("hotel_id")
        self.hotel_code = kw.get("hotel_code")
        self.guest_name = kw.get("guest_name")
        self.guest_phone = kw.get("guest_phone")
        self.guest_email = kw.get("guest_email")
        self.location = kw.get("location")
        self.channel = kw.get("channel")
        self.source = kw.get("source")
        self.raw_form_json = kw.get("raw_form_json")


def _apply_upsert_rules(row: _FakeRow, *, name="", phone="", email="", loc=""):
    """Mirror the set-once identity / latest-wins location rules from
    session_profile_service.upsert_session_profile (the else-branch)."""
    def _clean(v):
        return str(v or "").strip()
    if name and not _clean(row.guest_name):
        row.guest_name = name
    if phone and not _clean(row.guest_phone):
        row.guest_phone = phone
    if email and not _clean(row.guest_email):
        row.guest_email = email
    if loc:
        row.location = loc


def test_lead_identity_is_frozen_after_first_capture():
    row = _FakeRow(guest_name="Sana", guest_phone="+911111111111",
                   guest_email="sana@x.com", location="Kolkata")
    # A later form edits everything — identity must NOT change, location updates.
    _apply_upsert_rules(row, name="Trip", phone="+922222222222",
                        email="trip@x.com", loc="Jaipur")
    assert row.guest_name == "Sana"
    assert row.guest_phone == "+911111111111"
    assert row.guest_email == "sana@x.com"
    assert row.location == "Jaipur"  # location is latest-wins


def test_lead_fills_blank_identity_fields_only():
    row = _FakeRow(guest_name="Sana")  # phone/email never captured yet
    _apply_upsert_rules(row, name="Trip", phone="+999", email="t@x.com", loc="Goa")
    assert row.guest_name == "Sana"          # already set -> frozen
    assert row.guest_phone == "+999"         # was blank -> filled
    assert row.guest_email == "t@x.com"      # was blank -> filled
    assert row.location == "Goa"


# ── Entity resolution from session location ──────────────────────────────────

def _build(monkeypatch, *, pending, issue="room booking request", message="Guest wants a room."):
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(session_id="sess-zone", hotel_code="zone", pending_data=dict(pending))
    return service.build_lumira_ticket_payload(context, issue=issue, message=message, phase="pre_booking")


def test_entity_resolved_from_location_when_no_property_scope(monkeypatch):
    # No property selected, but the session captured location=Kolkata -> 6290,
    # and the ticket is NOT routed local-only (real FMS entity resolved).
    payload = _build(
        monkeypatch,
        pending={"_integration": {"organisation_id": "5703", "phase": "pre_booking"},
                 "location": "Kolkata"},
        issue="need help", message="please assist",
    )
    assert payload["organisation_id"] == "6290"
    assert payload.get("_force_local_only") is None


def test_selected_property_still_wins_over_location(monkeypatch):
    # Explicit property scope (Jaipur) beats the broader location (Kolkata).
    payload = _build(
        monkeypatch,
        pending={"_integration": {"organisation_id": "5703", "phase": "pre_booking"},
                 "_selected_property_scope_name": "Zone Park Jaipur",
                 "location": "Kolkata"},
    )
    assert payload["organisation_id"] == "6295"


def test_unmapped_location_flags_local_only(monkeypatch):
    # A location with no FMS id -> not mapped -> create LOCAL-ONLY (skip API).
    payload = _build(
        monkeypatch,
        pending={"_integration": {"organisation_id": "5703", "phase": "pre_booking"},
                 "location": "Udaipur"},
        issue="need help", message="please assist",
    )
    assert payload["organisation_id"] == "5703"
    assert payload.get("_force_local_only") is True


# ── create_ticket: force-local path creates a KePSLA ticket, skips API ───────

@pytest.mark.asyncio
async def test_force_local_creates_local_ticket_skips_api(monkeypatch):
    service = TicketingService()
    # Guard: if the code path tried the external API, this would blow up.
    async def _boom(*a, **k):  # pragma: no cover - must NOT be called
        raise AssertionError("external API must not be called for a local-only ticket")
    monkeypatch.setattr(service, "_request_json", _boom, raising=False)

    captured = {}
    async def _fake_local(payload):
        captured["payload"] = payload
        from services.ticketing_service import TicketingResult
        return TicketingResult(success=True, ticket_id="LOCAL-1", status_code=201, payload=payload)
    monkeypatch.setattr(service, "_create_ticket_local", _fake_local, raising=False)
    # Avoid the up-front LLM summary call.
    async def _noop_desc(payload):
        return payload
    monkeypatch.setattr(service, "_apply_kepsla_description", _noop_desc, raising=False)
    # Never touch the real DB in a unit test.
    async def _noop_save(payload, res):
        return None
    monkeypatch.setattr(service, "_save_ticket_to_db", _noop_save, raising=False)

    result = await service.create_ticket({
        "_force_local_only": True,
        "organisation_id": "5703",
        "issue": "need help",
        "message": "please assist",
        "session_id": "s1",
    })
    assert result.success is True
    assert result.ticket_id == "LOCAL-1"
    # The internal flag must be stripped before reaching the local writer.
    assert "_force_local_only" not in captured["payload"]


# ── Ticket message sanitization ──────────────────────────────────────────────

def test_sanitize_strips_markers_markdown_emoji():
    dirty = (
        "**Bazaar** is open 24/7 ⭐\n"
        "<<SHOW_BOOKING_BUTTON: Zone Park Kolkata>>\n"
        "Visit &lt;here&gt; for # details \U0001F600"
    )
    clean = TicketingService._sanitize_ticket_text(dirty)
    assert "<<" not in clean and ">>" not in clean
    assert "**" not in clean
    assert "⭐" not in clean and "\U0001F600" not in clean
    assert "&lt;" not in clean
    assert "Bazaar is open 24/7" in clean
    # Words survive even though markdown markers (incl. '>') are stripped.
    assert "Visit" in clean and "here" in clean and "details" in clean


def test_sanitize_preserves_multilingual_letters():
    # Hindi + Korean must survive (non-English tickets still go through).
    text = TicketingService._sanitize_ticket_text("मेरे कमरे की सफाई करें ⭐ 방 청소 해주세요")
    assert "मेरे" in text
    assert "방" in text
    assert "⭐" not in text


def test_build_message_context_includes_sanitized_transcript(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_external_transcript_enabled", True)
    from schemas.chat import MessageRole
    service = TicketingService()
    ctx = ConversationContext(session_id="s", hotel_code="zone", pending_data={})
    ctx.add_message(MessageRole.USER, "can u book a room")
    ctx.add_message(MessageRole.ASSISTANT, "Lovely choice — here is the booking link ⭐")
    msg = service._build_ticket_message_context(
        context=ctx,
        explicit_message="Guest submitted a Support Request. Issue Details: clean my room.",
        issue_text="clean my room",
    )
    # Transcript now carries the actual turns, labeled "Guest"/"Bot" (mirrors the
    # KePSLA conversation view) + sanitized, turns separated by a blank line.
    assert "Guest: can u book a room" in msg
    assert "Bot: Lovely choice" in msg
    assert "⭐" not in msg                    # emoji stripped
    assert "Issue Summary: clean my room" in msg


def test_build_message_context_excludes_internal_form_dump(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_external_transcript_enabled", True)
    from schemas.chat import MessageRole
    service = TicketingService()
    ctx = ConversationContext(session_id="s", hotel_code="zone", pending_data={})
    ctx.add_message(MessageRole.USER, "i need a top floor room")
    # The structured form-submission bubble must never reach the transcript.
    ctx.add_message(
        MessageRole.USER,
        "Active Property Ids: ['zone_park_jammu'] Service Id: support_request "
        "Full Name: jaax Phone: +918122345678 Entity Id: 5703",
    )
    msg = service._build_ticket_message_context(
        context=ctx,
        explicit_message="top floor room",
        issue_text="top floor room",
    )
    assert "Guest: i need a top floor room" in msg
    assert "Active Property Ids" not in msg
    assert "Entity Id" not in msg


def test_transcript_scoped_to_active_conversation_uid(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_external_transcript_enabled", True)
    from schemas.chat import MessageRole
    service = TicketingService()
    ctx = ConversationContext(session_id="s", hotel_code="zone", pending_data={})
    # Thread 1 (an earlier conversation in the same session).
    ctx.pending_data["_active_conversation_uid"] = "t1"
    ctx.add_message(MessageRole.USER, "tell me about jammu")
    # Thread 2 (the one that raises the ticket).
    ctx.pending_data["_active_conversation_uid"] = "t2"
    ctx.add_message(MessageRole.USER, "i want to complain")
    msg = service._build_ticket_message_context(
        context=ctx, explicit_message="complaint", issue_text="complaint",
    )
    assert "i want to complain" in msg
    assert "tell me about jammu" not in msg   # other thread excluded


def test_build_message_context_summary_only_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_external_transcript_enabled", False)
    from schemas.chat import MessageRole
    service = TicketingService()
    ctx = ConversationContext(session_id="s", hotel_code="zone", pending_data={})
    ctx.add_message(MessageRole.USER, "can u book a room")
    ctx.add_message(MessageRole.ASSISTANT, "Lovely choice — here is the booking link ⭐")
    msg = service._build_ticket_message_context(
        context=ctx,
        explicit_message="Guest submitted a Support Request. Issue Details: clean my room.",
        issue_text="clean my room",
    )
    assert "Guest submitted a Support Request" in msg
    assert "Lovely choice" not in msg       # no transcript when flag is off
    assert len(msg) <= 1500
