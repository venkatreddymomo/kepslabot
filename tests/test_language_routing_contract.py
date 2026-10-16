import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.llm_orchestration_service as orchestration_module
from schemas.chat import ConversationContext, MessageRole
from services.llm_orchestration_service import LLMOrchestrationService


def test_current_kannada_script_overrides_stale_hindi_router_result() -> None:
    result = LLMOrchestrationService._response_language_preference(
        user_message="ನನಗೆ ಎರಡು ಟವೆಲ್‌ಗಳು ಬೇಕು",
        history_rows=[{"role": "user", "content": "मुझे मदद चाहिए"}],
        last_assistant_message="ज़रूर, मैं आपकी मदद करूँगा।",
        router_language={
            "language": "Hindi",
            "script": "Devanagari",
            "style": "native",
            "confidence": 0.94,
        },
    )

    assert result == {
        "language": "Kannada",
        "script": "Kannada",
        "style": "native",
        "confidence": 1.0,
        "source": "current_message_script",
    }


def test_router_preserves_romanized_hindi_as_latin_script() -> None:
    result = LLMOrchestrationService._response_language_preference(
        user_message="mujhe do towels chahiye",
        history_rows=[],
        router_language={
            "language": "Hindi",
            "script": "Latin",
            "style": "romanized",
            "confidence": 0.98,
        },
    )

    assert result["language"] == "Hindi"
    assert result["script"] == "Latin"
    assert result["style"] == "romanized"
    assert result["source"] == "main_router"


def test_router_repairs_other_script_for_romanized_telugu() -> None:
    result = LLMOrchestrationService._response_language_preference(
        user_message="nenu hotel ki munduga vaste early check-in cheyyavachaa?",
        history_rows=[{"role": "assistant", "content": "Haan, kar sakte hain."}],
        router_language={
            "language": "te",
            "script": "other",
            "style": "native",
            "confidence": 0.99,
        },
    )

    assert result["language"] == "Telugu"
    assert result["script"] == "Latin"
    assert result["style"] == "romanized"
    assert result["source"] == "main_router"


def test_previous_assistant_language_is_read_from_saved_turn_metadata() -> None:
    context = ConversationContext(session_id="language-history", hotel_code="TEST")
    context.add_message(
        MessageRole.ASSISTANT,
        "Avunu, cheyyavachu.",
        metadata={
            "response_language": {
                "language": "Telugu",
                "script": "Latin",
                "style": "romanized",
                "confidence": 0.99,
            }
        },
    )

    result = LLMOrchestrationService._last_assistant_response_language(context)

    assert result["language"] == "Telugu"
    assert result["script"] == "Latin"
    assert result["style"] == "romanized"


def test_full_sentence_language_switch_hides_only_latest_assistant_wording() -> None:
    history, last_assistant, suppressed = (
        LLMOrchestrationService._history_for_explicit_language_switch(
            history_rows=[
                {"role": "user", "content": "kya main jaldi check in kar sakta hoon?"},
                {"role": "assistant", "content": "Haan, kar sakte hain."},
                {"role": "user", "content": "nenu hotel ki munduga vaste early check-in cheyyavachaa?"},
            ],
            last_assistant_message="Haan, kar sakte hain.",
            current_user_message="nenu hotel ki munduga vaste early check-in cheyyavachaa?",
            language_switch=True,
        )
    )

    assert suppressed is True
    assert last_assistant == ""
    assert [row["role"] for row in history] == ["user", "user"]


def test_short_translate_that_request_keeps_previous_assistant_context() -> None:
    original = [
        {"role": "user", "content": "What is check-in time?"},
        {"role": "assistant", "content": "Check-in is at 3 PM."},
        {"role": "user", "content": "say that in Kannada"},
    ]
    history, last_assistant, suppressed = (
        LLMOrchestrationService._history_for_explicit_language_switch(
            history_rows=original,
            last_assistant_message="Check-in is at 3 PM.",
            current_user_message="say that in Kannada",
            language_switch=True,
        )
    )

    assert suppressed is False
    assert last_assistant == "Check-in is at 3 PM."
    assert history == original


