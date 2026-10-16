import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.llm_orchestration_service as orchestration_module
from api.routes import chat as chat_routes
from schemas.chat import ChatRequest, ConversationContext
from schemas.orchestration import OrchestrationDecision
from services import conversation_cache_service
from services.config_service import config_service
from services.llm_orchestration_service import LLMOrchestrationService
from services.suggestion_prefetch_service import PrefetchBatch, SuggestionPrefetchService


@pytest.mark.asyncio
async def test_main_router_returns_selected_property_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-main-router-properties",
        hotel_code="TEST",
    )

    async def fake_get_property_catalog_db(*, hotel_code):
        assert hotel_code == "TEST"
        return [
            {
                "id": "shimla_property",
                "name": "Shimla Property",
                "aliases": ["shimla hotel"],
                "location": {"label": "Shimla", "city": "Shimla"},
            },
            {
                "id": "manali_property",
                "name": "Manali Property",
                "aliases": ["manali hotel"],
                "location": {"label": "Manali", "city": "Manali"},
            },
        ]

    async def fake_get_structured_book_catalog_db(*, hotel_code, property_slugs=None, only_active=True):
        assert hotel_code == "TEST"
        return [
            {
                "id": "shimla_overview",
                "title": "Shimla Overview",
                "description": "Overview of Shimla property.",
                "aliases": ["shimla hotel"],
                "keywords": ["shimla", "mountains"],
                "page_count": 5,
                "property_id": "shimla_property",
                "property_name": "Shimla Property",
            },
            {
                "id": "manali_overview",
                "title": "Manali Overview",
                "description": "Overview of Manali property.",
                "aliases": ["manali hotel"],
                "keywords": ["manali", "snow"],
                "page_count": 5,
                "property_id": "manali_property",
                "property_name": "Manali Property",
            },
        ]

    async def fake_prompt_get(_key: str) -> str:
        return "Route the request."

    async def fake_chat_with_json(**_kwargs):
        return {
            "route": "book",
            "service_id": "",
            "selected_property_ids": ["shimla_property", "manali_property"],
            "book_ids": ["shimla_overview", "manali_overview"],
            "clarification_question": "",
            "reason": "Guest asked for both properties.",
            "confidence": 0.91,
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(
        "services.kb_table_reader.get_property_catalog_db",
        fake_get_property_catalog_db,
    )
    monkeypatch.setattr(
        "services.kb_table_reader.get_structured_book_catalog_db",
        fake_get_structured_book_catalog_db,
    )
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    routed = await service._resolve_main_route_with_llm(
        user_message="Tell me about Shimla and Manali.",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        context=context,
    )

    assert routed["route"] == "book"
    assert routed["selected_property_ids"] == ["shimla_property", "manali_property"]
    assert routed["book_ids"] == ["shimla_overview", "manali_overview"]


@pytest.mark.asyncio
async def test_main_router_expands_location_to_all_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-location-expansion",
        hotel_code="TEST",
        pending_data={},
    )

    async def fake_get_property_catalog_db(*, hotel_code):
        assert hotel_code == "TEST"
        return [
            {
                "id": "orchid_hotel_pune",
                "name": "Orchid Hotel Pune",
                "aliases": ["Orchid Pune"],
                "location": {"label": "Pune", "city": "Pune"},
            },
            {
                "id": "fort_jadhavgadh",
                "name": "Fort Jadhavgadh",
                "aliases": ["Jadhavgadh Pune"],
                "location": {"label": "Pune", "city": "Pune"},
            },
            {
                "id": "toyam_wellness_retreat_center_pune",
                "name": "Toyam Wellness Retreat Center Pune",
                "aliases": ["Toyam Pune"],
                "location": {"label": "Pune", "city": "Pune"},
            },
        ]

    async def fake_get_structured_book_catalog_db(*, hotel_code, property_slugs=None, only_active=True):
        assert hotel_code == "TEST"
        return [
            {
                "id": "pune_overview_1",
                "title": "Orchid Pune Overview",
                "description": "Overview of Orchid Hotel Pune.",
                "aliases": ["Pune stay"],
                "keywords": ["pune", "stay"],
                "page_count": 5,
                "property_id": "orchid_hotel_pune",
                "property_name": "Orchid Hotel Pune",
            },
            {
                "id": "pune_overview_2",
                "title": "Fort Jadhavgadh Overview",
                "description": "Overview of Fort Jadhavgadh.",
                "aliases": ["Pune fort"],
                "keywords": ["pune", "fort"],
                "page_count": 5,
                "property_id": "fort_jadhavgadh",
                "property_name": "Fort Jadhavgadh",
            },
            {
                "id": "pune_overview_3",
                "title": "Toyam Pune Overview",
                "description": "Overview of Toyam Pune.",
                "aliases": ["Pune retreat"],
                "keywords": ["pune", "retreat"],
                "page_count": 5,
                "property_id": "toyam_wellness_retreat_center_pune",
                "property_name": "Toyam Wellness Retreat Center Pune",
            },
        ]

    async def fake_prompt_get(_key: str) -> str:
        return "Route the request."

    async def fake_chat_with_json(**_kwargs):
        return {
            "route": "book",
            "service_id": "",
            "selected_property_ids": [
                "orchid_hotel_pune",
                "fort_jadhavgadh",
                "toyam_wellness_retreat_center_pune",
            ],
            "book_ids": ["pune_overview_1", "pune_overview_2", "pune_overview_3"],
            "clarification_question": "",
            "reason": "Guest mentioned Pune.",
            "confidence": 0.92,
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(
        "services.kb_table_reader.get_property_catalog_db",
        fake_get_property_catalog_db,
    )
    monkeypatch.setattr(
        "services.kb_table_reader.get_structured_book_catalog_db",
        fake_get_structured_book_catalog_db,
    )
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    routed = await service._resolve_main_route_with_llm(
        user_message="show me options in pune",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        context=context,
    )

    assert routed["selected_property_ids"] == [
        "orchid_hotel_pune",
        "fort_jadhavgadh",
        "toyam_wellness_retreat_center_pune",
    ]


@pytest.mark.asyncio
async def test_main_router_does_not_override_llm_property_scope_from_location_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-location-llm-owned-scope",
        hotel_code="TEST",
        pending_data={},
    )

    async def fake_get_property_catalog_db(*, hotel_code):
        assert hotel_code == "TEST"
        return [
            {
                "id": "orchid_hotel_pune",
                "name": "Orchid Hotel Pune",
                "aliases": ["Orchid Pune"],
                "location": {"label": "Pune", "city": "Pune"},
            },
            {
                "id": "fort_jadhavgadh",
                "name": "Fort Jadhavgadh",
                "aliases": ["Jadhavgadh Pune"],
                "location": {"label": "Pune", "city": "Pune"},
            },
            {
                "id": "toyam_wellness_retreat_center_pune",
                "name": "Toyam Wellness Retreat Center Pune",
                "aliases": ["Toyam Pune"],
                "location": {"label": "Pune", "city": "Pune"},
            },
        ]

    async def fake_get_structured_book_catalog_db(*, hotel_code, property_slugs=None, only_active=True):
        assert hotel_code == "TEST"
        return [
            {
                "id": "pune_overview_1",
                "title": "Orchid Pune Overview",
                "description": "Overview of Orchid Hotel Pune.",
                "aliases": ["Pune stay"],
                "keywords": ["pune", "stay"],
                "page_count": 5,
                "property_id": "orchid_hotel_pune",
                "property_name": "Orchid Hotel Pune",
            },
            {
                "id": "pune_overview_2",
                "title": "Fort Jadhavgadh Overview",
                "description": "Overview of Fort Jadhavgadh.",
                "aliases": ["Pune fort"],
                "keywords": ["pune", "fort"],
                "page_count": 5,
                "property_id": "fort_jadhavgadh",
                "property_name": "Fort Jadhavgadh",
            },
            {
                "id": "pune_overview_3",
                "title": "Toyam Pune Overview",
                "description": "Overview of Toyam Pune.",
                "aliases": ["Pune retreat"],
                "keywords": ["pune", "retreat"],
                "page_count": 5,
                "property_id": "toyam_wellness_retreat_center_pune",
                "property_name": "Toyam Wellness Retreat Center Pune",
            },
        ]

    async def fake_prompt_get(_key: str) -> str:
        return "Route the request."

    async def fake_chat_with_json(**_kwargs):
        return {
            "route": "book",
            "service_id": "",
            "selected_property_ids": ["orchid_hotel_pune"],
            "book_ids": ["pune_overview_1"],
            "clarification_question": "",
            "reason": "LLM chose to stay narrowed on one Pune property.",
            "confidence": 0.92,
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(
        "services.kb_table_reader.get_property_catalog_db",
        fake_get_property_catalog_db,
    )
    monkeypatch.setattr(
        "services.kb_table_reader.get_structured_book_catalog_db",
        fake_get_structured_book_catalog_db,
    )
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    routed = await service._resolve_main_route_with_llm(
        user_message="show me options in pune",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        context=context,
    )

    assert routed["selected_property_ids"] == ["orchid_hotel_pune"]


@pytest.mark.asyncio
async def test_main_router_uses_active_property_books_but_keeps_location_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-active-scope-router",
        hotel_code="TEST",
        pending_data={"_active_property_ids": ["orchid_mumbai", "ira_mumbai"]},
    )
    captured: dict[str, object] = {}

    async def fake_get_property_catalog_db(*, hotel_code):
        assert hotel_code == "TEST"
        return [
            {
                "id": "orchid_mumbai",
                "name": "Orchid Hotel Mumbai",
                "aliases": ["Mumbai", "Orchid Mumbai"],
                "location": {"label": "Mumbai", "city": "Mumbai"},
            },
            {
                "id": "ira_mumbai",
                "name": "IRA by Orchid Mumbai",
                "aliases": ["Mumbai", "IRA Mumbai"],
                "location": {"label": "Mumbai", "city": "Mumbai"},
            },
            {
                "id": "orchid_shimla",
                "name": "Orchid Hotel Shimla",
                "aliases": ["Shimla"],
                "location": {"label": "Shimla", "city": "Shimla"},
            },
        ]

    async def fake_get_structured_book_catalog_db(*, hotel_code, property_slugs=None, only_active=True):
        assert hotel_code == "TEST"
        captured["property_slugs"] = list(property_slugs) if property_slugs else None
        rows = [
            {
                "id": "orchid_mumbai_rooms",
                "title": "Orchid Mumbai Rooms",
                "description": "Room categories for Orchid Hotel Mumbai.",
                "aliases": ["mumbai rooms"],
                "keywords": ["room", "suite"],
                "page_count": 4,
                "property_id": "orchid_mumbai",
                "property_name": "Orchid Hotel Mumbai",
            },
            {
                "id": "ira_mumbai_rooms",
                "title": "IRA Mumbai Rooms",
                "description": "Room categories for IRA by Orchid Mumbai.",
                "aliases": ["ira rooms"],
                "keywords": ["room", "suite"],
                "page_count": 4,
                "property_id": "ira_mumbai",
                "property_name": "IRA by Orchid Mumbai",
            },
            {
                "id": "shimla_rooms",
                "title": "Shimla Rooms",
                "description": "Room categories for Shimla.",
                "aliases": ["shimla rooms"],
                "keywords": ["room", "suite"],
                "page_count": 4,
                "property_id": "orchid_shimla",
                "property_name": "Orchid Hotel Shimla",
            },
        ]
        if property_slugs:
            return [row for row in rows if row["property_id"] in set(property_slugs)]
        return rows

    async def fake_prompt_get(_key: str) -> str:
        return "Route the request."

    async def fake_chat_with_json(**kwargs):
        captured["payload"] = json.loads(kwargs["messages"][1]["content"])
        return {
            "route": "book",
            "service_id": "",
            "selected_property_ids": ["orchid_mumbai", "ira_mumbai"],
            "book_ids": ["orchid_mumbai_rooms", "ira_mumbai_rooms"],
            "clarification_question": "",
            "reason": "Stay within the active Mumbai scope.",
            "confidence": 0.93,
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(
        "services.kb_table_reader.get_property_catalog_db",
        fake_get_property_catalog_db,
    )
    monkeypatch.setattr(
        "services.kb_table_reader.get_structured_book_catalog_db",
        fake_get_structured_book_catalog_db,
    )
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    routed = await service._resolve_main_route_with_llm(
        user_message="What room categories do you offer?",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        context=context,
    )

    # The router sees the full book catalog so it can answer fleet-wide or
    # cross-property questions without a second fetch; property scope is carried
    # by selected_property_ids, not by pre-scoping the catalog query.
    assert captured["property_slugs"] is None
    payload = captured["payload"]
    assert [row["id"] for row in payload["book_catalog"]] == [
        "orchid_mumbai_rooms",
        "ira_mumbai_rooms",
        "shimla_rooms",
    ]
    assert any(
        row["label"] == "Mumbai" and row["property_ids"] == ["orchid_mumbai", "ira_mumbai"]
        for row in payload["available_locations"]
    )
    assert any(
        row["label"] == "Shimla" and row["property_ids"] == ["orchid_shimla"]
        for row in payload["available_locations"]
    )
    assert routed["selected_property_ids"] == ["orchid_mumbai", "ira_mumbai"]


@pytest.mark.asyncio
async def test_service_continuity_is_disabled_by_default_for_book_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-service-continuity-override",
        hotel_code="TEST",
        pending_data={"_last_service_id": "room_booking"},
    )

    route, hint = await service._maybe_override_main_route_with_service_continuity(
        user_message="Tell me more about the suite options.",
        context=context,
        history_bundle={
            "last_assistant_message": "Here are the available room types for your stay.",
        },
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        main_router_route="book",
        main_router_hint={"route": "book", "confidence": 0.61},
    )

    assert route == "book"
    assert hint == {"route": "book", "confidence": 0.61}


