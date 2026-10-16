import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from services.ticketing_service import TicketingService


@pytest.mark.asyncio
async def test_apply_kepsla_description_stashes_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "ticketing_kepsla_description_to_api_enabled", True, raising=False
    )
    service = TicketingService()

    async def _fake_summary(payload):
        return {
            "description": "Guest reports the AC in room 204 is not cooling.",
            "department": "Housekeeping",
        }

    monkeypatch.setattr(service, "_generate_kepsla_summary", _fake_summary)

    payload = {
        "issue": "Support Request request",
        "message": "User: my ac is broken",
        "session_id": "s1",
    }
    out = await service._apply_kepsla_description(payload)

    # The summary + department are stashed on internal keys for the outbound
    # request and the DB write.
    assert out["_kepsla_description"] == "Guest reports the AC in room 204 is not cooling."
    assert out["_kepsla_department"] == "Housekeeping"
    # The stored `issue` stays the original so the dedup fingerprint is stable
    # (the summary is applied to the OUTBOUND payload by create_ticket).
    assert out["issue"] == "Support Request request"
    assert out["message"] == "User: my ac is broken"
    # Original payload is not mutated in place.
    assert "_kepsla_description" not in payload


@pytest.mark.asyncio
async def test_apply_kepsla_description_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "ticketing_kepsla_description_to_api_enabled", False, raising=False
    )
    service = TicketingService()

    async def _boom(payload):
        raise AssertionError("summary should not be generated when disabled")

    monkeypatch.setattr(service, "_generate_kepsla_summary", _boom)

    payload = {"issue": "Support Request request", "message": "x"}
    out = await service._apply_kepsla_description(payload)
    assert out["issue"] == "Support Request request"
    assert "_kepsla_description" not in out


@pytest.mark.asyncio
async def test_apply_kepsla_description_falls_back_when_no_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "ticketing_kepsla_description_to_api_enabled", True, raising=False
    )
    service = TicketingService()

    async def _none(payload):
        return None

    monkeypatch.setattr(service, "_generate_kepsla_summary", _none)

    payload = {"issue": "AC not working in room 204", "message": "x"}
    out = await service._apply_kepsla_description(payload)
    # No summary -> original issue stands, no internal keys leaked.
    assert out["issue"] == "AC not working in room 204"
    assert "_kepsla_description" not in out
