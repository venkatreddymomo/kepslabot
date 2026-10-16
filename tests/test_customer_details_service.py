"""Unit tests for CustomerDetailsService param mapping (no DB)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.customer_details_service import CustomerDetailsService


def test_param_mapping_full():
    svc = CustomerDetailsService()
    p = svc.build_params(
        session_id="sess-1",
        guest_name="Sana Shaaista",
        email="sana@example.com",
        phone="+91 8978967342",
        city="Goa",
        property_name="Zone Park Kolkata",
        topic="room booking request",
        conversation_id="conv-1",
        widget_id="42",
        group_id="7",
        others={"check_in": "2026-06-20", "room_type": "Deluxe"},
    )
    assert p["session_id"] == "sess-1"
    assert p["guest_name"] == "Sana Shaaista"
    assert p["first_name"] == "Sana"
    assert p["last_name"] == "Shaaista"
    assert p["email"] == "sana@example.com"
    assert p["mobile"] == "+91 8978967342"
    assert p["city"] == "Goa"
    assert p["property_name"] == "Zone Park Kolkata"
    assert p["widget_id"] == 42 and p["group_id"] == 7  # coerced to int
    assert p["form_id"] is None
    assert p["form_type"] == "ci_form"
    assert p["processing_status"] == "PROCESSED"  # no email triggered
    assert p["consent"] == 0
    assert '"check_in"' in p["others"]  # serialized json


def test_single_word_name_has_no_last_name():
    p = CustomerDetailsService().build_params(session_id="s", guest_name="Madonna")
    assert p["first_name"] == "Madonna"
    assert p["last_name"] is None


def test_blank_fields_become_null():
    p = CustomerDetailsService().build_params(session_id="s")
    assert p["guest_name"] is None
    assert p["email"] is None
    assert p["others"] is None
    assert p["form_id"] is None


def test_topic_truncated_to_500():
    p = CustomerDetailsService().build_params(session_id="s", topic="x" * 800)
    assert len(p["topic"]) == 500