@pytest.mark.asyncio
async def test_service_continuity_can_override_book_route_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-service-continuity-override-enabled",
        hotel_code="TEST",
        pending_data={"_last_service_id": "room_booking"},
    )

    async def fake_continuation(**_kwargs):
        return True, "Guest is still discussing the same room-booking flow."

    monkeypatch.setattr(
        orchestration_module.settings,
        "chat_llm_service_continuity_router_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "_resolve_last_service_continuation_with_llm",
        fake_continuation,
    )

    route, hint = await service._maybe_override_main_route_with_service_continuity(
        user_message="Tell me more about the suite options.",
        context=context,
        history_bundle={
            "last_assistant_message": "Here are the available room types for your stay.",
        },
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        main_router_route="book",
        main_router_hint={"route": "book", "confidence": 0.61},
    )

    assert route == "service"
    assert hint["service_id"] == "room_booking"
    assert hint["service_continuity_override"] is True


@pytest.mark.asyncio
async def test_main_router_skips_full_book_catalog_without_active_scope_for_multi_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-router-no-active-scope",
        hotel_code="TEST",
        pending_data={},
    )
    captured: dict[str, object] = {}

    async def fake_get_property_catalog_db(*, hotel_code):
        assert hotel_code == "TEST"
        return [
            {
                "id": "orchid_goa",
                "name": "Orchid Goa",
                "aliases": ["Goa"],
                "location": {"label": "Goa"},
            },
            {
                "id": "orchid_pune",
                "name": "Orchid Pune",
                "aliases": ["Pune"],
                "location": {"label": "Pune"},
            },
        ]

    async def fake_get_structured_book_catalog_db(**_kwargs):
        return [
            {
                "id": "goa_overview",
                "title": "Goa Overview",
                "description": "Overview of Orchid Goa.",
                "aliases": ["Goa"],
                "keywords": ["goa"],
                "page_count": 2,
                "property_id": "orchid_goa",
                "property_name": "Orchid Goa",
            },
            {
                "id": "pune_overview",
                "title": "Pune Overview",
                "description": "Overview of Orchid Pune.",
                "aliases": ["Pune"],
                "keywords": ["pune"],
                "page_count": 2,
                "property_id": "orchid_pune",
                "property_name": "Orchid Pune",
            },
        ]

    async def fake_prompt_get(*_args, **_kwargs):
        return "Return STRICT JSON only."

    async def fake_chat_with_json(*, messages, **_kwargs):
        _user_msg = next(m for m in reversed(messages) if m.get("role") == "user")
        payload = json.loads(_user_msg["content"])
        captured["payload"] = payload
        return {
            "route": "book",
            "service_id": "",
            "selected_property_ids": ["orchid_goa", "orchid_pune"],
            "book_ids": [],
            "clarification_question": "",
            "reason": "Location-level info request.",
            "confidence": 0.82,
        }

    monkeypatch.setattr(
        "services.kb_table_reader.get_property_catalog_db",
        fake_get_property_catalog_db,
    )
    monkeypatch.setattr(
        "services.kb_table_reader.get_structured_book_catalog_db",
        fake_get_structured_book_catalog_db,
    )
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    routed = await service._resolve_main_route_with_llm(
        user_message="tell me about goa and pune",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
        services_snapshot=[],
        context=context,
    )

    payload = captured["payload"]
    assert [row["id"] for row in payload["book_catalog"]] == [
        "goa_overview",
        "pune_overview",
    ]
    assert [row["id"] for row in payload["available_properties"]] == [
        "orchid_goa",
        "orchid_pune",
    ]
    assert routed["selected_property_ids"] == ["orchid_goa", "orchid_pune"]


