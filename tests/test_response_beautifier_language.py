from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.response_beautifier_service as beautifier_module
from services.response_beautifier_service import ResponseBeautifierService


def _enable_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beautifier_module.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(
        beautifier_module.settings,
        "chat_display_beautifier_enabled",
        True,
    )
    monkeypatch.setattr(
        beautifier_module.settings,
        "chat_display_beautifier_use_llm",
        True,
    )
    monkeypatch.setattr(
        beautifier_module.settings,
        "chat_display_beautifier_model",
        "gpt-4.1-mini-test",
    )


@pytest.mark.asyncio
async def test_language_contract_translates_even_structured_bold_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)
    calls: list[dict] = []

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        return "Early check-in availability meeda depend avutundi.\n- Standard check-in time 3 PM."

    monkeypatch.setattr(beautifier_module.llm_client, "chat", fake_chat)
    service = ResponseBeautifierService()
    draft = "**Early check-in**\n- Haan, availability par depend karta hai.\n- Standard check-in time 3 PM hai."

    result, metadata = await service.beautify_display_text(
        draft,
        metadata={
            "response_source": "llm_orchestration",
            "response_language": {
                "language": "Telugu",
                "script": "Latin",
                "style": "romanized",
            },
            "language_reference_text": "nenu hotel ki munduga vaste early check-in cheyyavachaa?",
            "language_switch": True,
        },
    )

    assert result.startswith("Early check-in availability")
    assert metadata["display_beautifier_language_enforced"] is True
    assert metadata["display_beautifier_applied"] is True
    assert metadata["display_beautifier_fallback_reason"] == ""
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-4.1-mini-test"
    assert calls[0]["temperature"] == 0.0
    combined_prompt = "\n".join(
        str(message["content"]) for message in calls[0]["messages"]
    )
    assert "REQUIRED OUTPUT LANGUAGE: Telugu" in combined_prompt
    # The script line now says outright that it is a writing system and not the
    # target language: given a bare "REQUIRED SCRIPT: Latin" the model returned
    # a reply written in the Latin LANGUAGE (seen live 2026-09-28). The
    # assertion still checks the script reaches the prompt, just not the exact
    # old phrasing.
    assert "REQUIRED SCRIPT" in combined_prompt
    assert "NEVER the target language" in combined_prompt
    assert "Latin" in combined_prompt
    assert "Language switch: True" in combined_prompt
    assert "nenu hotel ki munduga" in combined_prompt


def test_translated_refusal_keeps_its_negation() -> None:
    service = ResponseBeautifierService()
    passes, reason = service._rewrite_guardrails_pass(
        base_text="Nahin, is samay early check-in uplabdh nahin hai.",
        candidate_text="Ledu, ee samayamlo early check-in andubatulo ledu.",
        allow_language_rewrite=True,
        target_script="Latin",
    )

    assert passes is True
    assert reason == ""


def test_spelled_out_count_is_not_a_dropped_number() -> None:
    """"1 din pehle" -> "oka roju mundu" is the translation working.

    Counting the vanished digit as data loss made the guardrail throw the whole
    translated reply away and ship the untranslated draft, which is how a guest
    who asked in Telugu was answered in Hindi.
    """
    service = ResponseBeautifierService()
    passes, reason = service._rewrite_guardrails_pass(
        base_text="Ise 1 din pehle book karna hota hai.",
        candidate_text="Deenini oka roju mundu book cheyyali.",
        allow_language_rewrite=True,
        target_script="Latin",
    )

    assert passes is True
    assert reason == ""


def test_clock_time_still_cannot_be_dropped_in_translation() -> None:
    service = ResponseBeautifierService()
    passes, reason = service._rewrite_guardrails_pass(
        base_text="Standard check-in is at 3 PM.",
        candidate_text="Standard check-in madhyahnam tarvata untundi.",
        allow_language_rewrite=True,
        target_script="Latin",
    )

    assert passes is False
    assert reason == "numeric_details_dropped"


def test_control_marker_must_survive_language_rewrite() -> None:
    service = ResponseBeautifierService()
    passes, reason = service._rewrite_guardrails_pass(
        base_text="Dayavittu vivaragalannu kodi. <<SHOW_BOOKING_BUTTON: room>>",
        candidate_text="Dayavittu vivaragalannu kodi.",
        allow_language_rewrite=True,
        target_script="Latin",
    )

    assert passes is False
    assert reason == "control_markers_changed"


@pytest.mark.asyncio
async def test_without_language_contract_keeps_existing_short_reply_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)

    async def unexpected_chat(**_kwargs):
        raise AssertionError("LLM should not be called")

    monkeypatch.setattr(beautifier_module.llm_client, "chat", unexpected_chat)
    service = ResponseBeautifierService()

    result, metadata = await service.beautify_display_text("You're welcome!")

    assert result == "You're welcome!"
    assert metadata["display_beautifier_language_enforced"] is False
    assert metadata["display_beautifier_llm_used"] is False


@pytest.mark.asyncio
async def test_language_translation_dropping_number_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)

    async def fake_chat(**_kwargs):
        return "Early check-in availability meeda depend avutundi."

    monkeypatch.setattr(beautifier_module.llm_client, "chat", fake_chat)
    service = ResponseBeautifierService()
    draft = "Early check-in depends on availability. Standard check-in is at 3 PM."

    result, metadata = await service.beautify_display_text(
        draft,
        metadata={
            "response_language": {
                "language": "Telugu",
                "script": "Latin",
                "style": "romanized",
            }
        },
    )

    assert result == draft
    assert metadata["display_beautifier_fallback_reason"] == "numeric_details_dropped"


@pytest.mark.asyncio
async def test_latin_contract_rejects_native_script_leakage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)

    async def fake_chat(**_kwargs):
        return "Early check-in ముందుగా availability meeda depend avutundi. Standard check-in 3 PM."

    monkeypatch.setattr(beautifier_module.llm_client, "chat", fake_chat)
    service = ResponseBeautifierService()
    draft = "Early check-in depends on availability. Standard check-in is at 3 PM."

    result, metadata = await service.beautify_display_text(
        draft,
        metadata={
            "response_language": {
                "language": "Telugu",
                "script": "Latin",
                "style": "romanized",
            }
        },
    )

    assert result == draft
    assert metadata["display_beautifier_fallback_reason"] == "wrong_script"
