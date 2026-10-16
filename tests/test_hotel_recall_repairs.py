import sys
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.llm_orchestration_service as orchestration_module
from schemas.chat import ConversationContext
from services.llm_orchestration_service import LLMOrchestrationService


def test_compare_with_previous_hotel_request_is_detected() -> None:
    service = LLMOrchestrationService()
    assert service._is_compare_with_discussed_hotels_request("compare thisand prev one") is True
    assert service._is_compare_with_discussed_hotels_request("which is better than the earlier one") is True
    assert service._is_compare_with_discussed_hotels_request("what room types does it have?") is False


def test_first_discussed_hotel_reply_is_deterministic() -> None:
    service = LLMOrchestrationService()
    reply = service._direct_discussed_hotel_recall_reply(
        user_message="what was the 1st hotel we discussed",
        discussed_properties=[
            {"id": "zone_park_chennai_orr", "name": "Zone By The Park Chennai OMR"},
            {"id": "zone_park_jaipur", "name": "Zone By The Park Jaipur"},
        ],
    )
    assert reply == "The first hotel we discussed was **Zone By The Park Chennai OMR**."


def test_ambiguous_previous_hotel_recall_asks_for_clarification() -> None:
    service = LLMOrchestrationService()
    reply = service._direct_discussed_hotel_recall_reply(
        user_message="what about the previous hotel",
        discussed_properties=[
            {"id": "zone_park_chennai_orr", "name": "Zone By The Park Chennai OMR"},
            {"id": "zone_park_jaipur", "name": "Zone By The Park Jaipur"},
        ],
    )
    assert "Which one did you mean?" in reply
    assert "**Zone By The Park Chennai OMR**" in reply
    assert "**Zone By The Park Jaipur**" in reply


def test_reconcile_discussed_property_state_preserves_live_order_when_summary_is_stale() -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-recall-repair",
        hotel_code="ZONE",
        pending_data={
            "_active_property_ids": ["zone_connect_park_saket_delhi", "zone_park_darjeeling"],
            "_discussed_property_order": [
                "zone_park_darjeeling",
                "zone_connect_park_saket_delhi",
                "zone_connect_park_patna",
            ],
            "_past_property_context": {
                "zone_park_darjeeling": "Previously discussed: Zone Park Darjeeling",
                "zone_connect_park_saket_delhi": "Previously discussed: Zone Connect Park Saket Delhi",
            },
            "_property_name_map": {
                "zone_park_darjeeling": "Zone Park Darjeeling",
                "zone_connect_park_saket_delhi": "Zone Connect Park Saket Delhi",
                "zone_connect_park_patna": "Zone Connect Park Patna",
            },
        },
    )

    repaired_past, discussed = service._reconcile_discussed_property_state(
        context=context,
        available_properties=[
            {"id": "zone_park_darjeeling", "name": "Zone Park Darjeeling"},
            {"id": "zone_connect_park_saket_delhi", "name": "Zone Connect Park Saket Delhi"},
            {"id": "zone_connect_park_patna", "name": "Zone Connect Park Patna"},
        ],
        active_property_ids=["zone_connect_park_saket_delhi", "zone_park_darjeeling"],
        past_property_context=context.pending_data["_past_property_context"],
        memory_summary=(
            "LANGUAGE: english\n"
            "HOTELS DISCUSSED (oldest first):\n"
            "  - Zone Park Darjeeling | dates: none\n"
            "  - Zone Connect Park Saket Delhi | dates: none\n"
            "OPEN REQUESTS: none"
        ),
    )

    assert discussed == [
        {"id": "zone_park_darjeeling", "name": "Zone Park Darjeeling"},
        {"id": "zone_connect_park_saket_delhi", "name": "Zone Connect Park Saket Delhi"},
        {"id": "zone_connect_park_patna", "name": "Zone Connect Park Patna"},
    ]
    assert repaired_past == {
        "zone_connect_park_patna": "Previously discussed: Zone Connect Park Patna",
    }
    assert context.pending_data["_discussed_property_order"] == [
        "zone_park_darjeeling",
        "zone_connect_park_saket_delhi",
        "zone_connect_park_patna",
    ]
    assert context.pending_data["_past_property_context"] == repaired_past


