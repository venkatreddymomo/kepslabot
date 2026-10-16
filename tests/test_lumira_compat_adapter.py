import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from schemas.chat import ChatResponse, ConversationState
from services import lumira_compat_adapter as adapter


def test_guest_journey_request_resolves_hotel_code_from_entity_map(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "ticketing_property_entity_map_json",
        json.dumps(
            {
                "sarovar_portico_sriperumbudur": {"sriperumbudur": "6311"},
            }
        ),
        raising=False,
    )

    request = adapter.build_guest_journey_chat_request(
        {
            "message": "hello",
            "waNumber": "+91 81238 84969",
            "entity_id": "6311",
        }
    )

    assert request.hotel_code == "sarovar_portico_sriperumbudur"
    assert request.metadata["entity_id"] == "6311"
    assert request.session_id == "gj_+91 81238 84969"


def test_guest_journey_response_includes_bridge_contract_fields() -> None:
    response = ChatResponse(
        session_id="gj_test",
        message="hello",
        display_message="hello",
        state=ConversationState.IDLE,
        metadata={
            "needs_escalation": True,
            "name_of_file": "menu.pdf",
        },
    )

    payload = adapter.build_guest_journey_response(response, source_payload={})

    assert payload["needs_escalation"] is True
    assert payload["name_of_file"] == "menu.pdf"