def test_session_cache_ttl_uses_session_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestration_module.settings, "session_ttl_hours", 2, raising=False)

    ttl = conversation_cache_service.session_ttl_seconds(300)

    assert ttl == pytest.approx(7200.0)


def test_config_service_prefers_multi_property_welcome_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_service,
        "load_config",
        lambda: {
            "business": {
                "name": "KHIL",
                "welcome_message": "Hi! How can I help you today?",
                "property_count": 3,
                "location_count": 2,
                "multi_property_welcome_message": (
                    "Hi! Welcome to KHIL.\n"
                    "We’re present in Mumbai and Shimla.\n"
                    "Tell me which location you want, and you can choose one or more."
                ),
                "multi_property_location_manifest": [
                    {"id": "mumbai", "label": "Mumbai", "property_ids": ["orchid_mumbai", "ira_mumbai"]},
                    {"id": "shimla", "label": "Shimla", "property_ids": ["orchid_shimla"]},
                ],
            }
        },
    )

    summary = config_service.get_capability_summary("TEST")

    assert summary["resolved_welcome_message"].startswith("Hi! Welcome to KHIL.")
    assert summary["property_count"] == 3
    assert summary["location_count"] == 2


def test_config_service_rewrites_placeholder_business_name_in_welcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_service,
        "load_config",
        lambda: {
            "business": {
                "id": "KHIL",
                "code": "KHIL",
                "name": "Your Hotel Name",
                "welcome_message": "Hello! Welcome to {business_name}. How can I help you today?",
                "property_count": 23,
                "location_count": 19,
                "multi_property_welcome_message": "Welcome to Your Hotel Name! Please choose a location.",
                "multi_property_location_manifest": [
                    {"id": "pune", "label": "Pune", "property_ids": ["orchid_hotel_pune"]},
                ],
            }
        },
    )

    summary = config_service.get_capability_summary("KHIL")

    assert summary["resolved_welcome_message"].startswith("Welcome to KHIL!")
    assert "Your Hotel Name" not in summary["resolved_welcome_message"]