def test_reconcile_discussed_property_state_uses_summary_only_to_backfill_missing_hotels() -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-summary-backfill",
        hotel_code="ZONE",
        pending_data={
            "_active_property_ids": ["zone_connect_park_lansdowne"],
            "_discussed_property_order": ["zone_connect_park_lansdowne"],
            "_past_property_context": {},
            "_property_name_map": {
                "zone_connect_park_mussoorie": "Zone Connect Park Mussoorie",
                "zone_connect_park_lansdowne": "Zone Connect Park Lansdowne",
            },
        },
    )

    repaired_past, discussed = service._reconcile_discussed_property_state(
        context=context,
        available_properties=[
            {"id": "zone_connect_park_mussoorie", "name": "Zone Connect Park Mussoorie"},
            {"id": "zone_connect_park_lansdowne", "name": "Zone Connect Park Lansdowne"},
        ],
        active_property_ids=["zone_connect_park_lansdowne"],
        past_property_context={},
        memory_summary=(
            "LANGUAGE: english\n"
            "HOTELS DISCUSSED (oldest first):\n"
            "  - Zone Connect Park Mussoorie | dates: none\n"
            "  - Zone Connect Park Lansdowne | dates: none\n"
            "OPEN REQUESTS: none"
        ),
    )

    assert discussed == [
        {"id": "zone_connect_park_lansdowne", "name": "Zone Connect Park Lansdowne"},
        {"id": "zone_connect_park_mussoorie", "name": "Zone Connect Park Mussoorie"},
    ]
    assert repaired_past == {
        "zone_connect_park_mussoorie": "Previously discussed: Zone Connect Park Mussoorie",
    }
    assert context.pending_data["_discussed_property_order"] == [
        "zone_connect_park_lansdowne",
        "zone_connect_park_mussoorie",
    ]