@pytest.mark.asyncio
async def test_pending_flow_runs_small_language_router_for_romanized_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="language-pending-flow",
        hotel_code="TEST",
        pending_action="collect_housekeeping_details",
        pending_data={"_last_service_id": "housekeeping"},
    )
    captured = {}

    async def fake_chat_with_json(*, messages, **_kwargs):
        captured["payload"] = json.loads(messages[-1]["content"])
        return {
            "language": "Hindi",
            "script": "Latin",
            "style": "romanized",
            "confidence": 0.97,
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(
        orchestration_module.settings, "chat_llm_main_router_enabled", True, raising=False
    )
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    result = await service._resolve_main_route_with_llm(
        user_message="[CONTEXT: continue active flow] mujhe do towels chahiye",
        language_user_message="mujhe do towels chahiye",
        selected_phase_id="during_stay",
        selected_phase_name="During Stay",
        services_snapshot=[
            {
                "id": "housekeeping",
                "name": "Housekeeping",
                "phase_id": "during_stay",
                "is_active": True,
            }
        ],
        context=context,
    )

    assert captured["payload"]["current_guest_message"] == "mujhe do towels chahiye"
    assert result["route"] == "service"
    assert result["service_id"] == "housekeeping"
    assert result["response_language"]["language"] == "Hindi"
    assert result["response_language"]["script"] == "Latin"
    assert result["response_language"]["style"] == "romanized"


@pytest.mark.asyncio
async def test_main_router_emits_and_returns_turn_language_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(session_id="language-main-router", hotel_code="TEST")
    captured = {}

    async def fake_properties(*, hotel_code):
        assert hotel_code == "TEST"
        return []

    async def fake_books(*, hotel_code, property_slugs=None, only_active=True):
        assert hotel_code == "TEST"
        return []

    async def fake_prompt_get(_key: str, **_kwargs):
        return "Route the request and return JSON."

    async def fake_chat_with_json(*, messages, **_kwargs):
        captured["messages"] = messages
        payload = json.loads(messages[1]["content"])
        assert payload["language_user_message"] == "mujhe paani chahiye"
        assert "response_language" in payload["response_contract"]
        return {
            "route": "service",
            "service_id": "housekeeping",
            "selected_property_ids": [],
            "book_ids": [],
            "clarification_question": "",
            "reason": "Guest requests an item.",
            "confidence": 0.93,
            "response_language": {
                "language": "Hindi",
                "script": "Latin",
                "style": "romanized",
                "confidence": 0.98,
            },
        }

    monkeypatch.setattr(orchestration_module.settings, "openai_api_key", "test-key", raising=False)
    monkeypatch.setattr(
        orchestration_module.settings, "chat_llm_main_router_enabled", True, raising=False
    )
    monkeypatch.setattr(
        "services.kb_table_reader.get_property_catalog_db", fake_properties
    )
    monkeypatch.setattr(
        "services.kb_table_reader.get_structured_book_catalog_db", fake_books
    )
    monkeypatch.setattr(orchestration_module.prompt_registry, "get", fake_prompt_get)
    monkeypatch.setattr(service, "_chat_with_json", fake_chat_with_json)

    result = await service._resolve_main_route_with_llm(
        user_message="[CONTEXT: internal English note] mujhe paani chahiye",
        language_user_message="mujhe paani chahiye",
        selected_phase_id="during_stay",
        selected_phase_name="During Stay",
        services_snapshot=[
            {
                "id": "housekeeping",
                "name": "Housekeeping",
                "phase_id": "during_stay",
                "is_active": True,
            }
        ],
        context=context,
    )

    assert "FINAL LANGUAGE CLASSIFICATION" in captured["messages"][-1]["content"]
    assert result["response_language"]["language"] == "Hindi"
    assert result["response_language"]["script"] == "Latin"
    assert result["response_language"]["style"] == "romanized"


def test_answer_directive_treats_router_contract_as_authoritative() -> None:
    directive = LLMOrchestrationService._reply_language_directive()

    assert "response_language.language" in directive
    assert "AUTHORITATIVE" in directive
    assert "response_language.style" in directive
    assert "NEVER a language/style example" in directive
    assert "payload `language_switch` is true" in directive
    # The language directive must NOT dictate presentation. A block here once
    # told every model to use "'- ' bullets"; it was ungated, so it reached every
    # hotel and flipped Sarovar and Zone off the bullet style their own prompts
    # ask for (Sarovar Sep 04: 33 dots / 0 hyphens -> Sep 07: 16 / 56). Styling
    # belongs to each hotel's own prompt, and this directive sits LAST, so
    # anything it says about looks beats them on recency.
    assert "FINAL RESPONSE PRESENTATION" not in directive
    assert "'- ' bullets" not in directive
    # It may still SAY "every bullet" — that is about writing each bullet in the
    # guest's language, which is this directive's job. What it must never do is
    # name the character.
    for style_token in ("'- '", "'* '", "'• '"):
        assert style_token not in directive


def test_chip_language_directive_does_not_add_reply_formatting() -> None:
    directive = LLMOrchestrationService._reply_language_directive(for_chips=True)

    assert "response_language.language" in directive
    assert "FINAL RESPONSE PRESENTATION" not in directive