def test_service_kb_scope_falls_back_to_common_only() -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-service-scope",
        hotel_code="TEST",
        pending_data={"_active_property_ids": ["shimla_property"]},
    )
    service_knowledge = (
        "=== COMMON KNOWLEDGE ===\n"
        "Shared concierge guidance.\n"
        "=== PROPERTY: Mumbai Property | ALIASES: mumbai_property ===\n"
        "Mumbai-only dining details."
    )

    scoped = service._scope_service_knowledge_to_runtime_property(
        service_knowledge=service_knowledge,
        user_message="Tell me about dining.",
        context=context,
    )

    assert "Shared concierge guidance." in scoped
    assert "Mumbai-only dining details." not in scoped


def test_prefetch_runtime_context_rehydrates_private_scope() -> None:
    service = SuggestionPrefetchService()
    batch = PrefetchBatch(
        batch_id="pf_scope_rehydrate",
        session_id="sess-rehydrate",
        hotel_code="TEST",
        assistant_turn_id="turn_1",
        phase_id="pre_booking",
        conversation_state="idle",
        last_bot_message="How can I help?",
        triggering_user_message="Tell me about Shimla and Manali.",
        pending_action="",
        pending_data_public={
            "active_property_ids": ["shimla_property", "manali_property"],
            "active_property_names": ["Shimla Property", "Manali Property"],
            "selected_property_scope_id": "shimla_property",
            "selected_property_scope_ids": ["shimla_property", "manali_property"],
            "selected_property_scope_name": "Shimla Property",
            "stay_property_id": "shimla_property",
            "stay_property_name": "Shimla Property",
            "last_service_id": "room_booking",
            "service": "Room Booking",
            "selected_phase_id": "pre_booking",
        },
    )

    context = service._build_runtime_context(batch)

    assert context.pending_data["_active_property_ids"] == ["shimla_property", "manali_property"]
    assert context.pending_data["_selected_property_scope_id"] == "shimla_property"
    assert context.pending_data["_selected_property_scope_ids"] == ["shimla_property", "manali_property"]
    assert context.pending_data["_stay_property_id"] == "shimla_property"
    assert context.pending_data["_last_service_id"] == "room_booking"
    assert context.pending_data["_last_service_name"] == "Room Booking"
    assert context.pending_data["_selected_phase_id"] == "pre_booking"


