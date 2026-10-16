import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import prebooking_settings_service
from services.llm_orchestration_service import LLMOrchestrationService

T24_CATALOG = [
    {
        "id": "t24_residency_andheri_east",
        "name": "T24 Residency, Andheri East (Terminal 2)",
        "aliases": ["T24 Residency", "Residency", "T24 Residency Andheri East", "Andheri East"],
    },
    {
        "id": "t24_retro_vile_parle_east",
        "name": "T24 Retro, Vile Parle East (Terminal 1)",
        "aliases": ["T24 Retro", "Retro", "T24 Retro Vile Parle East", "Vile Parle East", "Vile Parle"],
    },
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message, expected",
    [
        ("T24 Retro, Vile Parle East", "t24_retro_vile_parle_east"),
        ("T24 Retro, Vile Parle East (Terminal 1)", "t24_retro_vile_parle_east"),
        ("residency", "t24_residency_andheri_east"),
        ("T24", ""),
        ("is Retro pet friendly?", ""),
        ("", ""),
    ],
)
async def test_a_reply_that_is_one_hotel_names_that_hotel(
    monkeypatch: pytest.MonkeyPatch, message: str, expected: str
) -> None:
    async def fake_properties(*, hotel_code):
        assert hotel_code == "t24"
        return T24_CATALOG

    monkeypatch.setattr("services.kb_table_reader.get_property_catalog_db", fake_properties)

    picked = await LLMOrchestrationService._property_named_by_message(hotel_code="t24", message=message)

    assert picked == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("hotel_code, admin", [("t24", True), ("zone", False)])
async def test_scope_judge_sees_the_hotels_own_named_places(
    monkeypatch: pytest.MonkeyPatch, hotel_code: str, admin: bool
) -> None:
    service = LLMOrchestrationService()
    facts = "- t24_residency_andheri_east (T24 Residency, Andheri East): the Workhard meeting room; Toast Bistro & Bar."
    captured: dict = {}

    async def fake_db_prompt_text(key: str) -> str:
        assert key == "router.hotel_facts"
        return facts

    async def fake_own_names(_hotel_code: str) -> list[str]:
        return ["T24 Residency, Andheri East (Terminal 2)"]

    async def fake_chat_with_json(*, messages, **_kwargs):
        captured["system"] = messages[0]["content"]
        captured["payload"] = json.loads(messages[1]["content"])
        return {"kind": "in_scope", "subject": "book meeting room"}

    monkeypatch.setattr(prebooking_settings_service, "is_admin_managed", lambda code: code == "t24")
    monkeypatch.setattr(service, "_db_prompt_text", fake_db_prompt_text)
    monkeypatch.setattr(service, "_own_property_names", fake_own_names)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    verdict = await service._scope_verdict(user_message="book one workhard venue", hotel_code=hotel_code)

    assert verdict == {"kind": "in_scope", "subject": "book meeting room"}
    if admin:
        assert captured["payload"]["our_named_places"] == facts
        assert "our_named_places lists the venues" in captured["system"]
    else:
        # Every other hotel's judge is unchanged.
        assert "our_named_places" not in captured["payload"]
        assert "our_named_places" not in captured["system"]
        assert captured["system"].endswith(
            "in_scope, even when the name does not sound like a hotel.\nWhen genuinely unsure, answer in_scope."
        )