@pytest.mark.asyncio
async def test_router_payload_includes_repaired_discussed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-router-history",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": ["zone_connect_park_lansdowne"],
            "_property_name_map": {
                "zone_connect_park_mussoorie": "Zone Connect Park Mussoorie",
                "zone_connect_park_lansdowne": "Zone Connect Park Lansdowne",
            },
            "_memory": {
                "summary": (
                    "LANGUAGE: english\n"
                    "HOTELS DISCUSSED (oldest first):\n"
                    "  - Zone Connect Park Mussoorie | dates: none\n"
                    "  - Zone Connect Park Lansdowne | dates: none\n"
                    "OPEN REQUESTS: none"
                )
            },
        },
    )

    async def fake_get_property_catalog_db(*, hotel_code):
        assert hotel_code == "TEST"
        return [
            {
                "id": "zone_connect_park_mussoorie",
                "name": "Zone Connect Park Mussoorie",
                "aliases": ["Mussoorie"],
                "location": {"label": "Mussoorie", "city": "Mussoorie"},
            },
            {
                "id": "zone_connect_park_lansdowne",
                "name": "Zone Connect Park Lansdowne",
                "aliases": ["Lansdowne"],
                "location": {"label": "Lansdowne", "city": "Lansdowne"},
            },
        ]

    async def fake_get_structured_book_catalog_db(*, hotel_code, property_slugs=None, only_active=True):
        assert hotel_code == "TEST"
        return [
            {
                "id": "mussoorie_overview",
                "title": "Mussoorie Overview",
                "description": "Overview of Mussoorie.",
                "aliases": ["mussoorie"],
                "keywords": ["mussoorie"],
                "page_count": 2,
                "property_id": "zone_connect_park_mussoorie",
                "property_name": "Zone Connect Park Mussoorie",
            },
            {
                "id": "lansdowne_overview",
                "title": "Lansdowne Overview",
                "description": "Overview of Lansdowne.",
                "aliases": ["lansdowne"],
                "keywords": ["lansdowne"],
                "page_count": 2,
                "property_id": "zone_connect_park_lansdowne",
                "property_name": "Zone Connect Park Lansdowne",
            },
        ]

    async def fake_prompt_get(_key: str) -> str:
        return "Route the request."

    async def fake_chat_with_json(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert payload["active_property_ids"] == ["zone_connect_park_lansdowne"]
        assert payload["past_property_context"] == {
            "zone_connect_park_mussoorie": "Previously discussed: Zone Connect Park Mussoorie",
        }
        assert payload["discussed_properties_in_order"] == [
            {"id": "zone_connect_park_mussoorie", "name": "Zone Connect Park Mussoorie"},
            {"id": "zone_connect_park_lansdowne", "name": "Zone Connect Park Lansdowne"},
        ]
        return {
            "route": "book",
            "service_id": "",
            "selected_property_ids": ["zone_connect_park_lansdowne"],
            "book_ids": ["lansdowne_overview"],
            "clarification_question": "",
            "reason": "test",
            "confidence": 0.9,
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr("services.kb_table_reader.get_property_catalog_db", fake_get_property_catalog_db)
    monkeypatch.setattr(
        "services.kb_table_reader.get_structured_book_catalog_db",
        fake_get_structured_book_catalog_db,
    )
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    await service._resolve_main_route_with_llm(
        user_message="not this i meant the other hotel",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        context=context,
    )


@pytest.mark.asyncio
async def test_router_payload_does_not_drop_recent_hotel_when_summary_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-summary-lag",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": ["zone_connect_park_saket_delhi", "zone_park_darjeeling"],
            "_discussed_property_order": [
                "zone_park_darjeeling",
                "zone_connect_park_saket_delhi",
                "zone_connect_park_patna",
            ],
            "_past_property_context": {
                "zone_park_darjeeling": "Previously discussed: Zone Park Darjeeling",
                "zone_connect_park_saket_delhi": "Previously discussed: Zone Connect Park Saket Delhi",
            },
            "_property_name_map": {
                "zone_park_darjeeling": "Zone Park Darjeeling",
                "zone_connect_park_saket_delhi": "Zone Connect Park Saket Delhi",
                "zone_connect_park_patna": "Zone Connect Park Patna",
            },
            "_memory": {
                "summary": (
                    "LANGUAGE: english\n"
                    "HOTELS DISCUSSED (oldest first):\n"
                    "  - Zone Park Darjeeling | dates: none\n"
                    "  - Zone Connect Park Saket Delhi | dates: none\n"
                    "OPEN REQUESTS: none"
                )
            },
        },
    )

    async def fake_get_property_catalog_db(*, hotel_code):
        assert hotel_code == "TEST"
        return [
            {
                "id": "zone_park_darjeeling",
                "name": "Zone Park Darjeeling",
                "aliases": ["Darjeeling"],
                "location": {"label": "Darjeeling", "city": "Darjeeling"},
            },
            {
                "id": "zone_connect_park_saket_delhi",
                "name": "Zone Connect Park Saket Delhi",
                "aliases": ["Delhi"],
                "location": {"label": "Delhi", "city": "Delhi"},
            },
            {
                "id": "zone_connect_park_patna",
                "name": "Zone Connect Park Patna",
                "aliases": ["Patna"],
                "location": {"label": "Patna", "city": "Patna"},
            },
        ]

    async def fake_get_structured_book_catalog_db(*, hotel_code, property_slugs=None, only_active=True):
        assert hotel_code == "TEST"
        return [
            {
                "id": "darjeeling_overview",
                "title": "Darjeeling Overview",
                "description": "Overview of Darjeeling.",
                "aliases": ["darjeeling"],
                "keywords": ["darjeeling"],
                "page_count": 2,
                "property_id": "zone_park_darjeeling",
                "property_name": "Zone Park Darjeeling",
            },
            {
                "id": "delhi_overview",
                "title": "Delhi Overview",
                "description": "Overview of Delhi.",
                "aliases": ["delhi"],
                "keywords": ["delhi"],
                "page_count": 2,
                "property_id": "zone_connect_park_saket_delhi",
                "property_name": "Zone Connect Park Saket Delhi",
            },
            {
                "id": "patna_overview",
                "title": "Patna Overview",
                "description": "Overview of Patna.",
                "aliases": ["patna"],
                "keywords": ["patna"],
                "page_count": 2,
                "property_id": "zone_connect_park_patna",
                "property_name": "Zone Connect Park Patna",
            },
        ]

    async def fake_prompt_get(_key: str) -> str:
        return "Route the request."

    async def fake_chat_with_json(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert payload["active_property_ids"] == [
            "zone_connect_park_saket_delhi",
            "zone_park_darjeeling",
        ]
        assert payload["past_property_context"] == {
            "zone_connect_park_patna": "Previously discussed: Zone Connect Park Patna",
        }
        assert payload["discussed_properties_in_order"] == [
            {"id": "zone_park_darjeeling", "name": "Zone Park Darjeeling"},
            {"id": "zone_connect_park_saket_delhi", "name": "Zone Connect Park Saket Delhi"},
            {"id": "zone_connect_park_patna", "name": "Zone Connect Park Patna"},
        ]
        return {
            "route": "book",
            "service_id": "",
            "selected_property_ids": [
                "zone_connect_park_saket_delhi",
                "zone_park_darjeeling",
            ],
            "book_ids": [],
            "clarification_question": "",
            "reason": "test",
            "confidence": 0.9,
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr("services.kb_table_reader.get_property_catalog_db", fake_get_property_catalog_db)
    monkeypatch.setattr(
        "services.kb_table_reader.get_structured_book_catalog_db",
        fake_get_structured_book_catalog_db,
    )
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    await service._resolve_main_route_with_llm(
        user_message="till now which hotels we discussed",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        context=context,
    )


@pytest.mark.asyncio
async def test_router_payload_keeps_full_catalog_for_fleet_wide_pet_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-pet-friendly",
        hotel_code="TEST",
        pending_data={
            "_active_property_ids": ["orchid_hotel_manali"],
            "_property_name_map": {
                "orchid_hotel_manali": "Orchid Hotel Manali",
                "orchid_hotel_shimla": "Orchid Hotel Shimla",
                "orchid_hotel_mussoorie": "Orchid Hotel Mussoorie",
            },
        },
    )

    async def fake_get_property_catalog_db(*, hotel_code):
        assert hotel_code == "TEST"
        return [
            {
                "id": "orchid_hotel_manali",
                "name": "Orchid Hotel Manali",
                "aliases": ["Manali"],
                "location": {"label": "Manali", "city": "Manali"},
            },
            {
                "id": "orchid_hotel_shimla",
                "name": "Orchid Hotel Shimla",
                "aliases": ["Shimla"],
                "location": {"label": "Shimla", "city": "Shimla"},
            },
            {
                "id": "orchid_hotel_mussoorie",
                "name": "Orchid Hotel Mussoorie",
                "aliases": ["Mussoorie"],
                "location": {"label": "Mussoorie", "city": "Mussoorie"},
            },
        ]

    async def fake_get_structured_book_catalog_db(*, hotel_code, property_slugs=None, only_active=True):
        assert hotel_code == "TEST"
        return [
            {
                "id": "manali_pet_policy",
                "title": "Manali Pet Policy",
                "description": "Pet-friendly policy for Manali.",
                "aliases": ["pet policy"],
                "keywords": ["pet", "friendly"],
                "page_count": 2,
                "property_id": "orchid_hotel_manali",
                "property_name": "Orchid Hotel Manali",
            },
            {
                "id": "shimla_pet_policy",
                "title": "Shimla Pet Policy",
                "description": "Pet-friendly policy for Shimla.",
                "aliases": ["pet policy"],
                "keywords": ["pet", "friendly"],
                "page_count": 2,
                "property_id": "orchid_hotel_shimla",
                "property_name": "Orchid Hotel Shimla",
            },
            {
                "id": "mussoorie_pet_policy",
                "title": "Mussoorie Pet Policy",
                "description": "Pet-friendly policy for Mussoorie.",
                "aliases": ["pet policy"],
                "keywords": ["pet", "friendly"],
                "page_count": 2,
                "property_id": "orchid_hotel_mussoorie",
                "property_name": "Orchid Hotel Mussoorie",
            },
        ]

    async def fake_prompt_get(_key: str) -> str:
        return "Route the request."

    async def fake_chat_with_json(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert {row["id"] for row in payload["available_properties"]} == {
            "orchid_hotel_manali",
            "orchid_hotel_shimla",
            "orchid_hotel_mussoorie",
        }
        assert {row["property_id"] for row in payload["book_catalog"]} == {
            "orchid_hotel_manali",
            "orchid_hotel_shimla",
            "orchid_hotel_mussoorie",
        }
        return {
            "route": "book",
            "service_id": "",
            "selected_property_ids": [
                "orchid_hotel_manali",
                "orchid_hotel_shimla",
                "orchid_hotel_mussoorie",
            ],
            "book_ids": [
                "manali_pet_policy",
                "shimla_pet_policy",
                "mussoorie_pet_policy",
            ],
            "clarification_question": "",
            "reason": "fleet-wide pet-friendly question",
            "confidence": 0.9,
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr("services.kb_table_reader.get_property_catalog_db", fake_get_property_catalog_db)
    monkeypatch.setattr(
        "services.kb_table_reader.get_structured_book_catalog_db",
        fake_get_structured_book_catalog_db,
    )
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    await service._resolve_main_route_with_llm(
        user_message="which hotels of yours are pet friendly",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        context=context,
    )