@pytest.mark.asyncio
async def test_schedule_post_response_chip_job_regenerates_post_response_chips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    session_id = "sess-seeded-chip-job"
    context = ConversationContext(session_id=session_id, hotel_code="TEST")
    decision = OrchestrationDecision(
        action="respond_only",
        response_text="Here is a comparison.",
        suggested_actions=[
            "Compare Shimla and Manali",
            "Which Mumbai property is better?",
        ],
        selected_property_ids=["shimla_property", "manali_property"],
    )

    async def fake_run_next_action_suggestion_agent(**_kwargs):
        return [
            "Compare Shimla and Manali",
            "Tell me more about Shimla",
        ]

    monkeypatch.setattr(service, "_run_next_action_suggestion_agent", fake_run_next_action_suggestion_agent)

    service.schedule_post_response_chip_job(
        session_id=session_id,
        user_message="Compare them",
        context=context,
        decision=decision,
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
    )

    chips = await service.await_post_response_chips(session_id=session_id, wait_ms=20)

    assert chips == [
        "Compare Shimla and Manali",
        "Tell me more about Shimla",
    ]


@pytest.mark.asyncio
async def test_prefetch_cached_response_restores_property_scope_and_chips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "sess-prefetch-cache-scope"
    context = ConversationContext(session_id=session_id, hotel_code="TEST")

    async def fake_get_or_create_context(**_kwargs):
        return context

    async def fake_save_context(_context, db_session=None):
        return None

    monkeypatch.setattr(chat_routes.context_manager, "get_or_create_context", fake_get_or_create_context)
    monkeypatch.setattr(chat_routes.context_manager, "save_context", fake_save_context)

    async def fake_run_next_action_suggestion_agent(**_kwargs):
        return [
            "Compare Shimla and Manali",
            "Tell me more about Shimla",
        ]

    monkeypatch.setattr(
        chat_routes.llm_orchestration_service,
        "_run_next_action_suggestion_agent",
        fake_run_next_action_suggestion_agent,
    )

    cached_decision = OrchestrationDecision(
        action="respond_only",
        response_text="Here is the comparison.",
        suggested_actions=[
            "Compare Shimla and Manali",
            "Tell me more about Shimla",
        ],
        selected_property_ids=["shimla_property", "manali_property"],
        metadata={"source": "suggestion_prefetch_cache"},
    )
    cached_payload = {
        "message": "Here is the comparison.",
        "display_message": "Here is the comparison.",
        "state": "idle",
        "metadata": {
            "response_source": "suggestion_prefetch_cache",
            "pending_data_public": {
                "active_property_ids": ["shimla_property", "manali_property"],
                "active_property_names": ["Shimla Property", "Manali Property"],
                "selected_property_scope_ids": ["shimla_property", "manali_property"],
                "selected_phase_id": "pre_booking",
            },
            "selected_phase_id": "pre_booking",
            "selected_phase_name": "Pre Booking",
            "cached_decision": cached_decision.model_dump(mode="json"),
        },
    }

    chat_routes.llm_orchestration_service._cancel_post_response_chip_job(session_id)
    response = await chat_routes._build_prefetch_cached_response(
        request=ChatRequest(
            session_id=session_id,
            message="Compare both properties.",
            hotel_code="TEST",
        ),
        hotel_code="TEST",
        cached_payload=cached_payload,
        db=None,
    )
    chips = await chat_routes.llm_orchestration_service.await_post_response_chips(
        session_id=session_id,
        wait_ms=20,
    )

    assert response.suggested_actions == [
        "Compare Shimla and Manali",
        "Tell me more about Shimla",
    ]
    assert context.pending_data["_active_property_ids"] == ["shimla_property", "manali_property"]
    assert context.pending_data["_selected_property_scope_ids"] == ["shimla_property", "manali_property"]
    assert chips == [
        "Compare Shimla and Manali",
        "Tell me more about Shimla",
    ]


