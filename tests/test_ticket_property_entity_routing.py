"""Per-property entity (organisation_id) routing for multi-property tenants.

Confirms a Zone ticket is filed under the FMS entity id of the SELECTED property
(Kolkata -> 6290, Jaipur -> 6295) instead of the hotel-wide / fallback entity.
Pure unit test — no DB, no network.
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


def _build(monkeypatch, *, hotel_code, pending):
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="sess-zone",
        hotel_code=hotel_code,
        pending_data=dict(pending),
    )
    payload = service.build_lumira_ticket_payload(
        context,
        issue="room booking request",
        message="Guest wants to book a room.",
        phase="pre_booking",
    )
    return payload


def test_zone_kolkata_routes_to_fms_6290(monkeypatch):
    payload = _build(
        monkeypatch,
        hotel_code="zone",
        pending={
            "_integration": {"organisation_id": "5703", "phase": "pre_booking"},
            "_selected_property_scope_name": "Zone Park Kolkata",
        },
    )
    assert payload["organisation_id"] == "6290"


def test_zone_jaipur_routes_to_fms_6295(monkeypatch):
    payload = _build(
        monkeypatch,
        hotel_code="zone",
        pending={
            "_integration": {"organisation_id": "5703", "phase": "pre_booking"},
            "_selected_property_scope_name": "Zone Park Jaipur",
        },
    )
    assert payload["organisation_id"] == "6295"


def test_zone_unknown_property_keeps_fallback(monkeypatch):
    # A Zone property NOT in the map must keep the existing hotel-wide entity.
    payload = _build(
        monkeypatch,
        hotel_code="zone",
        pending={
            "_integration": {"organisation_id": "5703", "phase": "pre_booking"},
            "_selected_property_scope_name": "Zone Connect Park Goa",
        },
    )
    assert payload["organisation_id"] == "5703"


def test_non_zone_tenant_untouched(monkeypatch):
    # Other tenants have no mapping -> no override, no regression.
    payload = _build(
        monkeypatch,
        hotel_code="orchid",
        pending={
            "_integration": {"organisation_id": "5703", "phase": "pre_booking"},
            "_selected_property_scope_name": "Orchid Kolkata",
        },
    )
    assert payload["organisation_id"] == "5703"


def test_zone_no_property_flags_wrong_tenant(monkeypatch):
    # No property selected -> entity stays the 5703 fallback (a DIFFERENT tenant,
    # ROHL). Must be flagged so create_ticket routes it LOCAL-ONLY (KePSLA),
    # skipping the external API (no mis-route, but the guest still gets a ticket).
    payload = _build(
        monkeypatch,
        hotel_code="zone",
        pending={"_integration": {"organisation_id": "5703", "phase": "pre_booking"}},
    )
    assert payload["organisation_id"] == "5703"
    assert payload.get("_force_local_only") is True


def test_zone_kolkata_not_flagged(monkeypatch):
    payload = _build(
        monkeypatch,
        hotel_code="zone",
        pending={
            "_integration": {"organisation_id": "5703", "phase": "pre_booking"},
            "_selected_property_scope_name": "Zone Park Kolkata",
        },
    )
    assert payload["organisation_id"] == "6290"
    assert payload.get("_force_local_only") is None  # configured property -> allowed


def test_non_zone_never_flagged(monkeypatch):
    # A tenant with no per-property map must never be blocked (no regression).
    payload = _build(
        monkeypatch,
        hotel_code="orchid",
        pending={"_integration": {"organisation_id": "5703", "phase": "pre_booking"}},
    )
    assert payload.get("_force_local_only") is None


def test_prebooking_leaves_guest_id_empty(monkeypatch):
    # Leads have no real guest record; Lumira sends guest_id="" so the dashboard's
    # guest_id->guest-record name lookup doesn't hit a dead synthetic id.
    payload = _build(
        monkeypatch,
        hotel_code="zone",
        pending={"_integration": {"organisation_id": "5703", "phase": "pre_booking"}},
    )
    assert payload["guest_id"] == ""


def test_during_stay_still_synthesizes_guest_id(monkeypatch):
    # Non-pre-booking keeps the existing synthetic fallback (no regression).
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="sess-zone-stay",
        hotel_code="zone",
        pending_data={"_integration": {"organisation_id": "5703", "phase": "during_stay"}},
    )
    payload = service.build_lumira_ticket_payload(
        context, issue="ac not working", message="room 101", phase="during_stay"
    )
    assert payload["guest_id"]  # synthetic, non-empty


def test_prebooking_ticket_forced_low_priority(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_prebooking_force_low_priority", True, raising=False)
    service = TicketingService()
    context = ConversationContext(session_id="s", hotel_code="zone", pending_data={})
    payload = service.build_lumira_ticket_payload(
        context, issue="x", message="y", phase="pre_booking", priority="medium"
    )
    assert payload["priority"] == "LOW"


def test_prebooking_critical_not_downgraded(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_prebooking_force_low_priority", True, raising=False)
    service = TicketingService()
    context = ConversationContext(session_id="s", hotel_code="zone", pending_data={})
    payload = service.build_lumira_ticket_payload(
        context, issue="x", message="y", phase="pre_booking", priority="critical"
    )
    assert payload["priority"] == "CRITICAL"


def test_during_stay_priority_untouched(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_prebooking_force_low_priority", True, raising=False)
    service = TicketingService()
    context = ConversationContext(session_id="s", hotel_code="zone", pending_data={})
    payload = service.build_lumira_ticket_payload(
        context, issue="x", message="y", phase="during_stay", priority="high"
    )
    assert payload["priority"] == "HIGH"


def test_prebooking_priority_respected_by_default(monkeypatch):
    # New default (force_low_priority=False): the LLM-assigned priority stands
    # for pre-booking tickets — they are NOT silently pinned to LOW.
    monkeypatch.setattr(settings, "ticketing_prebooking_force_low_priority", False, raising=False)
    service = TicketingService()
    context = ConversationContext(session_id="s", hotel_code="zone", pending_data={})
    for given, expected in (("high", "HIGH"), ("medium", "MEDIUM"), ("critical", "CRITICAL")):
        payload = service.build_lumira_ticket_payload(
            context, issue="x", message="y", phase="pre_booking", priority=given
        )
        assert payload["priority"] == expected, given


def test_zone_property_rescued_from_issue_text(monkeypatch):
    # The structured property scope is MISSING (e.g. lead context rebuilt after
    # the guest edited the prefilled form), but the ticket's issue text still
    # names the property. It must resolve to the property FMS entity and NOT be
    # blocked as a wrong tenant — this is the "edited name -> no ticket" fix.
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="sess-zone",
        hotel_code="zone",
        pending_data={"_integration": {"organisation_id": "5703", "phase": "pre_booking"}},
    )
    payload = service.build_lumira_ticket_payload(
        context,
        issue="Support Request request at Zone Park Kolkata",
        message="Guest: need to clean my room",
        phase="pre_booking",
    )
    assert payload["organisation_id"] == "6290"
    assert payload.get("_force_local_only") is None


def test_zone_rescue_from_message_text(monkeypatch):
    # Property named in the longer message body (not the short issue) also works.
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="sess-zone",
        hotel_code="zone",
        pending_data={"_integration": {"organisation_id": "5703", "phase": "pre_booking"}},
    )
    payload = service.build_lumira_ticket_payload(
        context,
        issue="Support Request",
        message="Guest needs help with their booking at Zone Park Jaipur.",
        phase="pre_booking",
    )
    assert payload["organisation_id"] == "6295"


def test_zone_no_property_in_text_still_blocked(monkeypatch):
    # No structured scope AND no property named in text -> still flagged, so we
    # never mis-route to a foreign tenant (no regression on the safety guard).
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="sess-zone",
        hotel_code="zone",
        pending_data={"_integration": {"organisation_id": "5703", "phase": "pre_booking"}},
    )
    payload = service.build_lumira_ticket_payload(
        context,
        issue="Support Request",
        message="Guest needs assistance.",
        phase="pre_booking",
    )
    assert payload["organisation_id"] == "5703"
    assert payload.get("_force_local_only") is True


_ZONE_FULL_MAP = (
    '{"zone": {"kolkata": "6290", "new market": "6302", "jaipur": "6295", '
    '"calangute": "6289", "coimbatore": "6292", "connect park coimbatore": "6300", '
    '"jammu": "6296", "pathankot": "6298", "patna": "6301", "mussoorie": "6299", '
    '"saket": "6297", "chennai": "6291", "gopalpur": "6288", "dimapur": "6293", '
    '"niathu": "6293", "phalodi": "6294", "darjeeling": "6304", "gangtok": "6303", '
    '"siniolchu": "6303", "jaisalmer": "6305", "lansdowne": "6306", '
    '"parra": "6307", "port blair": "6308", "udaipur": "6309", "digha": "6310"}}'
)


@pytest.mark.parametrize(
    "scope_name,expected",
    [
        ("Zone Park Kolkata", "6290"),
        ("Zone by The Park New Market Kolkata", "6302"),  # collision: specific wins
        ("Zone by The Park Jaipur", "6295"),
        ("Zone Connect Park Calangute Goa", "6289"),
        ("Zone by The Park Coimbatore", "6292"),
        ("Zone Connect Park Coimbatore", "6300"),  # collision: specific wins
        ("Zone by The Park Jammu", "6296"),
        ("Zone by The Park Pathankot", "6298"),
        ("Zone Connect Park Patna", "6301"),
        ("Zone Connect Park Mussoorie", "6299"),
        ("Zone Connect Park Saket Delhi", "6297"),
        ("Zone Park Chennai", "6291"),
        ("Zone Park Gopalpur", "6288"),
        ("Zone Niathu Park Dimapur", "6293"),
        ("Zone Palace Park Phalodi", "6294"),
        ("Zone Connect Park Jaisalmer", "6305"),
        ("Zone Connect Park Lansdowne", "6306"),
        ("Zone Connect Park Parra Goa", "6307"),
        ("Zone Connect Park Port Blair", "6308"),
        ("Zone Connect Park Udaipur", "6309"),
        ("Zone Park Digha", "6310"),
    ],
)
def test_full_map_structured_resolution(monkeypatch, scope_name, expected):
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_FULL_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="s",
        hotel_code="zone",
        pending_data={
            "_integration": {"organisation_id": "5703", "phase": "pre_booking"},
            "_selected_property_scope_name": scope_name,
        },
    )
    payload = service.build_lumira_ticket_payload(context, issue="x", message="y", phase="pre_booking")
    assert payload["organisation_id"] == expected
    assert payload.get("_force_local_only") is None


def test_full_map_coimbatore_collision_via_text(monkeypatch):
    # Nested keys ("coimbatore" ⊂ "connect park coimbatore") collapse to the
    # specific Connect entity, not ambiguous.
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_FULL_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="s", hotel_code="zone",
        pending_data={"_integration": {"organisation_id": "5703", "phase": "pre_booking"}},
    )
    payload = service.build_lumira_ticket_payload(
        context, issue="Support Request at Zone Connect Park Coimbatore",
        message="guest needs help", phase="pre_booking",
    )
    assert payload["organisation_id"] == "6300"


def test_full_map_two_cities_in_text_blocked(monkeypatch):
    # Two genuinely different properties named -> ambiguous -> blocked (no leak).
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_FULL_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="s", hotel_code="zone",
        pending_data={"_integration": {"organisation_id": "5703", "phase": "pre_booking"}},
    )
    payload = service.build_lumira_ticket_payload(
        context, issue="comparing Kolkata and Jaipur properties",
        message="which is better", phase="pre_booking",
    )
    assert payload["organisation_id"] == "5703"
    assert payload.get("_force_local_only") is True


def test_full_map_parra_goa_routes_to_its_own_entity(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", _ZONE_FULL_MAP, raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="s", hotel_code="zone",
        pending_data={
            "_integration": {"organisation_id": "5703", "phase": "pre_booking"},
            "_selected_property_scope_name": "Zone Connect Park Parra Goa",
        },
    )
    payload = service.build_lumira_ticket_payload(context, issue="x", message="y", phase="pre_booking")
    assert payload["organisation_id"] == "6307"
    assert payload.get("_force_local_only") is None


def test_no_map_configured_keeps_fallback(monkeypatch):
    monkeypatch.setattr(settings, "ticketing_property_entity_map_json", "", raising=False)
    service = TicketingService()
    context = ConversationContext(
        session_id="sess-zone",
        hotel_code="zone",
        pending_data={
            "_integration": {"organisation_id": "5703", "phase": "pre_booking"},
            "_selected_property_scope_name": "Zone Park Kolkata",
        },
    )
    payload = service.build_lumira_ticket_payload(
        context, issue="x", message="y", phase="pre_booking"
    )
    assert payload["organisation_id"] == "5703"
