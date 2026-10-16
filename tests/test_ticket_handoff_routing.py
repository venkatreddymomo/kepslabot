import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from services.ticketing_service import TicketingResult, TicketingService


@pytest.mark.asyncio
async def test_handoff_to_agent_uses_guest_journey_endpoint_and_payload(monkeypatch):
    service = TicketingService()
    monkeypatch.setattr(settings, "agent_handoff_api_url", "https://engage.example/insert/transfer.htm", raising=False)
    monkeypatch.setattr(
        settings,
        "agent_handoff_api_url_gj",
        "https://guest.example/guestjourney/transfer",
        raising=False,
    )

    captured: dict[str, object] = {}

    async def _fake_request_json(method, url, payload, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["payload"] = payload
        captured["debug_context"] = kwargs.get("debug_context")
        return TicketingResult(success=True, status_code=200, response={"ok": True})

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    result = await service.handoff_to_agent(
        conversation_id="gj_918123884969",
        session_id="gj_918123884969",
        reason="Need front office help",
        agent_id="6424",
        channel="whatsapp",
        flow="guest_journey",
        wa_number="918123884969",
        entity_id="6311",
    )

    assert result.success is True
    assert captured["url"] == "https://guest.example/guestjourney/transfer"
    assert captured["payload"] == {
        "wa_number": "918123884969",
        "from_responder": "BOT",
        "to_responder": "AGENT",
        "to_agent_id": "6424",
        "reason": "Need front office help",
        "entity_id": "6311",
    }
    assert captured["debug_context"]["handoff_mode"] == "guest_journey"


@pytest.mark.asyncio
async def test_handoff_to_agent_keeps_engage_endpoint_and_payload(monkeypatch):
    service = TicketingService()
    monkeypatch.setattr(settings, "agent_handoff_api_url", "https://engage.example/insert/transfer.htm", raising=False)
    monkeypatch.setattr(settings, "agent_handoff_api_url_gj", "", raising=False)

    captured: dict[str, object] = {}

    async def _fake_request_json(method, url, payload, **kwargs):
        captured["url"] = url
        captured["payload"] = payload
        return TicketingResult(success=True, status_code=200, response={"ok": True})

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    result = await service.handoff_to_agent(
        conversation_id="conv-123",
        session_id="sess-123",
        reason="Need staff help",
        agent_id="77",
        widget_id="12",
        channel="web_widget",
        flow="engage",
    )

    assert result.success is True
    assert captured["url"] == "https://engage.example/insert/transfer.htm"
    assert captured["payload"] == {
        "from_responder": "BOT",
        "to_responder": "AGENT",
        "conversation_id": "conv-123",
        "session_id": "sess-123",
        "to_agent_id": "77",
        "widget_id": "12",
        "reason": "Need staff help",
    }