def test_coerce_public_pending_mirrors_property_scope() -> None:
    pending_data = {
        "_active_property_ids": ["shimla_property", "manali_property"],
        "_selected_property_scope_id": "shimla_property",
        "_selected_property_scope_ids": ["shimla_property", "manali_property"],
        "_selected_property_scope_name": "Shimla Property",
        "_stay_property_id": "shimla_property",
        "_stay_property_name": "Shimla Property",
        "_last_service_id": "room_booking",
        "_last_service_name": "Room Booking",
        "_selected_phase_id": "pre_booking",
    }

    public_pending = LLMOrchestrationService._coerce_public_pending(pending_data)

    assert public_pending["active_property_ids"] == ["shimla_property", "manali_property"]
    assert public_pending["selected_property_scope_id"] == "shimla_property"
    assert public_pending["selected_property_scope_ids"] == ["shimla_property", "manali_property"]
    assert public_pending["selected_property_scope_name"] == "Shimla Property"
    assert public_pending["stay_property_id"] == "shimla_property"
    assert public_pending["stay_property_name"] == "Shimla Property"
    assert public_pending["service_id"] == "room_booking"
    assert public_pending["service"] == "Room Booking"
    assert public_pending["selected_phase_id"] == "pre_booking"


def test_sync_property_scope_pending_uses_property_name_map_over_stale_names() -> None:
    pending_data = {
        "_property_name_map": {
            "orchid_hotel_manali": "The Orchid Hotel Manali",
            "orchid_hotel_shimla": "The Orchid Hotel Shimla",
        },
        "_active_property_names": ["Orchid Passaros Goa South Goa"],
    }

    LLMOrchestrationService._sync_property_scope_pending(
        pending_data=pending_data,
        property_ids=["orchid_hotel_manali", "orchid_hotel_shimla"],
        active_property_names=["Orchid Passaros Goa South Goa", "IRA Bhubaneswar"],
        selected_property_name="Orchid Passaros Goa South Goa",
    )

    assert pending_data["_active_property_names"] == [
        "The Orchid Hotel Manali",
        "The Orchid Hotel Shimla",
    ]
    assert pending_data["_selected_property_scope_name"] == "The Orchid Hotel Manali"
