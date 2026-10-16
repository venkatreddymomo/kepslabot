from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Optional

from core.text_utils import normalize_identifier

from config.settings import settings
from llm.client import llm_client
from schemas.chat import ConversationContext
from schemas.orchestration import OrchestrationDecision, TicketDecision
from services.config_service import config_service
from services import conversation_cache_service
from services.conversation_memory_service import conversation_memory_service
from services.everything_trace_service import everything_trace_service
from services.faq_context_service import build_faq_block
from services.form_mode_service import (
    canonicalize_trigger_pending_data,
    infer_trigger_value_from_message,
    normalize_trigger_missing_fields,
    resolve_trigger_field_value,
    strip_form_confirmation_instructions,
)
from services.prompt_registry_service import (
    PromptMissingError,
    prompt_registry,
)

# ── LLM Input Logger ─────────────────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(_log_dir, exist_ok=True)
# LLM-input logger lives in services/llm_call_logging.py so the extracted
# orchestrator mixins can call _log_llm_call without importing this module.
from services.llm_call_logging import _log_llm_call

# ── Structured Decision Logger ────────────────────────────────────────────────
_decision_log_dir = os.path.join(_log_dir, "decisions")
os.makedirs(_decision_log_dir, exist_ok=True)
_decision_logger = logging.getLogger("orchestration_decisions")
if not _decision_logger.handlers:
    _dfh = logging.FileHandler(os.path.join(_decision_log_dir, "decisions.jsonl"), encoding="utf-8")
    _dfh.setFormatter(logging.Formatter("%(message)s"))
    _decision_logger.addHandler(_dfh)
    _decision_logger.setLevel(logging.DEBUG)
    _decision_logger.propagate = False

logger = logging.getLogger(__name__)


def _current_thread_messages(context: ConversationContext) -> list:
    """The current conversation's messages for Pre-booking-tab hotels (T24
    onward); every other hotel keeps reading the whole session. The dustbin
    starts a new conversation under the same session, and the router was still
    handed the previous chat's last reply as `last_assistant_message`: asked for
    the food menu in a fresh chat with no hotel chosen, it narrowed to T24 Retro
    because the old chat's last answer was about Retro."""
    messages = list(getattr(context, "messages", None) or [])
    try:
        from services import prebooking_settings_service

        if prebooking_settings_service.is_admin_managed(
            str(getattr(context, "hotel_code", "") or "").strip().lower()
        ):
            return list(context.get_recent_messages(len(messages)) or [])
    except Exception:
        pass
    return messages


def _previous_assistant_was_form_completion(context: ConversationContext) -> bool:
    """True when the most-recent assistant turn was a support-form / ticket
    completion. Used to gate the router's `broaden_properties` flag — that
    flag is only honored when a form just completed, since "another property"
    mid-conversation is too ambiguous to act on.
    """
    if not isinstance(getattr(context, "messages", None), list):
        return False
    for msg in reversed(context.messages):
        msg_role = str(getattr(getattr(msg, "role", None), "value", "") or "").strip().lower()
        if msg_role != "assistant":
            continue
        meta = getattr(msg, "metadata", None)
        if not isinstance(meta, dict):
            return False
        return str(meta.get("orchestration_action") or "").strip().lower() == "create_ticket"
    return False


def _log_decision(record: dict) -> None:
    """Append a structured decision record as one JSON line."""
    try:
        _decision_logger.debug(json.dumps(record, ensure_ascii=False, default=str))
        from services.turn_diagnostics_service import turn_diagnostics_service

        turn_diagnostics_service.log_orchestration_decision(record)
        everything_trace_service.log_event(
            "orchestration_decision",
            {"record": record},
            session_id=str((record or {}).get("session_id") or ""),
            component="services.llm_orchestration_service",
        )
        try:
            from services.flow_logger import log_book_routing_event

            routing = (record or {}).get("routing")
            if isinstance(routing, dict):
                log_book_routing_event(
                    event="orchestration_decision",
                    session_id=str((record or {}).get("session_id") or ""),
                    route=str(routing.get("main_router_route") or routing.get("final_action") or ""),
                    payload={
                        "trace_id": str((record or {}).get("trace_id") or ""),
                        "phase": (record or {}).get("phase", {}),
                        "routing": routing,
                        "intent": str((record or {}).get("intent") or ""),
                        "context_switched": bool((record or {}).get("context_switched")),
                        "response_preview": str((record or {}).get("response_text") or "")[:500],
                    },
                )
        except Exception:
            pass
    except Exception:
        pass


# _log_llm_call moved to services/llm_call_logging.py (imported above).
# ─────────────────────────────────────────────────────────────────────────────


from services.llm_orchestration_chips_mixin import LLMOrchestrationChipsMixin


from services.llm_orchestration_grounding_mixin import LLMOrchestrationGroundingMixin


from services.llm_orchestration_directives_mixin import LLMOrchestrationDirectivesMixin


class LLMOrchestrationService(LLMOrchestrationChipsMixin, LLMOrchestrationGroundingMixin, LLMOrchestrationDirectivesMixin):
    """
    Two-stage LLM runtime:
    1) global orchestrator decides service/intent/action
    2) service-level LLM generates grounded response and slot updates

    Both stages must emit strict JSON contracts.
    """

    # ── Chip speculation cache ──────────────────────────────────────────────
    # When a turn ends, we kick off background tasks to pre-generate the answer
    # for each suggestion chip we just shipped. If the guest then CLICKS one of
    # those chips on the next turn, the chip text arrives as the user_message
    # verbatim and we serve the cached answer instantly (zero LLM wait). If
    # they type something else, the cache misses and we run the normal
    # router → answerer flow. Cached entries are dropped on the next turn or
    # after `_chip_speculation_ttl_seconds`, whichever comes first, so stale
    # speculations never leak into a different conversation moment.
    #
    # Shape: {session_id: [{"chip_norm": str, "decision": OrchestrationDecision,
    #                       "ts": float}]}
    _chip_speculation_cache: dict[str, list[dict[str, Any]]] = {}
    _chip_speculation_ttl_seconds: float = 180.0
    _chip_speculation_max_per_session: int = 3
    # In-flight speculation tasks per session. Cancelled at the start of
    # every new turn so background work from N turns ago never piles up
    # against the OpenAI rate limit (which would slow the live request).
    _chip_speculation_inflight: dict[str, list[Any]] = {}
    # One post-response chip job per session. This becomes the single source
    # of truth for idle informational follow-up chips shown after the live
    # answer is already on screen.
    _post_response_chip_jobs: dict[str, dict[str, Any]] = {}
    # Chip texts shown to the user recently (per session), used to avoid
    # serving the same deterministic fallback chips turn after turn.
    _recently_shown_chips: dict[str, list[str]] = {}

    @classmethod
    def _cancel_pending_speculations(cls, session_id: str) -> int:
        """Cancel and forget all in-flight speculation tasks for a session.
        Called at the start of every turn so the next turn's background work
        doesn't compete with the previous turn's stale speculations for
        OpenAI's per-account rate limit. Returns the number cancelled.
        Safe to call when nothing is pending."""
        if not session_id:
            return 0
        tasks = cls._chip_speculation_inflight.pop(session_id, None)
        if not tasks:
            return 0
        cancelled = 0
        for t in tasks:
            try:
                if t is not None and not t.done():
                    t.cancel()
                    cancelled += 1
            except Exception:
                pass
        return cancelled


    @classmethod
    def _cancel_session_background_work(cls, session_id: str) -> int:
        """Cancel every non-critical background task associated with a session."""
        cancelled = cls._cancel_pending_speculations(session_id)
        cancelled += cls._cancel_post_response_chip_job(session_id)
        return cancelled

    @classmethod
    def _track_speculation_task(cls, session_id: str, task: Any) -> None:
        """Register a fire-and-forget speculation task so the next turn can
        cancel it if the guest moves on. Bounds total in-flight tasks per
        session to N+a-bit (max chips per session) — ample but never an
        unbounded fan-out."""
        if not session_id or task is None:
            return
        bucket = cls._chip_speculation_inflight.setdefault(session_id, [])
        bucket.append(task)
        # Drop already-finished tasks so the bucket doesn't grow forever.
        if len(bucket) > cls._chip_speculation_max_per_session * 2:
            cls._chip_speculation_inflight[session_id] = [
                t for t in bucket if t is not None and not t.done()
            ]





    async def _speculate_for_decision_universal(
        self,
        *,
        decision: Any,
        user_message: str,
        history_bundle: dict[str, Any],
        faq_candidates: list[dict[str, Any]],
        memory_snapshot: dict[str, Any],
        selected_phase_id: str,
        selected_phase_name: str,
        context: Any,
        in_phase_services: list[dict[str, Any]],
        out_of_phase_services: list[dict[str, Any]],
        book_route_context: dict[str, Any] | None,
        main_router_hint: dict[str, Any] | None,
        main_router_book_ids: list[str],
        book_route_status: str,
        orchestration_trace_id: str,
    ) -> None:
        """Route-agnostic chip speculation kickoff.

        Works for book-route turns (where book_route_context is already
        built) AND service-route turns (where it isn't — we build a
        speculation-only context on demand from the active properties so
        the cached chip answers are still KB-grounded).

        Fire-and-forget: kicked off from `asyncio.create_task` by the
        caller; failures here are silent. The live request never waits."""
        chips = [
            str(c).strip()
            for c in (getattr(decision, "suggested_actions", None) or [])
            if str(c or "").strip()
        ][: self._chip_speculation_max_per_session]
        if not chips:
            return
        session_id = str(getattr(context, "session_id", "") or "")
        if not session_id:
            return
        if not self._legacy_chip_speculation_enabled():
            return

        ctx = book_route_context if isinstance(book_route_context, dict) else {}
        book_text = str(ctx.get("context_text") or "").strip()
        spec_book_ids = list(main_router_book_ids or [])

        if not book_text:
            # Service-route or auto-route turn: no answerer KB was built.
            # Speculate-only context — load all books for active properties
            # so the speculated chip answers have full KB to ground in.
            try:
                from services.kb_table_reader import (
                    build_book_context_from_ids_db,
                    get_structured_book_catalog_db,
                )
                hotel_code = str(getattr(context, "hotel_code", "") or "").strip()
                active_props: list[str] = []
                pending = getattr(context, "pending_data", None)
                if isinstance(pending, dict):
                    active_props = [
                        str(p).strip()
                        for p in (pending.get("_active_property_ids") or [])
                        if str(p).strip()
                    ]
                if not active_props:
                    catalog = await get_structured_book_catalog_db(
                        hotel_code=hotel_code,
                        property_slugs=None,
                        only_active=True,
                    )
                    seen: set[str] = set()
                    for b in catalog or []:
                        if not isinstance(b, dict):
                            continue
                        pid = str(b.get("property_id") or "").strip()
                        if pid and pid not in seen:
                            seen.add(pid)
                            active_props.append(pid)
                if not active_props:
                    return
                scoped = await get_structured_book_catalog_db(
                    hotel_code=hotel_code,
                    property_slugs=active_props,
                    only_active=True,
                )
                spec_book_ids = [
                    str(b.get("id")).strip()
                    for b in (scoped or [])
                    if isinstance(b, dict) and str(b.get("id") or "").strip()
                ]
                if not spec_book_ids:
                    return
                result = await build_book_context_from_ids_db(
                    hotel_code=hotel_code,
                    book_ids=spec_book_ids,
                    property_slugs=active_props,
                    max_chars=80000,
                )
                ctx = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
                book_text = str(ctx.get("context_text") or "").strip()
            except Exception:
                return
        if not book_text:
            return

        spec_args = {
            "session_id": session_id,
            "user_message_just_asked": user_message,
            "answer_just_given": str(getattr(decision, "response_text", "") or ""),
            "book_context_text": book_text,
            "history_bundle": history_bundle or {},
            "faq_candidates": faq_candidates or [],
            "selected_phase_id": selected_phase_id,
            "selected_phase_name": selected_phase_name,
            "context": context,
            "memory_snapshot": memory_snapshot or {},
            "in_phase_services": in_phase_services or [],
            "out_of_phase_services": out_of_phase_services or [],
            "book_route_context": ctx,
            "main_router_hint": main_router_hint or {},
            "main_router_book_ids": spec_book_ids,
            "book_route_status": book_route_status,
            "orchestration_trace_id": orchestration_trace_id,
        }
        for chip in chips:
            self._schedule_chip_speculation(
                chip_text=chip,
                **spec_args,
            )

    # Phase 5: model context window lookup. Pure OpenAI today (per user setup).
    # Adding new models = one line. Default 128k for unknown OpenAI models.
    # NOTE: prefix match is first-wins (see _kb_model_context_window), so more
    # specific prefixes MUST come before shorter ones — "gpt-4.1" before "gpt-4",
    # else "gpt-4.1-mini" would match "gpt-4" (8k) and get a tiny budget.
    _OPENAI_CONTEXT_WINDOW_TOKENS = {
        "gpt-4o": 128_000,
        "gpt-4o-mini": 128_000,
        "gpt-4.1": 1_000_000,
        "gpt-5": 400_000,
        "gpt-4-turbo": 128_000,
        "gpt-4": 8_192,
        "gpt-3.5-turbo": 16_385,
        "gpt-3.5-turbo-16k": 16_385,
        "o1": 200_000,
        "o1-mini": 128_000,
        "o3-mini": 200_000,
    }



    @staticmethod
    def _build_guest_point_style_contract() -> str:
        """Empty by design — all formatting/style rules now live in the DB
        prompt `orchestrator.book_answerer` (single source of truth as the
        user requested). Kept as a no-op so existing call sites remain valid.
        Editing prompt behaviour means editing the DB row, not this code.
        """
        return ""














    @staticmethod
    def _session_cache_ttl_seconds(default_seconds: float | int | None = None) -> float:
        return float(conversation_cache_service.session_ttl_seconds(default_seconds))
    @staticmethod
    def _build_service_response_text_style_contract() -> str:
        """Strict formatting contract for service-agent JSON `response_text`."""
        return (
            "\n=== RESPONSE_TEXT FORMAT (MANDATORY) ===\n"
            "In your JSON output, `response_text` MUST follow this format:\n"
            "- Keep it easy to scan and naturally guest-facing.\n"
            "- No long paragraphs or dense spec dumps.\n"
            "- Simple answers may be just 1-3 short lines.\n"
            "- When multiple facts help, use 2-4 compact mini sections or concise points; do NOT force '-' bullets on every reply.\n"
            "- A short lead-in or heading is welcome when it improves readability.\n"
            "- Cover every relevant option the guest asked for, but summarize each one selectively unless they asked for exact specs.\n"
            "- For room options, lead with what makes each room appealing or who it suits; do not lead with size, bed codes, or dry field dumps unless asked.\n"
            "- Keep wording natural, concise, polished, and hospitality-like.\n"
            "- Prefer short concierge-style sections, emoji-led mini headings, or `?` points when that improves readability.\n"
            "- Avoid plain `-` dash bullets as the default visual style.\n"
            "- For hotel overviews and room answers, a strong default is: one short warm opener, one bridge line if useful, then 2-4 guest-friendly mini sections.\n"
            "- Unless the guest asked for them, do not lead with phone/email, full address, room count, square footage, or dry inventory facts.\n"
            "- If you are declining an unsupported execution request, keep the reply short and final. Do not append room options, offers, booking help, or unrelated upsell.\n"
            "\n=== EMPHASIS / BOLD INSIDE response_text (MANDATORY) ===\n"
            "Use markdown bold (`**text**`) only on the words that matter most. The chat UI renders `**...**` as bold and the asterisks disappear visually:\n"
            "- The proper noun the line/section is ABOUT - every room/service/restaurant/package/amenity name (e.g. `**LUX Suite**`, `**Airport Transfer**`).\n"
            "- A short label when it improves scanability (e.g. `**Check-in**:`).\n"
            "- Numeric facts only when that number is the actual answer.\n"
            "- The trigger field's selected option when confirming (e.g. `**Premier Twin Room**`).\n"
            "Rules:\n"
            "- Exactly two asterisks per side, no inner spaces (`**X**`).\n"
            "- Keep pairs balanced; never leave `**` unclosed.\n"
            "- Bold the noun/label/number, not whole sentences.\n"
            "- Do NOT use single-asterisk italics for emphasis.\n"
            "- Emojis are optional; use at most 1-2 when they genuinely help readability.\n"
            "- Suitable examples include ??, ???, ???, ??, ??, ?, ??, ??.\n"
            "\n=== SUGGESTED_ACTIONS FIELD (MANDATORY) ===\n"
            "Your JSON output MUST include a `suggested_actions` array with exactly 3 entries - these become the chips the guest sees right next to your reply.\n"
            "- Each entry is 2-8 words, written first-person AS IF THE GUEST WOULD TYPE IT NEXT.\n"
            "- Each must be answerable RIGHT NOW from the same KB you used.\n"
            "- Phase-aware: never propose booking/ordering/requesting any service that is not allowed in the current phase. For unavailable services, propose informational follow-ups instead (e.g. 'What does the spa offer?' instead of 'Book a spa session').\n"
            "- During slot collection, prefer chips that move the booking forward (e.g. 'Use my booked dates', 'Pay at check-in', 'Add airport pickup') - never include 'yes confirm' or generic confirmation phrases unless you are explicitly in the confirmation step.\n"
            "- No emojis, no quotes, no leading bullets/dashes.\n"
            "\n=== LANGUAGE MIRRORING (HARD RULE - APPLIES TO EVERY REPLY) ===\n"
            "- If the guest explicitly asks to use a specific language, write BOTH `response_text` AND every `suggested_actions` chip in that language and its normal script, and keep using it for short follow-ups until the guest clearly switches or asks for another language.\n"
            "- Otherwise, detect the language and script of the guest's LATEST message and mirror whatever they wrote in (English -> English, Hindi -> Hindi, Spanish -> Spanish, Japanese -> Japanese, and so on for any language).\n"
            "- Decide the language from the guest's messages, not from the guest's name, hotel city/country, the English KB, or these instructions.\n"
            "- If the guest writes in a romanized or mixed style without asking for a named language, reply in that SAME romanized style. A named-language request overrides romanized style unless they ask for romanized text.\n"
            "- This covers EVERY guest-facing word: lead-in lines, every bullet, field-collection prompts, decline/handoff lines, and the confirmation summary.\n"
            "- You still read the knowledge base and reason in English internally; only the visible reply is rendered in the guest's language. Never mention that you translated anything.\n"
            "- Translate ONLY prose. Keep EXACTLY as-is, never translated: proper nouns (room/service/restaurant/package names), prices, the confirmation phrase, all JSON keys and field ids, and any `<<...>>` markers. Markdown syntax (`-`, `**`) is unchanged.\n"
            "- Default to English only when the language is genuinely indeterminate (a bare number, emoji, or single proper noun).\n"
        )








    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        return normalize_identifier(value)


    @classmethod
    def _normalize_phase_identifier(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "_")
        aliases = {
            "prebooking": "pre_booking",
            "booking": "pre_checkin",
            "precheckin": "pre_checkin",
            "duringstay": "during_stay",
            "instay": "during_stay",
            "in_stay": "during_stay",
            "postcheckout": "post_checkout",
        }
        return aliases.get(normalized, normalized)

    @classmethod
    def _is_stay_property_pin_phase(cls, phase_id: Any) -> bool:
        return cls._normalize_phase_identifier(phase_id) in {
            "pre_checkin",
            "during_stay",
            "post_checkout",
        }

    @staticmethod
    def _model_uses_self_contained_prompts(model: str | None = None) -> bool:
        """True when the given model carries its full router/answerer scope rules
        INLINE in its DB prompt (the 5.4 variant), so the shared 4.1-style
        code-appended scope blocks must NOT be pasted on.

        Pass the ROLE's resolved model — in multi-model mode the router runs on
        4.1 while the answerer runs on 5.4, so a turn-global answer would be wrong
        for at least one of them. With no model given it falls back to the
        turn-global signal for backward compatibility. 4.1 — and any model without
        a self-contained DB prompt — keeps the legacy code blocks unchanged.
        Extend the set below when another model gets its own self-contained rows."""
        _SELF_CONTAINED_MODELS = {"gpt-5.4-mini"}
        try:
            if model is not None and str(model).strip():
                resolved = str(model).strip().lower()
            else:
                from services.model_override_service import default_model, get_model_override

                resolved = (get_model_override() or default_model() or "").strip().lower()
        except Exception:
            return False
        return resolved in _SELF_CONTAINED_MODELS

    @staticmethod
    def _role_model(role: str) -> str | None:
        """Resolve the chat model for an orchestration role (routing -> 4.1,
        answering / chips -> 5.4 by default). Returns None on failure so the LLM
        call and prompt fetch both fall back to their historical defaults."""
        try:
            from services.model_override_service import resolve_role_model

            return resolve_role_model(role) or None
        except Exception:
            return None

    @staticmethod
    def _normalize_property_id_list(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized

    @staticmethod
    def _normalize_property_name_list(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            normalized.append(text)
        return normalized



    def _apply_stay_property_pin(
        self,
        *,
        context: ConversationContext,
        selected_phase_id: str,
        property_ids: list[str],
        source: str,
    ) -> tuple[list[str], bool]:
        normalized_ids = self._normalize_property_id_list(property_ids)
        if not self._is_stay_property_pin_phase(selected_phase_id):
            return normalized_ids, False

        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        stay_property_id = str(pending.get("_stay_property_id") or "").strip()
        stay_property_name = str(
            pending.get("_stay_property_name")
            or getattr(context, "booking_property_name", "")
            or ""
        ).strip()
        if not stay_property_id:
            return normalized_ids, False
        if stay_property_id in normalized_ids:
            return normalized_ids, False
        # The guest EXPLICITLY focused a different property this turn (a vague
        # follow-up resolves to the existing/booked scope or to nothing; an explicit
        # new property/city resolves to a non-empty different id). Honor that
        # re-scope — do NOT merge the booked property back in. Merging produced a
        # 2-property scope that routed booking intents to the main answerer instead
        # of the room agent (so no ticket got created and answers fanned out across
        # both properties). Only re-pin the booked property when there is NO explicit
        # focus this turn.
        if normalized_ids:
            return normalized_ids, False

        pinned_ids = [stay_property_id, *normalized_ids]
        logger.info(
            "Stay property pin applied (session_id=%s phase=%s source=%s stay_property_id=%s incoming_ids=%s)",
            str(getattr(context, "session_id", "") or "").strip(),
            self._normalize_phase_identifier(selected_phase_id),
            str(source or "").strip(),
            stay_property_id,
            normalized_ids,
        )
        if isinstance(context.pending_data, dict):
            context.pending_data["_stay_property_name"] = stay_property_name or stay_property_id
            context.pending_data["_stay_property_pin_last_source"] = str(source or "").strip()
        return pinned_ids, True

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
        return default

    def _normalize_field_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            key = self._normalize_identifier(item)
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(key)
            if len(normalized) >= 12:
                break
        return normalized

    def _normalize_ticket_evidence(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = " ".join(str(item or "").strip().split())
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(text[:280])
            if len(normalized) >= 4:
                break
        return normalized

    @staticmethod
    def _compact_text(value: Any, *, max_chars: int = 400) -> str:
        return " ".join(str(value or "").strip().split())[:max_chars]

    @staticmethod
    def _normalize_ws_lower(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())


    @classmethod
    def _tokenize_router_text(cls, value: Any) -> set[str]:
        text = cls._normalize_scope_match_phrase(value)
        if not text:
            return set()
        return {
            token
            for token in re.findall(r"[a-z0-9]+", text)
            if len(token) > 1
        }

    @classmethod
    def _normalize_property_name_map(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            key = cls._normalize_identifier(raw_key)
            text = str(raw_value or "").strip()
            if key and text:
                normalized[key] = text
        return normalized

    @classmethod
    def _merge_property_name_map(
        cls,
        existing: Any,
        rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        merged = cls._normalize_property_name_map(existing)
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            property_id = cls._normalize_identifier(row.get("id") or row.get("property_id"))
            property_name = str(row.get("name") or row.get("property_name") or "").strip()
            if property_id and property_name:
                merged[property_id] = property_name
        return merged

    @classmethod
    def _resolve_property_name_from_pending(
        cls,
        *,
        pending_data: Any,
        property_id: Any,
        fallback: Any = "",
    ) -> str:
        normalized_id = cls._normalize_identifier(property_id)
        if not normalized_id:
            return str(fallback or "").strip()
        name_map = cls._normalize_property_name_map(
            pending_data.get("_property_name_map") if isinstance(pending_data, dict) else {}
        )
        resolved = str(name_map.get(normalized_id) or "").strip()
        return resolved or str(fallback or "").strip()

    @classmethod
    def _extract_discussed_hotel_names_from_summary(
        cls,
        summary: Any,
    ) -> list[str]:
        lines = str(summary or "").splitlines()
        in_block = False
        names: list[str] = []
        seen: set[str] = set()
        for raw_line in lines:
            line = str(raw_line or "").rstrip()
            stripped = line.strip()
            if not in_block:
                if stripped.lower().startswith("hotels discussed"):
                    in_block = True
                continue
            if not stripped:
                continue
            if not stripped.startswith("- "):
                break
            name = str(stripped[2:].split("|", 1)[0] or "").strip()
            key = re.sub(r"[^a-z0-9]+", "", name.lower())
            if not name or not key or key in seen:
                continue
            seen.add(key)
            names.append(name)
        return names

    @classmethod
    def _match_property_ids_for_names(
        cls,
        *,
        property_names: list[str],
        available_properties: list[dict[str, Any]] | None,
        pending_data: Any,
    ) -> list[str]:
        if not property_names:
            return []
        exact_index: dict[str, str] = {}
        ambiguous: set[str] = set()
        for row in (available_properties or []):
            if not isinstance(row, dict):
                continue
            property_id = cls._normalize_identifier(row.get("id") or row.get("property_id"))
            if not property_id:
                continue
            candidates = [
                str(row.get("name") or row.get("property_name") or "").strip(),
                *[
                    str(alias).strip()
                    for alias in list(row.get("aliases") or [])
                    if str(alias).strip()
                ],
            ]
            for candidate in candidates:
                key = re.sub(r"[^a-z0-9]+", "", candidate.lower())
                if not key:
                    continue
                existing = exact_index.get(key)
                if existing and existing != property_id:
                    ambiguous.add(key)
                    continue
                exact_index[key] = property_id
        pending_name_map = cls._normalize_property_name_map(
            pending_data if isinstance(pending_data, dict) else {}
        )
        for property_id, property_name in pending_name_map.items():
            key = re.sub(r"[^a-z0-9]+", "", property_name.lower())
            if not key:
                continue
            existing = exact_index.get(key)
            if existing and existing != property_id:
                ambiguous.add(key)
                continue
            exact_index[key] = property_id

        matched: list[str] = []
        for property_name in property_names:
            key = re.sub(r"[^a-z0-9]+", "", str(property_name or "").lower())
            property_id = exact_index.get(key, "")
            if not property_id or key in ambiguous or property_id in matched:
                continue
            matched.append(property_id)
        return matched

    @classmethod
    def _reconcile_discussed_property_state(
        cls,
        *,
        context: ConversationContext,
        available_properties: list[dict[str, Any]] | None,
        active_property_ids: list[str] | None,
        past_property_context: dict[str, Any] | None,
        memory_summary: Any = "",
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        pending = context.pending_data if isinstance(getattr(context, "pending_data", None), dict) else {}
        active_ids = cls._normalize_property_id_list(list(active_property_ids or []))
        past_ctx_raw = dict(past_property_context or {})
        scoped_catalog_ids = {
            cls._normalize_identifier(row.get("id") or row.get("property_id"))
            for row in (available_properties or [])
            if isinstance(row, dict) and cls._normalize_identifier(row.get("id") or row.get("property_id"))
        }
        known_catalog_ids = cls._normalize_property_id_list(
            [
                *list(scoped_catalog_ids),
                *list(cls._normalize_property_name_map(
                    pending.get("_property_name_map") if isinstance(pending, dict) else {}
                ).keys()),
                *active_ids,
                *list(past_ctx_raw.keys()),
            ]
        )

        summary_names = cls._extract_discussed_hotel_names_from_summary(memory_summary)
        summary_ids = cls._match_property_ids_for_names(
            property_names=summary_names,
            available_properties=available_properties,
            pending_data=pending.get("_property_name_map") if isinstance(pending, dict) else {},
        )

        existing_order = cls._normalize_property_id_list(
            list(pending.get("_discussed_property_order") or []) if isinstance(pending, dict) else []
        )
        past_ids = cls._normalize_property_id_list(list(past_ctx_raw.keys()))

        # A fleet-attribute / broaden answer (e.g. "which hotel is pet friendly")
        # puts nearly the WHOLE catalog into active scope for ONE turn. That is
        # scope, not genuine interest — folding it into the discussed order
        # pollutes "which hotels did we discuss" with hotels the guest never
        # engaged with. So treat a broad active set as fleet-scope and DO NOT
        # record it as "discussed"; only a genuine narrow focus (the guest
        # named/narrowed to a hotel or a small city cluster) counts.
        _is_fleet_scope = bool(
            known_catalog_ids
            and len(active_ids) >= max(6, len(known_catalog_ids) // 2)
        )
        _active_for_discussed = [] if _is_fleet_scope else active_ids

        order_ids: list[str] = []
        # Live per-turn state is fresher than the async summary. The summary can
        # lag by multiple turns, so it may omit a recently discussed hotel
        # (Patna/Goa-style failures). Use the live discussed order as truth and
        # let the summary only backfill missing older hotels without shrinking
        # the conversation set.
        order_ids.extend(existing_order)
        for property_id in [*_active_for_discussed, *past_ids, *summary_ids]:
            if property_id and property_id not in order_ids:
                order_ids.append(property_id)

        discussed_properties: list[dict[str, str]] = []
        deduped_order: list[str] = []
        for property_id in order_ids:
            if not property_id:
                continue
            if known_catalog_ids and property_id not in known_catalog_ids:
                continue
            property_name = cls._resolve_property_name_from_pending(
                pending_data=pending,
                property_id=property_id,
                fallback=next(
                    (
                        str(row.get("name") or row.get("property_name") or "").strip()
                        for row in (available_properties or [])
                        if isinstance(row, dict)
                        and cls._normalize_identifier(row.get("id") or row.get("property_id")) == property_id
                    ),
                    property_id,
                ),
            )
            if not property_name:
                continue
            deduped_order.append(property_id)
            discussed_properties.append({"id": property_id, "name": property_name})

        reconciled_past: dict[str, str] = {}
        for row in discussed_properties:
            property_id = cls._normalize_identifier(row.get("id"))
            property_name = str(row.get("name") or "").strip()
            if not property_id or property_id in active_ids or not property_name:
                continue
            existing_note = str(past_ctx_raw.get(property_id) or "").strip()
            reconciled_past[property_id] = existing_note or f"Previously discussed: {property_name}"

        if isinstance(pending, dict):
            pending["_discussed_property_order"] = deduped_order[-12:]
            pending["_past_property_context"] = dict(list(reconciled_past.items())[-10:])

        return dict(list(reconciled_past.items())[-10:]), discussed_properties[-12:]



    @classmethod
    def _is_compare_with_discussed_hotels_request(cls, user_message: Any) -> bool:
        text = cls._normalize_ws_lower(user_message)
        if not text:
            return False
        compare_markers = (
            "compare",
            "which is better",
            "which one is better",
            "difference between",
            " versus ",
            " vs ",
        )
        history_markers = (
            "prev",
            "previous",
            "earlier",
            "before",
            "first one",
            "last one",
        )
        return any(marker in text for marker in compare_markers) and any(
            marker in text for marker in history_markers
        )

    @classmethod
    def _direct_discussed_hotel_recall_reply(
        cls,
        *,
        user_message: Any,
        discussed_properties: list[dict[str, str]] | None,
    ) -> str:
        text = cls._normalize_ws_lower(user_message)
        discussed = [row for row in (discussed_properties or []) if isinstance(row, dict)]
        if not text or not discussed:
            return ""

        if re.search(
            r"\b(what|which)\s+was\s+(the\s+)?(1st|first)\s+hotel\s+(we\s+)?(discussed|talked about|looked at)\b",
            text,
        ) or re.search(
            r"\bwhich\s+hotel\s+did\s+we\s+(discuss|talk about|look at)\s+first\b",
            text,
        ):
            first_name = str(discussed[0].get("name") or "").strip()
            if first_name:
                return f"The first hotel we discussed was **{first_name}**."

        if re.search(
            r"\b(previous|last|earlier)\s+hotel\b|\bhotel\s+we\s+discussed\s+before\b|\bone\s+before\b",
            text,
        ):
            names = [
                str(row.get("name") or "").strip()
                for row in discussed
                if str(row.get("name") or "").strip()
            ]
            if len(names) >= 2:
                preview = ", ".join(f"**{name}**" for name in names[:3])
                if len(names) > 3:
                    preview += ", and others"
                return f"We've talked about {preview}. Which one did you mean?"

        return ""

    @classmethod
    def _should_preserve_pending_key_on_property_switch(cls, key: Any) -> bool:
        normalized = cls._normalize_identifier(key)
        if not normalized:
            return False
        safe_exact = {
            "service_id",
            "service",
            "service_name",
            "booking_type",
            "booking_sub_category",
            "active_property_ids",
            "active_property_names",
            "past_property_context",
            "selected_property_scope_id",
            "selected_property_scope_ids",
            "selected_property_scope_name",
            "stay_property_id",
            "stay_property_name",
            "last_service_id",
            "last_service_name",
            "selected_phase_id",
            "guest_name",
            "full_name",
            "name",
            "guest_phone",
            "phone",
            "phone_number",
            "mobile",
            "mobile_number",
            "guest_email",
            "email",
            "email_address",
            "guest_count",
            "party_size",
            "adults",
            "adult_count",
            "children",
            "child_count",
            "kids",
            "rooms",
            "room_count",
            "check_in",
            "check_out",
            "checkin",
            "checkout",
            "checkin_date",
            "checkout_date",
            "stay_checkin_date",
            "stay_checkout_date",
            "stay_date_range",
            "arrival_date",
            "departure_date",
            "special_request",
            "special_requests",
        }
        if normalized in safe_exact:
            return True
        if any(marker in normalized for marker in ("phone", "mobile", "email")):
            return True
        if normalized.endswith("_name") or normalized.startswith("guest_"):
            return True
        return any(
            marker in normalized
            for marker in (
                "checkin",
                "checkout",
                "check_in",
                "check_out",
                "arrival",
                "departure",
                "date_range",
                "stay_date",
                "guest_count",
                "party_size",
                "adult",
                "child",
                "room_count",
            )
        )

    def _clear_pending_state_for_property_switch(
        self,
        *,
        context: ConversationContext,
        services_snapshot: list[dict[str, Any]],
        current_service_id: str,
    ) -> list[str]:
        if not isinstance(context.pending_data, dict):
            return []
        normalized_service_id = self._normalize_identifier(current_service_id)
        normalized_pending_action = self._normalize_identifier(getattr(context, "pending_action", ""))
        required_slot_ids: set[str] = set()
        if normalized_service_id:
            for service in services_snapshot or []:
                if not isinstance(service, dict):
                    continue
                if self._normalize_identifier(service.get("id")) != normalized_service_id:
                    continue
                for slot in list(service.get("required_slots") or []):
                    if not isinstance(slot, dict):
                        continue
                    slot_id = self._normalize_identifier(slot.get("id"))
                    if slot_id:
                        required_slot_ids.add(slot_id)
                break
        extra_clear_ids: set[str] = set()
        if normalized_service_id == "room_booking" or normalized_pending_action in {
            "room_booking",
            "collect_room_booking_details",
            "select_room_type",
            "confirm_room_booking",
            "confirm_room_availability_check",
            "confirm_booking",
        }:
            extra_clear_ids.update(
                {
                    "room_type",
                    "room_name",
                    "selected_room",
                    "selected_room_type",
                    "requested_room_type",
                    "room_type_candidates",
                    "room_type_preference",
                }
            )
        cleared_keys: list[str] = []
        clear_every_non_safe_public_key = not bool(required_slot_ids)

        for raw_key in list(context.pending_data.keys()):
            if not isinstance(raw_key, str) or raw_key.startswith("_"):
                continue
            normalized_key = self._normalize_identifier(raw_key)
            if not normalized_key or self._should_preserve_pending_key_on_property_switch(normalized_key):
                continue
            should_clear = normalized_key in extra_clear_ids
            if not should_clear and clear_every_non_safe_public_key:
                should_clear = True
            if not should_clear and normalized_key in required_slot_ids:
                should_clear = True
            if not should_clear:
                continue
            context.pending_data.pop(raw_key, None)
            cleared_keys.append(raw_key)

        integration = context.pending_data.get("_integration")
        if isinstance(integration, dict):
            integration_changed = False
            for raw_key in list(integration.keys()):
                normalized_key = self._normalize_identifier(raw_key)
                if not normalized_key or self._should_preserve_pending_key_on_property_switch(normalized_key):
                    continue
                should_clear = normalized_key in extra_clear_ids
                if not should_clear and clear_every_non_safe_public_key:
                    should_clear = True
                if not should_clear and normalized_key in required_slot_ids:
                    should_clear = True
                if not should_clear:
                    continue
                integration.pop(raw_key, None)
                integration_changed = True
                cleared_keys.append(f"_integration.{raw_key}")
            if integration_changed:
                if integration:
                    context.pending_data["_integration"] = integration
                else:
                    context.pending_data.pop("_integration", None)
        return cleared_keys




    def _build_faq_candidates(
        self,
        *,
        capabilities_summary: dict[str, Any] | None = None,
        max_items: int | None = None,
        active_property_slugs: Any = None,
    ) -> list[dict[str, Any]]:
        cap_summary = capabilities_summary if isinstance(capabilities_summary, dict) else {}
        raw_bank = cap_summary.get("faq_bank", [])
        if not isinstance(raw_bank, list) or not raw_bank:
            try:
                raw_bank = config_service.get_faq_bank()
            except Exception:
                raw_bank = []

        limit_cfg = max_items
        if limit_cfg is None:
            limit_cfg = int(getattr(settings, "llm_orchestration_faq_max_items", 80) or 80)
        limit = max(0, min(int(limit_cfg or 0), 200))
        if limit <= 0:
            return []

        # Property scope: drop other-property FAQs only when a property is focused;
        # common FAQs (no property_slug) always pass. Mirrors build_faq_block.
        scope_set: set[str] = set()
        if isinstance(active_property_slugs, (list, tuple, set)):
            scope_set = {
                self._normalize_identifier(slug)
                for slug in active_property_slugs
                if self._normalize_identifier(slug)
            }

        entries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for faq in raw_bank:
            if not isinstance(faq, dict):
                continue
            if not bool(faq.get("enabled", True)):
                continue
            faq_slug = self._normalize_identifier(faq.get("property_slug"))
            if scope_set and faq_slug and faq_slug not in scope_set:
                continue
            question = self._compact_text(faq.get("question"), max_chars=300)
            answer = self._compact_text(faq.get("answer"), max_chars=1000)
            faq_id = self._normalize_identifier(
                faq.get("id")
                or question
                or ""
            )
            if not faq_id or not question or not answer:
                continue
            if faq_id in seen_ids:
                continue
            seen_ids.add(faq_id)
            tags_raw = faq.get("tags", [])
            tags: list[str] = []
            if isinstance(tags_raw, list):
                for tag in tags_raw:
                    cleaned = self._compact_text(tag, max_chars=60).lower()
                    if cleaned:
                        tags.append(cleaned)
                    if len(tags) >= 12:
                        break
            entries.append(
                {
                    "faq_id": faq_id,
                    "question": question,
                    "answer": answer,
                    "description": self._compact_text(faq.get("description"), max_chars=240),
                    "tags": tags,
                }
            )
            if len(entries) >= limit:
                break
        return entries

    @staticmethod
    def _coerce_confidence(value: Any, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        if parsed != parsed:  # NaN guard
            return default
        return max(0.0, min(1.0, parsed))

    def _normalize_faq_resolution_metadata(
        self,
        decision: OrchestrationDecision,
        *,
        faq_candidates: list[dict[str, Any]] | None = None,
    ) -> OrchestrationDecision:
        if not isinstance(decision.metadata, dict):
            decision.metadata = {}
        metadata = decision.metadata

        known_ids: set[str] = set()
        for row in faq_candidates or []:
            if not isinstance(row, dict):
                continue
            row_id = self._normalize_identifier(row.get("faq_id") or row.get("id"))
            if row_id:
                known_ids.add(row_id)

        normalized_resolution: list[dict[str, Any]] = []
        seen_resolution: set[str] = set()
        raw_resolution = metadata.get("faq_resolution")
        if isinstance(raw_resolution, list):
            for item in raw_resolution:
                if not isinstance(item, dict):
                    continue
                faq_id = self._normalize_identifier(
                    item.get("faq_id")
                    or item.get("id")
                    or ""
                )
                relation = self._normalize_identifier(
                    item.get("relation")
                    or item.get("resolution")
                    or item.get("type")
                    or ""
                )
                if relation not in {"override", "merge", "ignore"}:
                    relation = "ignore"
                if faq_id:
                    dedupe_key = faq_id
                else:
                    dedupe_key = f"unknown:{relation}:{self._compact_text(item.get('reason'), max_chars=80)}"
                if dedupe_key in seen_resolution:
                    continue
                seen_resolution.add(dedupe_key)
                confidence = self._coerce_confidence(
                    item.get("confidence"),
                    default=1.0 if relation in {"override", "merge"} else 0.0,
                )
                applied = self._coerce_bool(
                    item.get("applied"),
                    default=relation in {"override", "merge"},
                )
                reason = self._compact_text(item.get("reason"), max_chars=260)
                fact = self._compact_text(
                    item.get("fact")
                    or item.get("resolved_fact")
                    or item.get("value")
                    or "",
                    max_chars=340,
                )
                normalized_item: dict[str, Any] = {
                    "faq_id": faq_id,
                    "relation": relation,
                    "applied": bool(applied),
                    "confidence": confidence,
                }
                if reason:
                    normalized_item["reason"] = reason
                if fact:
                    normalized_item["fact"] = fact
                if faq_id and known_ids and faq_id not in known_ids:
                    normalized_item["unknown_faq_id"] = True
                normalized_resolution.append(normalized_item)
                if len(normalized_resolution) >= 40:
                    break
        metadata["faq_resolution"] = normalized_resolution

        normalized_facts: list[dict[str, str]] = []
        seen_facts: set[str] = set()
        raw_facts = metadata.get("resolved_facts")
        if isinstance(raw_facts, list):
            for row in raw_facts:
                source = ""
                fact = ""
                if isinstance(row, dict):
                    source = self._compact_text(row.get("source"), max_chars=80).lower()
                    fact = self._compact_text(
                        row.get("fact")
                        or row.get("text")
                        or row.get("value")
                        or "",
                        max_chars=340,
                    )
                else:
                    fact = self._compact_text(row, max_chars=340)
                if not fact:
                    continue
                if not source:
                    source = "kb"
                dedupe_key = f"{source}|{fact.lower()}"
                if dedupe_key in seen_facts:
                    continue
                seen_facts.add(dedupe_key)
                normalized_facts.append({"source": source, "fact": fact})
                if len(normalized_facts) >= 40:
                    break

        if not normalized_facts:
            for row in normalized_resolution:
                if not isinstance(row, dict):
                    continue
                if not bool(row.get("applied")):
                    continue
                relation = self._normalize_identifier(row.get("relation"))
                if relation not in {"override", "merge"}:
                    continue
                fact = self._compact_text(row.get("fact"), max_chars=340)
                faq_id = self._normalize_identifier(row.get("faq_id"))
                if not fact:
                    continue
                source = f"faq:{faq_id}" if faq_id else "faq"
                dedupe_key = f"{source}|{fact.lower()}"
                if dedupe_key in seen_facts:
                    continue
                seen_facts.add(dedupe_key)
                normalized_facts.append({"source": source, "fact": fact})
                if len(normalized_facts) >= 20:
                    break

        metadata["resolved_facts"] = normalized_facts
        metadata["faq_resolution_applied"] = any(
            bool(row.get("applied"))
            and self._normalize_identifier(row.get("relation")) in {"override", "merge"}
            for row in normalized_resolution
            if isinstance(row, dict)
        )
        metadata["faq_candidates_considered"] = len(faq_candidates or [])
        return decision







    @staticmethod
    def _build_recent_history_rows(
        *,
        context: ConversationContext,
        max_messages: int = 12,
        max_chars: int = 6000,
        append_assistant_message: str = "",
    ) -> list[dict[str, str]]:
        messages = getattr(context, "messages", []) or []
        total_chars = 0
        rows: list[dict[str, str]] = []
        iterable = messages[-max_messages:] if isinstance(messages, list) else []
        for item in iterable:
            if isinstance(item, dict):
                role = str(item.get("role") or "").strip().lower()
                content = str(item.get("content") or "").strip()
            else:
                role = str(getattr(item, "role", "") or "").strip().lower()
                content = str(getattr(item, "content", "") or "").strip()
            if not content or total_chars >= max_chars:
                continue
            clipped = content[: min(max_chars - total_chars, 850)]
            rows.append({"role": role or "message", "content": clipped})
            total_chars += len(clipped)

        assistant_text = str(append_assistant_message or "").strip()
        if assistant_text and total_chars < max_chars:
            normalized_assistant = re.sub(r"\s+", " ", assistant_text).strip().lower()
            last_row = rows[-1] if rows and isinstance(rows[-1], dict) else {}
            last_role = str(last_row.get("role") or "").strip().lower()
            last_content = re.sub(r"\s+", " ", str(last_row.get("content") or "")).strip().lower()
            if last_role != "assistant" or last_content != normalized_assistant:
                clipped = assistant_text[: min(max_chars - total_chars, 850)]
                if clipped:
                    rows.append({"role": "assistant", "content": clipped})
        return rows



    @staticmethod
    def _phase_transition_timing_hint(current_phase_id: str, service_phase_id: str) -> str:
        current_norm = str(current_phase_id or "").strip().lower().replace(" ", "_")
        service_norm = str(service_phase_id or "").strip().lower().replace(" ", "_")
        if not current_norm or not service_norm or current_norm == service_norm:
            return ""
        if current_norm == "pre_booking":
            if service_norm == "pre_checkin":
                return "after your booking is confirmed"
            if service_norm in {"during_stay", "post_checkout"}:
                return "after check-in"
        if current_norm == "pre_checkin":
            if service_norm == "during_stay":
                return "once you check in"
            if service_norm == "post_checkout":
                return "after checkout"
        if current_norm == "during_stay" and service_norm == "post_checkout":
            return "after checkout"
        return ""

    def _sanitize_json(self, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return str(value)[:8000]
        if value is None:
            return None
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:16000]
        if isinstance(value, list):
            return [self._sanitize_json(item, depth + 1) for item in value[:120]]
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for raw_key, raw_value in list(value.items())[:120]:
                key = str(raw_key)[:200]
                normalized[key] = self._sanitize_json(raw_value, depth + 1)
            return normalized
        return str(value)[:16000]

    @classmethod
    def _coerce_public_pending(cls, pending_data: Any) -> dict[str, Any]:
        if not isinstance(pending_data, dict):
            return {}
        public_pending = {
            key: value
            for key, value in pending_data.items()
            if isinstance(key, str) and not key.startswith("_")
        }
        mirror_fields = {
            "active_property_ids": "_active_property_ids",
            "active_property_names": "_active_property_names",
            "past_property_context": "_past_property_context",
            "selected_property_scope_id": "_selected_property_scope_id",
            "selected_property_scope_ids": "_selected_property_scope_ids",
            "selected_property_scope_name": "_selected_property_scope_name",
            "stay_property_id": "_stay_property_id",
            "stay_property_name": "_stay_property_name",
            "last_service_id": "_last_service_id",
            "last_service_name": "_last_service_name",
            "selected_phase_id": "_selected_phase_id",
        }
        for public_key, private_key in mirror_fields.items():
            value = pending_data.get(private_key)
            if value is None:
                continue
            public_pending[public_key] = value
        scope_fields = cls._canonical_property_scope_fields(
            active_property_ids=public_pending.get("active_property_ids"),
            active_property_names=public_pending.get("active_property_names"),
            selected_property_scope_id=public_pending.get("selected_property_scope_id"),
            selected_property_scope_ids=public_pending.get("selected_property_scope_ids"),
            selected_property_scope_name=public_pending.get("selected_property_scope_name"),
            property_name_map=pending_data.get("_property_name_map"),
        )
        if scope_fields:
            public_pending.update(scope_fields)
        if "service_id" not in public_pending and str(pending_data.get("_last_service_id") or "").strip():
            public_pending["service_id"] = str(pending_data.get("_last_service_id") or "").strip()
        if "service" not in public_pending and str(pending_data.get("_last_service_name") or "").strip():
            public_pending["service"] = str(pending_data.get("_last_service_name") or "").strip()
        return public_pending

    @staticmethod
    async def _chat_with_json(
        *,
        messages: list[dict[str, Any]],
        model: str | None,
        temperature: float | None,
        trace_context: dict[str, Any] | None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        try:
            return await llm_client.chat_with_json(
                messages=messages,
                model=model,
                temperature=temperature,
                trace_context=trace_context,
                max_tokens=max_tokens,
            )
        except TypeError as exc:
            # Backward compatibility for tests/mocks that still expose the older
            # chat_with_json(messages, model=None, temperature=None) signature.
            if "trace_context" not in str(exc) and "max_tokens" not in str(exc):
                raise
            return await llm_client.chat_with_json(
                messages=messages,
                model=model,
                temperature=temperature,
            )

    @staticmethod
    def _last_message_by_role(context: ConversationContext, role: str) -> str:
        if not isinstance(getattr(context, "messages", None), list):
            return ""
        role_norm = str(role or "").strip().lower()
        for msg in reversed(_current_thread_messages(context)):
            msg_role = str(getattr(getattr(msg, "role", None), "value", "") or "").strip().lower()
            if msg_role != role_norm:
                continue
            return str(getattr(msg, "content", "") or "").strip()
        return ""

    def _build_history_bundle(
        self,
        *,
        context: ConversationContext,
        memory_snapshot: dict[str, Any],
        last_window_messages: int = 10,
        last_window_chars: int = 5000,
        full_window_messages: int = 120,
        full_window_chars: int = 12000,
    ) -> dict[str, Any]:
        # Last-10 raw messages are always included for short-term grounding.
        last_10 = self._history_preview(
            context,
            max_messages=max(1, int(last_window_messages or 10)),
            max_chars=max(1000, int(last_window_chars or 5000)),
        )
        # Wider tail context is included with bounded size.
        full_tail = self._history_preview(
            context,
            max_messages=max(10, int(full_window_messages or 120)),
            max_chars=max(2000, int(full_window_chars or 12000)),
        )
        return {
            "history_last_10": last_10,
            "full_history_context": {
                "message_count_total": len(context.messages) if isinstance(context.messages, list) else 0,
                "tail_messages": full_tail,
                "older_context_summary": str((memory_snapshot or {}).get("summary") or "").strip()[:6000],
            },
            "last_user_message": self._last_message_by_role(context, "user"),
            "last_assistant_message": self._last_message_by_role(context, "assistant"),
        }

    def _service_name_by_id(self, service_id: str, services_snapshot: list[dict[str, Any]]) -> str:
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return ""
        for row in services_snapshot:
            if not isinstance(row, dict):
                continue
            if self._normalize_identifier(row.get("id")) != normalized_id:
                continue
            return str(row.get("name") or normalized_id).strip()
        return normalized_id

    def _suspend_active_service_task(
        self,
        *,
        context: ConversationContext,
        services_snapshot: list[dict[str, Any]],
        next_service_id: str = "",
        force: bool = False,
    ) -> bool:
        """
        Park the current in-flight service task before a topic/service diversion.
        Returns True when a task was suspended.
        """
        pending_public = self._coerce_public_pending(context.pending_data)
        current_active_service = self._normalize_identifier(pending_public.get("service_id") or "")
        pending_action = str(context.pending_action or "").strip()
        normalized_next = self._normalize_identifier(next_service_id)

        if not current_active_service or not pending_action:
            return False
        if not force and not normalized_next:
            return False
        if not force and normalized_next == current_active_service:
            return False

        has_collected_values = any(
            str(value or "").strip()
            for key, value in pending_public.items()
            if key != "service_id"
        )
        if not has_collected_values:
            return False

        existing_rows = context.suspended_services if isinstance(context.suspended_services, list) else []
        for existing in existing_rows:
            if not isinstance(existing, dict):
                continue
            if (
                self._normalize_identifier(existing.get("service_id")) == current_active_service
                and str(existing.get("pending_action") or "").strip() == pending_action
                and isinstance(existing.get("pending_data"), dict)
                and existing.get("pending_data") == pending_public
            ):
                # Already parked; only clear active state.
                internal = {
                    key: value
                    for key, value in (context.pending_data or {}).items()
                    if isinstance(key, str) and key.startswith("_")
                }
                context.pending_data = internal
                context.pending_action = None
                return True

        context.suspended_services.append(
            {
                "service_id": current_active_service,
                "service_name": self._service_name_by_id(current_active_service, services_snapshot),
                "pending_data": dict(pending_public),
                "pending_action": pending_action,
            }
        )
        internal = {
            key: value
            for key, value in (context.pending_data or {}).items()
            if isinstance(key, str) and key.startswith("_")
        }
        context.pending_data = internal
        context.pending_action = None
        return True

    @staticmethod
    def _phase_label(phase_id: str, capabilities_summary: dict[str, Any]) -> str:
        normalized = str(phase_id or "").strip().lower().replace(" ", "_")
        if not normalized:
            return ""
        phase_rows = capabilities_summary.get("journey_phases")
        if not isinstance(phase_rows, list):
            phase_rows = capabilities_summary.get("phases")
        if isinstance(phase_rows, list):
            for row in phase_rows:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or "").strip().lower().replace(" ", "_")
                if rid == normalized:
                    label = str(row.get("name") or "").strip()
                    if label:
                        return label
        return normalized.replace("_", " ").title()

    def _resolve_selected_phase(
        self,
        *,
        context: ConversationContext,
        selected_phase_context: dict[str, Any] | None,
        capabilities_summary: dict[str, Any],
    ) -> tuple[str, str]:
        selected = selected_phase_context if isinstance(selected_phase_context, dict) else {}
        selected_id = self._normalize_identifier(selected.get("selected_phase_id"))
        selected_name = str(selected.get("selected_phase_name") or "").strip()
        if selected_id:
            if not selected_name:
                selected_name = self._phase_label(selected_id, capabilities_summary)
            return selected_id, selected_name

        pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = pending_raw.get("_integration", {})
        if isinstance(integration, dict):
            phase_candidate = self._normalize_identifier(integration.get("phase"))
            if phase_candidate:
                return phase_candidate, self._phase_label(phase_candidate, capabilities_summary)

        fallback = "pre_booking"
        return fallback, self._phase_label(fallback, capabilities_summary)


    def _service_extracted_knowledge_by_service(self, capabilities_summary: dict[str, Any]) -> dict[str, str]:
        """Return mapping of service_id -> LLM-extracted knowledge string (from enrich_service_kb_records)."""
        records = capabilities_summary.get("service_kb_records", [])
        if not isinstance(records, list):
            return {}
        mapping: dict[str, str] = {}
        for row in records[:220]:
            if not isinstance(row, dict):
                continue
            service_id = self._normalize_identifier(row.get("service_id"))
            if not service_id:
                continue
            extracted = str(row.get("extracted_knowledge") or "").strip()
            if extracted:
                mapping[service_id] = extracted
        return mapping




    @staticmethod
    def _service_routing_keywords(*, service_row: dict[str, Any], prompt_pack: dict[str, Any]) -> list[str]:
        source_text = " ".join(
            [
                str(service_row.get("id") or ""),
                str(service_row.get("name") or ""),
                str(service_row.get("type") or ""),
                str(service_row.get("description") or ""),
                str(prompt_pack.get("profile") or ""),
                str(prompt_pack.get("role") or ""),
            ]
        ).strip().lower()
        if not source_text:
            return []
        stopwords = {
            "the",
            "and",
            "for",
            "with",
            "you",
            "your",
            "our",
            "hotel",
            "service",
            "assistant",
            "support",
            "help",
            "request",
            "booking",
        }
        keywords: list[str] = []
        seen: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", source_text):
            if len(token) < 3 or token in stopwords:
                continue
            if token in seen:
                continue
            seen.add(token)
            keywords.append(token)
            if len(keywords) >= 14:
                break
        return keywords

    def _service_snapshot(self, capabilities_summary: dict[str, Any]) -> list[dict[str, Any]]:
        services = capabilities_summary.get("service_catalog", [])
        if not isinstance(services, list):
            return []
        # Merge extracted_knowledge from service_kb_records so service agents
        # get the same KB data that the main orchestrator sees.
        kb_by_service = self._service_extracted_knowledge_by_service(capabilities_summary)
        result: list[dict[str, Any]] = []
        for row in services:
            if not isinstance(row, dict):
                continue
            sid = self._normalize_identifier(row.get("id"))
            if not sid:
                continue
            prompt_pack = row.get("service_prompt_pack")
            if not isinstance(prompt_pack, dict):
                prompt_pack = config_service.get_service_prompt_pack(sid)
            if not isinstance(prompt_pack, dict):
                prompt_pack = {}
            execution_guard = prompt_pack.get("execution_guard", {})
            if not isinstance(execution_guard, dict):
                execution_guard = {}
            confirmation_format = prompt_pack.get("confirmation_format", {})
            if not isinstance(confirmation_format, dict):
                confirmation_format = {}
            prompt_ticketing_policy = prompt_pack.get("ticketing_policy", {})
            if not isinstance(prompt_ticketing_policy, dict):
                prompt_ticketing_policy = {}
            ticketing_conditions = str(prompt_pack.get("ticketing_conditions") or "").strip()
            if not ticketing_conditions:
                ticketing_conditions = str(prompt_ticketing_policy.get("policy") or "").strip()
            if not ticketing_conditions:
                ticketing_conditions = str(row.get("ticketing_policy") or "").strip()
            prompt_pack_source = str(prompt_pack.get("source") or "").strip().lower()
            pack_is_admin_managed = (
                bool(row.get("service_prompt_pack_custom", False))
                or prompt_pack_source in {"manual_override", "admin_ui", "admin_override", "db"}
            )
            required_slots = prompt_pack.get("required_slots", []) if pack_is_admin_managed else []
            normalized_slots: list[dict[str, Any]] = []
            if isinstance(required_slots, list):
                for item in required_slots[:15]:
                    if not isinstance(item, dict):
                        continue
                    normalized_slots.append(
                        {
                            "id": self._normalize_identifier(item.get("id")),
                            "label": str(item.get("label") or "").strip(),
                            "required": bool(item.get("required", True)),
                        }
                    )
            # Preserve form_config from the original service row so the service
            # agent gets ticketing_mode, trigger_field, and form fields at runtime.
            _row_form_config = row.get("form_config")
            if not isinstance(_row_form_config, dict):
                _row_form_config = None

            result.append(
                {
                    "id": sid,
                    "name": str(row.get("name") or sid).strip(),
                    "type": str(row.get("type") or "service").strip(),
                    "description": str(row.get("description") or "").strip(),
                    "is_active": bool(row.get("is_active", True)),
                    "phase_id": self._normalize_identifier(row.get("phase_id")),
                    "phase_name": self._phase_label(self._normalize_identifier(row.get("phase_id")), capabilities_summary),
                    "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                    "ticketing_policy": str(row.get("ticketing_policy") or "").strip(),
                    "ticketing_conditions": ticketing_conditions,
                    "ticketing_mode": str(row.get("ticketing_mode") or "").strip().lower(),
                    "form_config": _row_form_config,
                    "required_slots": normalized_slots,
                    "profile": str(prompt_pack.get("profile") or "").strip().lower(),
                    "service_prompt_role": str(prompt_pack.get("role") or "").strip()[:300],
                    "service_prompt_behavior": str(prompt_pack.get("professional_behavior") or "").strip()[:500],
                    "execution_guard": self._sanitize_json(execution_guard),
                    "confirmation_format": self._sanitize_json(confirmation_format),
                    "service_prompt_pack": self._sanitize_json(prompt_pack),
                    "service_prompt_pack_custom": bool(row.get("service_prompt_pack_custom", False)),
                    "knowledge_facts": [],
                    # Merge extracted_knowledge: prefer prompt_pack (admin-entered),
                    # fall back to service_kb_records (auto-compiled from KB files).
                    "extracted_knowledge": (
                        str(prompt_pack.get("extracted_knowledge") or "").strip()
                        or kb_by_service.get(sid, "")
                    ),
                    "generated_system_prompt": str(row.get("generated_system_prompt") or "").strip(),
                    "cuisine": str(row.get("cuisine") or "").strip(),
                    "hours": row.get("hours") if isinstance(row.get("hours"), dict) else {},
                    "delivery_zones": row.get("delivery_zones") if isinstance(row.get("delivery_zones"), list) else [],
                    "confirmation_pending_action": "confirm_booking",
                    "routing_keywords": self._service_routing_keywords(service_row=row, prompt_pack=prompt_pack),
                }
            )
        return result

    @staticmethod
    def _looks_like_catalog_information_request(message: str) -> bool:
        text = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not text:
            return False
        markers = (
            "what rooms",
            "room types",
            "list",
            "all rooms",
            "all room",
            "more rooms",
            "more options",
            "tell me all",
            "show all",
            "available rooms",
            "with bathtub",
            "suite options",
        )
        if any(marker in text for marker in markers):
            return True
        return text.startswith(("what", "which", "show", "list", "tell me", "give me", "more"))

    @staticmethod
    def _room_type_candidates_from_text(content: str) -> list[str]:
        text = str(content or "")
        if not text:
            return []
        candidates: list[str] = []
        seen: set[str] = set()

        section_match = re.search(r"section:\s*([^\n\r]+)", text, flags=re.IGNORECASE)
        if section_match:
            section_name = str(section_match.group(1) or "").strip()
            if section_name:
                lowered = section_name.lower()
                if "room" in lowered or "suite" in lowered:
                    key = lowered
                    if key not in seen:
                        seen.add(key)
                        candidates.append(section_name.title())

        pattern = re.compile(
            r"\b([A-Z][A-Za-z0-9&'/-]*(?:\s+[A-Z][A-Za-z0-9&'/-]*)*\s+(?:Room|Suite))\b"
        )
        for match in pattern.findall(text):
            label = str(match or "").strip()
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(label)
            if len(candidates) >= 20:
                break
        return candidates

    @staticmethod
    def _compact_message_text(message: Any) -> str:
        return re.sub(r"\s+", " ", str(message or "").strip().lower())

    @classmethod
    def _is_generic_affirmation(cls, message: str) -> bool:
        compact = cls._compact_message_text(message)
        if not compact or "?" in compact:
            return False
        exact_markers = {
            "yes",
            "yes please",
            "yeah",
            "yep",
            "sure",
            "sure thing",
            "ok",
            "okay",
            "please do",
            "go ahead",
            "continue",
            "proceed",
            "book it",
            "lets do it",
            "let's do it",
        }
        if compact in exact_markers:
            return True
        tokens = compact.split()
        return len(tokens) <= 3 and compact in {"do it", "sounds good"}


    @classmethod
    def _is_support_handoff_service(
        cls,
        *,
        service_id: str,
        service_name: str = "",
        description: str = "",
    ) -> bool:
        norm_id = cls._normalize_identifier(service_id)
        norm_name = cls._compact_message_text(service_name)
        norm_desc = cls._compact_message_text(description)
        return any(
            marker in norm_id
            for marker in (
                "support",
                "complaint",
                "issue",
                "feedback",
                "callback",
                "helpdesk",
                "contact_staff",
                "staff_callback",
                "raise_request",
            )
        ) or any(
            marker in norm_name
            for marker in (
                "support",
                "complaint",
                "issue",
                "feedback",
                "callback",
                "help desk",
                "helpdesk",
                "contact staff",
            )
        ) or any(
            marker in norm_desc
            for marker in (
                "support intake",
                "support form",
                "talk to staff",
                "connect to staff",
                "connect with staff",
                "speak to staff",
                "report an issue",
                "raise a request",
                "raise request",
                "callback",
            )
        )


    @classmethod
    def _response_signals_missing_room_detail(cls, response_text: str) -> bool:
        compact = cls._compact_message_text(response_text)
        if not compact:
            return False
        markers = (
            "i don't have",
            "i dont have",
            "i do not have",
            "not publicly listed",
            "no public details",
            "not listed",
            "is not listed",
            "isn't listed",
            "basis is not available",
            "details are not publicly listed",
            "specific details on size or price are not publicly listed",
        )
        return any(marker in compact for marker in markers)

    @classmethod
    def _response_offers_staff_handoff(cls, response_text: str) -> bool:
        compact = cls._compact_message_text(response_text)
        if not compact:
            return False
        markers = (
            "would you like our team to reach out",
            "would you like our team to share",
            "connect you with our team",
            "team to reach out",
            "connect to staff",
            "connect with staff",
            "talk to staff",
            "speak with our team",
        )
        return any(marker in compact for marker in markers)

    @classmethod
    def _message_explicitly_requests_human_contact(cls, message: str) -> bool:
        compact = cls._compact_message_text(message).lower()
        if not compact:
            return False
        phrases = (
            "talk to staff",
            "talk to a staff",
            "talk to someone",
            "talk to a human",
            "talk to a person",
            "talk to an agent",
            "talk to a real",
            "speak to staff",
            "speak with staff",
            "speak to someone",
            "speak with someone",
            "speak to a human",
            "speak to a person",
            "speak to an agent",
            "connect me",
            "connect to staff",
            "connect with staff",
            "put me through",
            "call me back",
            "callback",
            "contact me",
            "have your team contact me",
            "have the team contact me",
            "i want a human",
            "i need a human",
            "want a human",
            "need a human",
            "real person",
            "live agent",
            "human agent",
            "can a human",
            "get me a human",
            "get me someone",
            "talk to a member",
            "speak to a member",
            "talk to your team",
            "talk to the team",
        )
        return any(phrase in compact for phrase in phrases)

    @classmethod
    def _message_looks_like_prearrival_instay_request_or_complaint(
        cls,
        message: str,
    ) -> bool:
        compact = cls._compact_message_text(message).lower()
        if not compact:
            return False
        direct_room_context = any(
            marker in compact
            for marker in (
                "my room",
                "in my room",
                "to my room",
                "room service",
                "housekeeping",
                "maintenance",
                "laundry",
                "front desk",
            )
        )
        request_verbs = any(
            marker in compact
            for marker in (
                "send",
                "bring",
                "deliver",
                "provide",
                "give",
                "need",
                "want",
                "get",
                "fix",
                "repair",
                "clean",
                "change",
                "replace",
                "book",
                "reserve",
                "arrange",
            )
        )
        service_or_issue_terms = any(
            marker in compact
            for marker in (
                "towel",
                "towels",
                "pillow",
                "pillows",
                "linen",
                "linens",
                "bedsheet",
                "bed sheet",
                "toiletries",
                "laundry",
                "housekeeping",
                "cleaning",
                "room service",
                "in room dining",
                "spa session",
                "massage",
                "restaurant table",
                "table reservation",
                "air conditioning",
                " ac ",
                "ac not working",
                "tv",
                "hot water",
                "cockroach",
                "roach",
                "bug",
                "dirty room",
                "not working",
                "broken",
            )
        )
        if direct_room_context and service_or_issue_terms:
            return True
        return request_verbs and service_or_issue_terms

    @classmethod
    def _normalize_prearrival_instay_request_decision(
        cls,
        *,
        decision: OrchestrationDecision,
        user_message: str,
        selected_phase_id: str,
    ) -> OrchestrationDecision:
        phase_norm = cls._normalize_phase_identifier(selected_phase_id)
        if phase_norm not in {"pre_booking", "pre_checkin"}:
            return decision
        if cls._message_explicitly_requests_human_contact(user_message):
            return decision
        if not cls._message_looks_like_prearrival_instay_request_or_complaint(user_message):
            return decision

        decision.action = "respond_only"
        decision.target_service_id = ""
        decision.pending_action = None
        decision.missing_fields = []
        decision.suggested_actions = []
        decision.requires_human_handoff = False
        decision.answered_current_query = True
        decision.followup_question = ""
        decision.blocking_fields = []
        decision.deferrable_fields = []
        try:
            decision.ticket.required = False
            decision.ticket.ready_to_create = False
            decision.ticket.generic_request = False
            decision.ticket.reason = ""
            decision.ticket.issue = ""
            decision.ticket.category = ""
            decision.ticket.sub_category = ""
            decision.ticket.priority = ""
            decision.ticket.evidence = []
        except Exception:
            pass
        decision.response_text = (
            "I'm sorry about that.\n\n"
            "This chat helps with hotel information and room booking before the stay.\n\n"
            "Requests like this need to be handled directly by the property team during the stay."
        )
        decision.metadata["prearrival_instay_request_decline_forced"] = True
        return decision

    @classmethod
    def _build_room_missing_info_handoff_clause(cls, selected_phase_id: str) -> str:
        """
        Phase-aware instruction injected into the room/booking agent prompt for the
        "I don't have that info -> offer staff callback" behavior.

        Judgment-based by design: the model decides whether a missing detail is a
        genuine booking-relevant fact (price, room size, occupancy/capacity, bed
        type, availability, view, inclusions, cancellation terms, etc.) rather than
        matching a hardcoded keyword list. The staff-callback offer is scoped to the
        pre_booking phase only; in every other phase the agent states it lacks the
        detail without offering a callback. Trivial in-room amenity trivia and idle
        curiosity never trigger a callback offer in any phase.
        """
        if cls._normalize_phase_identifier(selected_phase_id) == "pre_booking":
            return (
                "- MISSING BOOKING INFO -> STAFF OFFER (pre-booking): If the guest asks for a genuine room or booking detail that would reasonably affect their decision to book — price/rate, room size, how many people it sleeps (occupancy/capacity), bed type, availability for their dates, view, what's included (breakfast/taxes), or cancellation/refund terms — and that fact is NOT in the KB, reply with exactly two bullets: '- I don't have that detail right now.' followed by '- Would you like our team to reach out to share that with you?' Stop there. Never mention timeframes ('within 24 hours', 'shortly'). Do NOT fall back to 'which room type would you like to know more about?' when the missing fact is the blocker.\n"
                "- Use judgment, not a fixed list: anything a guest genuinely needs in order to choose or commit to a room counts. Do NOT offer staff connection for trivial in-room amenity trivia (telephone, kettle, hairdryer, water bottle, slippers, TV channels, plug type, etc.), idle curiosity, or services the hotel doesn't have at all — for those, answer from the KB or simply say you don't have that detail, with no staff offer.\n\n"
            )
        return (
            "- Never offer a staff callback for a missing detail in this phase. If a specific in-scope detail is not in the KB, say '- I don't have that detail right now.' and stop. Never offer staff connection for trivia, curiosity, or services the hotel doesn't have at all.\n\n"
        )

    @staticmethod
    def _normalize_prefill_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")




    @classmethod
    def _dedupe_labels(cls, values: list[str], *, limit: int = 20) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for raw in values:
            label = str(raw or "").strip()
            if not label:
                continue
            key = cls._compact_message_text(label)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(label)
            if len(deduped) >= limit:
                break
        return deduped

    @classmethod
    def _collect_room_type_candidates_from_history_bundle(
        cls,
        history_bundle: dict[str, Any] | None,
        *,
        knowledge_text: str = "",
    ) -> list[str]:
        texts: list[str] = []
        bundle = history_bundle if isinstance(history_bundle, dict) else {}
        for key in ("last_assistant_message", "last_user_message"):
            text = str(bundle.get(key) or "").strip()
            if text:
                texts.append(text)
        for row in bundle.get("history_last_10", []):
            if not isinstance(row, dict):
                continue
            text = str(row.get("content") or "").strip()
            if text:
                texts.append(text)
        if str(knowledge_text or "").strip():
            texts.append(str(knowledge_text or "")[:18000])

        candidates: list[str] = []
        for text in texts:
            candidates.extend(cls._room_type_candidates_from_text(text))
        return cls._dedupe_labels(candidates, limit=8)

    @classmethod
    def _extract_confirmable_room_from_last_assistant(cls, last_assistant_message: str) -> str:
        text = str(last_assistant_message or "").strip()
        compact = cls._compact_message_text(text)
        if not compact:
            return ""
        room_candidates = cls._room_type_candidates_from_text(text)
        if len(room_candidates) != 1:
            return ""
        confirmation_markers = (
            "would you like",
            "do you want",
            "shall i",
            "can i",
            "may i",
            "proceed with",
            "continue with",
            "go ahead with",
            "book the",
            "booking the",
        )
        if not any(marker in compact for marker in confirmation_markers):
            return ""
        if "?" not in text and "please fill in the booking details below" not in compact:
            return ""
        return room_candidates[0]




    @classmethod
    def _enforce_explicit_room_type_selection(
        cls,
        *,
        decision: OrchestrationDecision,
        user_message: str,
        history_bundle: dict[str, Any] | None,
        extracted_knowledge: str = "",
        trigger_field_id: str,
        trigger_field_label: str,
    ) -> OrchestrationDecision:
        bundle = history_bundle if isinstance(history_bundle, dict) else {}
        pending_updates = dict(decision.pending_data_updates or {})
        room_candidates = cls._collect_room_type_candidates_from_history_bundle(
            bundle,
            knowledge_text=str(extracted_knowledge or ""),
        )
        if room_candidates:
            pending_updates["room_type_candidates"] = list(room_candidates)

        _, trigger_value_raw = resolve_trigger_field_value(
            pending_updates,
            trigger_field_id,
            trigger_field_label,
        )
        trigger_value = str(trigger_value_raw or "").strip()

        normalization_intent = bool(trigger_value) or (
            str(decision.pending_action or "").strip().lower() == "collect_form_details"
        ) or (str(decision.action or "").strip().lower() == "collect_info")
        explicit_room = ""
        if normalization_intent:
            _, inferred_room = infer_trigger_value_from_message(
                pending_updates,
                trigger_field_id,
                trigger_field_label,
                user_message,
            )
            explicit_room = str(inferred_room or "").strip()

        last_assistant_message = str(bundle.get("last_assistant_message") or "")
        generic_affirmation = cls._is_generic_affirmation(user_message)
        confirmable_room = ""
        if generic_affirmation and cls._assistant_message_invites_room_booking(last_assistant_message):
            confirmable_room = cls._extract_confirmable_room_from_last_assistant(
                last_assistant_message
            )

        valid_trigger_room = ""
        if confirmable_room:
            valid_trigger_room = confirmable_room
        elif explicit_room and normalization_intent:
            valid_trigger_room = explicit_room

        if valid_trigger_room:
            pending_updates[trigger_field_id] = valid_trigger_room
            decision.pending_data_updates = pending_updates
            decision.missing_fields = normalize_trigger_missing_fields(
                decision.missing_fields,
                trigger_field_id,
                trigger_field_label,
                trigger_value_present=True,
            )
            if confirmable_room:
                decision.action = "collect_info"
                decision.pending_action = "collect_form_details"
                decision.metadata["room_booking_trigger_from_last_assistant"] = confirmable_room
                if (
                    not str(decision.response_text or "").strip()
                    or "fill in the booking details below"
                    not in str(decision.response_text or "").strip().lower()
                ):
                    decision.response_text = (
                        f"Great choice! We can proceed with the {confirmable_room}. "
                        "Please fill in the booking details below."
                    )
            return decision

        invalid_form_state = bool(trigger_value) or (
            str(decision.pending_action or "").strip().lower() == "collect_form_details"
        )
        if generic_affirmation and cls._assistant_message_invites_room_booking(last_assistant_message):
            invalid_form_state = True
        if not invalid_form_state:
            decision.pending_data_updates = pending_updates
            return decision

        pending_updates.pop(trigger_field_id, None)
        decision.pending_data_updates = pending_updates
        decision.action = "collect_info"
        decision.pending_action = "select_room_type"
        decision.missing_fields = normalize_trigger_missing_fields(
            ["room_type"],
            trigger_field_id,
            trigger_field_label,
            trigger_value_present=False,
        )
        decision.metadata["room_booking_requires_explicit_room_type"] = True
        if trigger_value:
            decision.metadata["room_booking_stripped_unconfirmed_trigger"] = True
            decision.metadata["room_booking_invalid_trigger_value"] = trigger_value
        decision.response_text = cls._rewrite_room_booking_response_for_room_selection(
            decision.response_text,
            room_candidates,
        )
        return decision

    async def _build_service_grounding_pack(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        service: dict[str, Any],
        service_profile: str,
        is_first_service_turn: bool = False,
    ) -> dict[str, Any]:
        """Return full KB text only when service has no extracted_knowledge yet."""
        extracted = str(service.get("extracted_knowledge") or "").strip()
        if extracted:
            # Enrichment has already run — service_knowledge in payload is sufficient
            return {"full_kb_text": ""}
        # No extracted knowledge yet — fall back to full KB so LLM can still answer
        from services.config_service import config_service as _cs
        full_kb_text = _cs.get_full_kb_text_with_sources(
            max_chars=40_000,
            max_sources=200,
        )
        return {"full_kb_text": full_kb_text}

    def _build_pending_action_context(self, context: Any) -> str:
        """Return a human-readable sentence describing what the guest was mid-flow on."""
        pending_action = str(getattr(context, "pending_action", "") or "")
        if not pending_action:
            return ""
        pending_data = getattr(context, "pending_data", {}) or {}
        service_id = str(pending_data.get("service_id", "") or "").strip()
        svc_name = ""
        if service_id:
            try:
                from services.config_service import config_service as _cs
                svc = _cs.get_service(service_id)
                if svc:
                    svc_name = str(svc.get("name", "") or "").strip()
            except Exception:
                pass
        collected = [
            k for k in pending_data
            if not str(k).startswith("_") and k not in {"service_id", "phase"}
            and pending_data[k] not in (None, "", [])
        ]
        parts: list[str] = []
        if svc_name:
            parts.append(f"service: {svc_name}")
        if collected:
            parts.append(f"already collected: {', '.join(collected)}")
        suffix = f" ({', '.join(parts)})" if parts else ""
        return f"Guest was mid-flow: {pending_action}{suffix}"






    def _resolve_target_service_for_followups(
        self,
        *,
        decision: OrchestrationDecision,
        target_service: dict[str, Any] | None = None,
        prefetched_services: list[dict[str, Any]] | None = None,
        in_phase_services: list[dict[str, Any]] | None = None,
        out_of_phase_services: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        if isinstance(target_service, dict):
            return target_service
        target_service_id = self._normalize_identifier(
            decision.target_service_id
            or (decision.metadata or {}).get("target_service_id")
            or ""
        )
        if not target_service_id:
            return None
        for row in list(prefetched_services or []) + list(in_phase_services or []) + list(out_of_phase_services or []):
            if not isinstance(row, dict):
                continue
            if self._normalize_identifier(row.get("id")) == target_service_id:
                return row
        return None


















    @classmethod
    def _find_supporting_evidence_excerpt(cls, *, text: str, grounding_text: str) -> str:
        normalized_grounding = str(grounding_text or "").strip()
        if not normalized_grounding:
            return ""
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in normalized_grounding.splitlines()
            if re.sub(r"\s+", " ", line).strip()
        ]
        if not lines:
            return ""
        best_line = ""
        best_score = -1
        tokens = cls._chip_content_tokens(text)
        for line in lines:
            line_norm = cls._normalize_chip_match_text(line)
            if not line_norm:
                continue
            score = 0
            for token in tokens:
                aliases = cls._chip_token_aliases(token)
                if any(alias and alias in line_norm for alias in aliases):
                    score += 1
            if score > best_score:
                best_score = score
                best_line = line[:220]
        if best_score <= 0:
            return ""
        return best_line






    @staticmethod
    def _history_preview(context: ConversationContext, max_messages: int, max_chars: int) -> list[dict[str, str]]:
        messages = context.get_recent_messages(max_messages if max_messages > 0 else 12)
        result: list[dict[str, str]] = []
        total = 0
        for msg in messages:
            content = str(msg.content or "").strip()
            if not content:
                continue
            room_left = max_chars - total
            if room_left <= 0:
                break
            clipped = content[: min(room_left, 2500)]
            result.append({"role": msg.role.value, "content": clipped})
            total += len(clipped)
        return result

    @staticmethod
    def _script_language_hint(text: str) -> dict[str, str]:
        value = str(text or "")
        script_checks = (
            # U+0964 danda and U+0965 double danda are DELIBERATELY excluded from
            # the Devanagari range. They are shared Indic punctuation - Punjabi,
            # Bengali, Gujarati and others end sentences with the same mark - but
            # they live inside the Devanagari block, which is tested first. So a
            # perfectly ordinary Punjabi sentence ("... \u0A39\u0A48\u0964") matched Devanagari
            # on its full stop and came back as Hindi before its own Gurmukhi
            # letters were ever tested. Seen live on 2026-09-21: a Gurmukhi
            # question got a correct Gurmukhi answer that this classified Hindi.
            # Every real Devanagari letter and digit still matches.
            ("Hindi", "Devanagari", r"[\u0900-\u0963\u0966-\u097F]"),
            ("Bengali", "Bengali", r"[\u0980-\u09FF]"),
            ("Tamil", "Tamil", r"[\u0B80-\u0BFF]"),
            ("Telugu", "Telugu", r"[\u0C00-\u0C7F]"),
            ("Kannada", "Kannada", r"[\u0C80-\u0CFF]"),
            ("Malayalam", "Malayalam", r"[\u0D00-\u0D7F]"),
            ("Gujarati", "Gujarati", r"[\u0A80-\u0AFF]"),
            ("Punjabi", "Gurmukhi", r"[\u0A00-\u0A7F]"),
            ("Arabic", "Arabic", r"[\u0600-\u06FF]"),
            ("Chinese", "Han", r"[\u4E00-\u9FFF]"),
            ("Japanese", "Japanese", r"[\u3040-\u30FF]"),
            ("Korean", "Hangul", r"[\uAC00-\uD7AF]"),
            ("Russian", "Cyrillic", r"[\u0400-\u04FF]"),
            ("Thai", "Thai", r"[\u0E00-\u0E7F]"),
        )
        for language, script, pattern in script_checks:
            if re.search(pattern, value):
                return {"language": language, "script": script}
        return {}

    @classmethod
    def _normalize_response_language_contract(cls, value: Any) -> dict[str, Any]:
        """Validate the router's per-turn visible-language decision.

        This is deliberately a small open contract rather than an enum: the bot
        serves international guests, so accepting only a fixed language list would
        turn every unlisted language back into English.
        """
        if not isinstance(value, dict):
            return {}
        language = re.sub(r"\s+", " ", str(value.get("language") or "")).strip()
        script = re.sub(r"\s+", " ", str(value.get("script") or "")).strip()
        if not language or not script:
            return {}
        language_aliases = {
            "bn": "Bengali",
            "en": "English",
            "gu": "Gujarati",
            "hi": "Hindi",
            "kn": "Kannada",
            "ml": "Malayalam",
            "mr": "Marathi",
            "pa": "Punjabi",
            "ta": "Tamil",
            "te": "Telugu",
            "ur": "Urdu",
        }
        language = language_aliases.get(language.lower(), language)
        style = str(value.get("style") or "native").strip().lower()
        if style not in {"native", "romanized", "mixed"}:
            style = "native"
        try:
            confidence = max(0.0, min(1.0, float(value.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        result: dict[str, Any] = {
            "language": language[:60],
            "script": script[:60],
            "style": style,
            "confidence": confidence,
        }
        source = str(value.get("source") or "").strip()
        if source:
            result["source"] = source[:80]
        return result

    @classmethod
    def _last_assistant_response_language(
        cls,
        context: ConversationContext,
    ) -> dict[str, Any]:
        for message in reversed(list(getattr(context, "messages", None) or [])):
            role = getattr(message, "role", "")
            if str(getattr(role, "value", role) or "").strip().lower() != "assistant":
                continue
            metadata = getattr(message, "metadata", None)
            if not isinstance(metadata, dict):
                return {}
            return cls._normalize_response_language_contract(
                metadata.get("response_language")
            )
        return {}

    @staticmethod
    def _history_for_explicit_language_switch(
        *,
        history_rows: list[dict[str, str]] | None,
        last_assistant_message: str,
        current_user_message: str,
        language_switch: bool,
    ) -> tuple[list[dict[str, str]], str, bool]:
        rows = [dict(row) for row in (history_rows or []) if isinstance(row, dict)]
        # Short requests such as "say that in Kannada" need the preceding reply
        # as their subject. A full sentence already carries its own subject, so
        # the immediately preceding assistant wording is harmful language bias,
        # not required context. The stored conversation remains untouched.
        current_word_count = len(re.findall(r"[\w'-]+", str(current_user_message or "")))
        suppress_previous_reply = bool(language_switch and current_word_count >= 5)
        if not suppress_previous_reply:
            return rows, str(last_assistant_message or ""), False
        for index in range(len(rows) - 1, -1, -1):
            if str(rows[index].get("role") or "").strip().lower() == "assistant":
                rows.pop(index)
                break
        return rows, "", True

    @classmethod
    def _response_language_preference(
        cls,
        *,
        user_message: str,
        history_rows: list[dict[str, str]] | None,
        last_assistant_message: str = "",
        router_language: Any = None,
    ) -> dict[str, Any]:
        rows = history_rows if isinstance(history_rows, list) else []
        candidate_texts: list[str] = []
        for row in rows[-8:]:
            if not isinstance(row, dict):
                continue
            if str(row.get("role") or "").strip().lower() == "user":
                content = str(row.get("content") or "").strip()
                if content:
                    candidate_texts.append(content)
        current = str(user_message or "").strip()
        if current:
            candidate_texts.append(current)

        # A script visible in the CURRENT message is objective and must beat a
        # stale/incorrect model classification. This is the exact Hindi -> Kannada
        # failure mode: the prior assistant was Hindi, but the new guest turn was
        # visibly Kannada and therefore represents a clear switch.
        current_script_hint = cls._script_language_hint(current)
        if current_script_hint:
            return {
                "language": current_script_hint["language"],
                "script": current_script_hint["script"],
                "style": "native",
                "confidence": 1.0,
                "source": "current_message_script",
            }

        # For Latin-script text the script alone cannot distinguish English from
        # romanized Hindi/Kannada/etc. The main router makes that semantic decision
        # once; every answering lane consumes the same result.
        routed = cls._normalize_response_language_contract(router_language)
        if routed:
            # Some model outputs use script="other" and style="native" even
            # when a normally non-Latin language is visibly transliterated in
            # ASCII. The current message is objective evidence for the rendering
            # mode, so normalize those contracts before any answerer consumes it.
            normally_non_latin_languages = {
                "arabic",
                "bengali",
                "chinese",
                "gujarati",
                "hindi",
                "japanese",
                "kannada",
                "korean",
                "malayalam",
                "marathi",
                "nepali",
                "odia",
                "oriya",
                "punjabi",
                "russian",
                "sinhala",
                "tamil",
                "telugu",
                "thai",
                "urdu",
            }
            language_key = str(routed.get("language") or "").strip().lower()
            has_latin_text = bool(re.search(r"[A-Za-z]", current))
            has_non_ascii_letters = bool(
                re.search(r"[^\x00-\x7F]", re.sub(r"[\d\W_]", "", current))
            )
            if (
                language_key in normally_non_latin_languages
                and has_latin_text
                and not has_non_ascii_letters
            ):
                routed["script"] = "Latin"
                routed["style"] = "romanized"
            routed["source"] = str(routed.get("source") or "main_router")
            return routed

        # Last explicit language request wins. Capture the language name; the
        # model decides the final rendering from the generic preference payload.
        patterns = (
            re.compile(r"\b(?:talk|speak|reply|respond|answer|write|continue|use)\s+(?:to\s+me\s+)?(?:in|with)\s+([a-z][a-z\-\s]{1,30})\b", re.I),
            re.compile(r"\b([a-z][a-z\-\s]{1,30})\s+(?:mein|me)\s+(?:baat|bolo|bol|likho|reply|respond|answer|talk|speak|continue)\b", re.I),
        )
        for text_value in reversed(candidate_texts):
            lowered = text_value.lower()
            if any(stop in lowered for stop in ("switch to english", "reply in english", "respond in english", "talk in english", "speak in english")):
                return {
                    "language": "English",
                    "script": "Latin",
                    "style": "native",
                    "confidence": 1.0,
                    "source": "explicit_language_request",
                }
            for pattern in patterns:
                match = pattern.search(text_value)
                if not match:
                    continue
                language = re.sub(r"\s+", " ", match.group(1)).strip(" .,!?:;").title()
                if not language:
                    continue
                script_hint = cls._script_language_hint(text_value)
                result = {
                    "language": language,
                    "script": script_hint.get("script") or "normal script",
                    "style": "native",
                    "confidence": 1.0,
                    "source": "explicit_language_request",
                }
                return result

        # For bare follow-ups after a non-Latin reply, preserve the established
        # visible language instead of snapping back to English.
        word_count = len(re.findall(r"\w+", current))
        if current and word_count <= 4:
            script_hint = cls._script_language_hint(last_assistant_message)
            if script_hint:
                return {
                    "language": script_hint["language"],
                    "script": script_hint["script"],
                    "style": "native",
                    "confidence": 0.85,
                    "source": "previous_assistant_language_for_short_followup",
                }
        return {}

    async def _detect_response_language_with_llm(
        self,
        *,
        user_message: str,
        context: ConversationContext,
    ) -> dict[str, Any]:
        """Classify language when the full main router is skipped mid-flow."""
        current = str(user_message or "").strip()
        deterministic = self._response_language_preference(
            user_message=current,
            history_rows=self._history_preview(context, max_messages=6, max_chars=1800),
            last_assistant_message=self._last_message_by_role(context, "assistant"),
        )
        if self._script_language_hint(current):
            return deterministic
        try:
            raw = await self._chat_with_json(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a language-and-script router. Return STRICT JSON only with "
                            "language, script, style, confidence. Classify the guest's CURRENT "
                            "message, not the assistant or hotel. style is native, romanized, or "
                            "mixed. Romanized/transliterated text such as 'mujhe ye chahiye' is "
                            "Hindi with Latin script and romanized style, not English. A full clear "
                            "current message switches language; only a language-neutral reply such "
                            "as a bare number, emoji, yes/no, or acknowledgement inherits the most "
                            "recent established guest language from recent_messages."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "current_guest_message": current,
                                "recent_messages": self._history_preview(
                                    context, max_messages=6, max_chars=1800
                                ),
                                "response_contract": {
                                    "language": "language name",
                                    "script": "Latin|Devanagari|Kannada|other script",
                                    "style": "native|romanized|mixed",
                                    "confidence": "0..1",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                model=self._role_model("main_router"),
                temperature=0.0,
                max_tokens=100,
                trace_context={
                    "responder_type": "main",
                    "agent": "language_router",
                    "session_id": str(getattr(context, "session_id", "")),
                },
            )
        except Exception:
            return deterministic
        return self._response_language_preference(
            user_message=current,
            history_rows=self._history_preview(context, max_messages=6, max_chars=1800),
            last_assistant_message=self._last_message_by_role(context, "assistant"),
            router_language=raw,
        )

    @classmethod
    def _safe_parse_decision(
        cls,
        raw: Any,
        *,
        user_message: str = "",
        service_id: str = "",
    ) -> OrchestrationDecision:
        if not isinstance(raw, dict):
            raw = {}
        # Pass 1 — validate as-is (the happy path).
        try:
            return OrchestrationDecision.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "decision parse: model_validate failed (salvaging): %s | keys=%s",
                exc, list(raw.keys())[:20] if isinstance(raw, dict) else "n/a",
            )
        # Pass 2 — sanitize the usual offenders and re-validate. The model's actual
        # answer is almost always fine; typically ONE stray field trips validation
        # (out-of-range `confidence`, a non-dict `metadata`/`ticket`, etc.). Don't
        # discard a perfectly good reply over that.
        try:
            safe = dict(raw)
            try:
                safe["confidence"] = min(1.0, max(0.0, float(safe.get("confidence"))))
            except (TypeError, ValueError):
                safe.pop("confidence", None)
            for _k in ("pending_data_updates", "metadata", "ticket"):
                if _k in safe and not isinstance(safe[_k], dict):
                    safe.pop(_k, None)
            for _k in (
                "missing_fields", "blocking_fields", "deferrable_fields",
                "suggested_actions", "selected_property_ids",
            ):
                if _k in safe and not isinstance(safe[_k], (list, str)):
                    safe.pop(_k, None)
            return OrchestrationDecision.model_validate(safe)
        except Exception as exc:
            logger.warning("decision parse: sanitized re-validate failed: %s", exc)
        # Pass 3 — preserve the model's REAL reply text if it produced one. Never
        # throw away a good answer just because an auxiliary field was malformed.
        salvaged_text = ""
        if isinstance(raw, dict):
            salvaged_text = str(
                raw.get("response_text") or raw.get("message") or raw.get("answer") or ""
            ).strip()
        if salvaged_text:
            return OrchestrationDecision(
                intent=str(raw.get("intent") or "faq"),
                action="respond_only",
                response_text=salvaged_text,
                confidence=0.5,
                metadata={"safe_parse_salvaged": True},
            )
        # Pass 4 — booking-aware fallback: for a clear room-booking intent we must
        # NEVER emit the generic clarification. Send the guest to the website.
        _svc = str(service_id or "").lower()
        _msg = str(user_message or "").lower()
        booking_intent = (
            "room_booking" in _svc
            or "book_a_stay" in _svc
            or any(p in _msg for p in (
                "book a room", "book room", "booking a room", "i want to book",
                "wanna book", "make a booking", "reserve a room", "book a stay",
            ))
        )
        if booking_intent:
            return OrchestrationDecision(
                intent="room_booking",
                action="respond_only",
                response_text=(
                    "You can complete your booking on our website — just pick your "
                    "room and dates there, and any current offers apply at checkout."
                ),
                confidence=0.5,
                metadata={"safe_parse_booking_fallback": True},
            )
        # Pass 5 — last-resort generic (non-booking, nothing salvageable).
        return OrchestrationDecision(
            intent="faq",
            action="respond_only",
            response_text="I need one clarification to proceed accurately. Could you share a bit more detail?",
            confidence=0.45,
        )

    def _apply_answer_priority_fields(self, decision: OrchestrationDecision) -> OrchestrationDecision:
        """
        Normalize missing field semantics:
        - missing_fields == blocking_fields
        - deferrable_fields are kept for later and must not force immediate collect_info
        """
        blocking_fields = self._normalize_field_list(
            decision.blocking_fields if decision.blocking_fields else decision.missing_fields
        )
        deferrable_fields = self._normalize_field_list(decision.deferrable_fields)
        if blocking_fields:
            decision.blocking_fields = blocking_fields
            decision.missing_fields = list(blocking_fields)
        else:
            decision.blocking_fields = []
            decision.missing_fields = []
        decision.deferrable_fields = deferrable_fields
        return decision

    @staticmethod
    def _count_bullet_lines(text: str) -> int:
        lines = str(text or "").splitlines()
        count = 0
        for line in lines:
            if re.match(r"^\s*[-*]\s+\S+", str(line or "")):
                count += 1
        return count

    @staticmethod
    def _looks_like_food_or_dining_service(service: dict[str, Any] | None) -> bool:
        if not isinstance(service, dict):
            return False
        combined = " ".join(
            [
                str(service.get("id") or ""),
                str(service.get("name") or ""),
                str(service.get("type") or ""),
                str(service.get("profile") or ""),
                str(service.get("description") or ""),
                str(service.get("cuisine") or ""),
            ]
        ).lower().replace("_", " ")
        if not combined:
            return False
        markers = (
            "dining",
            "food",
            "restaurant",
            "menu",
            "meal",
            "beverage",
            "breakfast",
            "lunch",
            "dinner",
            "room service",
            "in room dining",
            "order",
        )
        return any(marker in combined for marker in markers)

    @staticmethod
    def _extract_food_order_values(pending_data: dict[str, Any]) -> list[str]:
        if not isinstance(pending_data, dict):
            return []
        key_markers = (
            "meal",
            "breakfast",
            "lunch",
            "dinner",
            "beverage",
            "drink",
            "food",
            "dish",
            "item",
            "order",
            "snack",
            "kid",
        )
        excluded_exact = {
            "service_id",
            "service_name",
            "guest_id",
            "guest_name",
            "guest_phone",
            "room_number",
            "check_in",
            "check_out",
            "stay_checkin_date",
            "stay_checkout_date",
            "stay_date_range",
            "booking_ref",
        }
        excluded_values = {"yes", "no", "true", "false", "none", "na", "n/a"}
        values: list[str] = []
        seen: set[str] = set()
        for raw_key, raw_value in pending_data.items():
            key = str(raw_key or "").strip().lower()
            if not key or key.startswith("_") or key in excluded_exact:
                continue
            if not any(marker in key for marker in key_markers):
                continue
            value_text = str(raw_value or "").strip()
            if not value_text:
                continue
            for part in re.split(r"[\n,]+", value_text):
                candidate = re.sub(r"\s+", " ", str(part or "").strip(" ."))
                if not candidate:
                    continue
                candidate_norm = candidate.lower()
                if candidate_norm in excluded_values:
                    continue
                if re.fullmatch(r"\d+", candidate_norm):
                    continue
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate_norm):
                    continue
                if candidate_norm in seen:
                    continue
                seen.add(candidate_norm)
                values.append(candidate)
                if len(values) >= 10:
                    return values
        return values

    @staticmethod
    def _response_mentions_value(response_text: str, value: str) -> bool:
        response_norm = re.sub(r"[^a-z0-9]+", " ", str(response_text or "").lower())
        value_norm = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
        if not response_norm or not value_norm:
            return False
        if value_norm in response_norm:
            return True
        tokens = [tok for tok in value_norm.split() if len(tok) >= 3][:5]
        if not tokens:
            return False
        return all(tok in response_norm for tok in tokens)

    def _build_answer_first_knowledge_base(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        target_service: dict[str, Any] | None = None,
        full_kb_text: str = "",
    ) -> tuple[str, str]:
        service_knowledge = ""
        if isinstance(target_service, dict):
            prompt_pack = target_service.get("service_prompt_pack")
            if not isinstance(prompt_pack, dict):
                prompt_pack = {}
            service_knowledge = str(
                target_service.get("extracted_knowledge")
                or prompt_pack.get("extracted_knowledge")
                or ""
            ).strip()
            if service_knowledge:
                try:
                    service_knowledge = self._scope_service_knowledge_to_runtime_property(
                        service_knowledge=service_knowledge,
                        user_message=user_message,
                        context=context,
                    )
                except Exception:
                    pass

        sections: list[str] = []
        if service_knowledge:
            sections.append("=== SERVICE KNOWLEDGE ===\n" + service_knowledge)
        hotel_knowledge = str(full_kb_text or "").strip()
        if hotel_knowledge:
            sections.append("=== HOTEL KNOWLEDGE ===\n" + hotel_knowledge)
        return "\n\n".join(section for section in sections if section).strip(), service_knowledge


    async def _enforce_answer_first_policy(
        self,
        *,
        user_message: str,
        decision: OrchestrationDecision,
        context: ConversationContext,
        selected_phase_id: str,
        selected_phase_name: str,
        target_service: dict[str, Any] | None = None,
        full_kb_text: str = "",
    ) -> OrchestrationDecision:
        decision = self._apply_answer_priority_fields(decision)
        pre_guard_response_text = str(decision.response_text or "").strip()

        # Latency gate (structural — no keyword matching):
        # Skip the guard's LLM call (~2-4s) on book-route responses that
        # already look complete. The guard's value is mainly catching
        # service-agent confusion or truncated answers. When the response
        # has bullets, reasonable length, the answerer marked it as
        # answering the query, and there's no pending slot collection,
        # the guard rarely rewrites anything — pure latency cost.
        # All gating uses STRUCTURAL fields the answerer LLM itself emits:
        # response length, bullet count, action, pending_action,
        # answered_current_query. No keyword/regex matching on the text.
        if target_service is None and self._book_response_looks_complete(decision):
            decision.metadata.setdefault("answer_first_guard_skipped", True)
            decision.metadata.setdefault(
                "answer_first_guard_skip_reason", "structural_book_response_complete"
            )
            return decision

        guard_raw = await self._run_answer_first_guard(
            user_message=user_message,
            decision=decision,
            context=context,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            target_service=target_service,
            full_kb_text=full_kb_text,
        )
        if not guard_raw:
            return decision

        answers_current_query = self._coerce_bool(
            guard_raw.get("answers_current_query"),
            default=bool(decision.answered_current_query),
        )
        blocking_fields = self._normalize_field_list(guard_raw.get("blocking_fields"))
        deferrable_fields = self._normalize_field_list(guard_raw.get("deferrable_fields"))
        revised_response_text = str(guard_raw.get("revised_response_text") or "").strip()
        recommended_action = self._normalize_identifier(guard_raw.get("recommended_action"))
        recommended_pending_action_raw = guard_raw.get("recommended_pending_action")
        recommended_pending_action = (
            self._normalize_identifier(recommended_pending_action_raw)
            if recommended_pending_action_raw is not None
            else ""
        )
        followup_question = str(guard_raw.get("followup_question") or "").strip()

        decision.answered_current_query = answers_current_query
        if revised_response_text:
            decision.response_text = revised_response_text
        decision.blocking_fields = list(blocking_fields)
        decision.missing_fields = list(blocking_fields)
        if deferrable_fields:
            decision.deferrable_fields = list(deferrable_fields)
        elif "deferrable_fields" in guard_raw:
            decision.deferrable_fields = []
        if followup_question:
            decision.followup_question = followup_question

        if recommended_action in {
            "respond_only",
            "collect_info",
            "dispatch_handler",
            "create_ticket",
            "resume_pending",
            "cancel_pending",
        }:
            decision.action = recommended_action

        if recommended_pending_action_raw is not None:
            decision.pending_action = recommended_pending_action or None

        decision = self._apply_answer_priority_fields(decision)
        rewrite_reverted_reason = ""

        if pre_guard_response_text and str(decision.response_text or "").strip():
            before_bullets = self._count_bullet_lines(pre_guard_response_text)
            after_bullets = self._count_bullet_lines(decision.response_text)
            if before_bullets >= 2 and after_bullets < before_bullets:
                decision.response_text = pre_guard_response_text
                rewrite_reverted_reason = "list_item_loss"

        if self._looks_like_food_or_dining_service(target_service):
            merged_pending = dict(self._coerce_public_pending(context.pending_data))
            if isinstance(decision.pending_data_updates, dict):
                for key, value in decision.pending_data_updates.items():
                    if str(value or "").strip():
                        merged_pending[str(key)] = value
            order_values = self._extract_food_order_values(merged_pending)
            if len(order_values) >= 2:
                missing_after = [
                    item for item in order_values
                    if not self._response_mentions_value(str(decision.response_text or ""), item)
                ]
                if missing_after:
                    missing_before = [
                        item for item in order_values
                        if not self._response_mentions_value(pre_guard_response_text, item)
                    ]
                    if len(missing_before) < len(missing_after) and pre_guard_response_text:
                        decision.response_text = pre_guard_response_text
                        rewrite_reverted_reason = "food_order_item_loss"
                        decision.metadata["answer_first_guard_missing_order_items"] = missing_after[:6]

        decision.metadata.setdefault("answer_first_guard_applied", True)
        decision.metadata["answer_first_guard_answers_current_query"] = answers_current_query
        decision.metadata["answer_first_guard_blocking_fields"] = list(decision.blocking_fields or [])
        decision.metadata["answer_first_guard_deferrable_fields"] = list(decision.deferrable_fields or [])
        decision.metadata["answer_first_guard_recommended_action"] = recommended_action
        decision.metadata["answer_first_guard_recommended_pending_action"] = recommended_pending_action or ""
        decision.metadata["answer_first_guard_reason"] = str(guard_raw.get("reason") or "").strip()
        if rewrite_reverted_reason:
            decision.metadata["answer_first_guard_rewrite_reverted"] = rewrite_reverted_reason
        return decision

    async def _resolve_target_service_with_llm(
        self,
        *,
        user_message: str,
        selected_phase_id: str,
        selected_phase_name: str,
        services_snapshot: list[dict[str, Any]],
        context: Any = None,
    ) -> tuple[str, str]:
        if not str(settings.openai_api_key or "").strip():
            return "", ""
        in_phase_services = [
            {
                "id": self._normalize_identifier(item.get("id")),
                "name": str(item.get("name") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "what_it_handles": str(item.get("description") or "").strip(),
                "routing_keywords": item.get("routing_keywords", []),
                "profile": str(item.get("profile") or "").strip(),
                "phase_id": self._normalize_identifier(item.get("phase_id")),
            }
            for item in services_snapshot
            if isinstance(item, dict)
            and bool(item.get("is_active", True))
            and self._normalize_identifier(item.get("phase_id")) == selected_phase_id
        ]
        if not in_phase_services:
            return "", ""

        last_active_service: dict[str, Any] = {}
        if context is not None and isinstance(getattr(context, "pending_data", None), dict):
            last_service_id = self._normalize_identifier(
                (context.pending_data or {}).get("_last_service_id")
            )
            if last_service_id:
                for item in in_phase_services:
                    if self._normalize_identifier(item.get("id")) != last_service_id:
                        continue
                    last_active_service = {
                        "id": last_service_id,
                        "name": str(item.get("name") or last_service_id).strip(),
                        "description": str(item.get("description") or "").strip(),
                    }
                    break

        # Build a plain-prose summary of each available service for the router LLM
        service_guide_lines = ["AVAILABLE SERVICES IN CURRENT PHASE:"]
        for svc in in_phase_services:
            _sid = svc.get("id", "")
            _sname = svc.get("name", _sid)
            _sdesc = svc.get("description", "")
            _skw = ", ".join(svc.get("routing_keywords", []))
            service_guide_lines.append(
                f"- {_sname} (id: {_sid})"
                + (f": {_sdesc}" if _sdesc else "")
                + (f" | triggers: {_skw}" if _skw else "")
            )
        service_guide = "\n".join(service_guide_lines)

        # Give the router the immediately previous assistant turn so it can resolve
        # context-dependent messages — above all a BARE booking verb ("book it",
        # "book", "yes book") whose object lives in the prior turn, not in
        # `last_active_service`. Without this the router is blind to the topic and
        # force-fits a bare verb onto whatever service was last active (e.g. snapping
        # "book it" right after a VENUE answer back into room booking).
        last_assistant_message = (
            self._last_message_by_role(context, "assistant") if context is not None else ""
        )

        prompt_payload = {
            "user_message": str(user_message or "").strip(),
            "last_assistant_message": str(last_assistant_message or "").strip(),
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "in_phase_services": in_phase_services,
            "last_active_service": last_active_service,
            "response_contract": {
                "is_service_request": "bool",
                "service_id": "exact id from in_phase_services when service request else empty string",
                "action_hint": "respond_only|collect_info|dispatch_handler|create_ticket",
                "ambiguous": "bool — true if two or more services are equally plausible",
                "reason": "short reason for the routing decision",
            },
        }
        # Multi-model: routing runs on 4.1 and loads 4.1's service-router prompt.
        model = self._role_model("service_router")
        try:
            system_prompt = await prompt_registry.get(
                "orchestrator.service_router",
                {"service_guide": service_guide},
                model=model,
            )
        except PromptMissingError:
            logger.exception("orchestrator_service_router_prompt_missing")
            return "", ""
        system_prompt = (
            str(system_prompt or "").rstrip()
            + "\n\n=== SERVICE CONTINUITY ===\n"
            + "If `last_active_service` is present, keep it ONLY when the guest is clearly continuing that transaction right now.\n"
            + "Continuation signals include slot values, choosing an option, asking to proceed, confirming, cancelling, or explicit execution intent.\n"
            + "Pure informational questions do NOT stay in the service by default.\n"
        )
        if context:
            _router_bk = self._build_booking_context_prompt_block(context)
            if _router_bk:
                system_prompt = system_prompt + "\n" + _router_bk
        # `model` was resolved above (self._role_model("service_router")).
        service_router_max_tokens = max(
            100,
            int(getattr(settings, "chat_llm_service_router_max_tokens", 320) or 320),
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
                ],
                model=model,
                temperature=0.0,
                max_tokens=service_router_max_tokens,
                trace_context={
                    "responder_type": "main",
                    "agent": "service_router",
                    "selected_phase_id": selected_phase_id,
                    "selected_phase_name": selected_phase_name,
                },
            )
        except Exception:
            return "", ""
        _log_llm_call(
            label="SERVICE ROUTER RESPONSE",
            session_id="",
            user_message=user_message,
            system_prompt=system_prompt,
            payload=prompt_payload,
            response=raw if isinstance(raw, dict) else {},
        )
        if not isinstance(raw, dict):
            return "", ""
        service_request_raw = raw.get("is_service_request")
        service_request = False
        if isinstance(service_request_raw, bool):
            service_request = service_request_raw
        elif str(service_request_raw or "").strip().lower() in {"true", "1", "yes", "y"}:
            service_request = True
        if not service_request:
            return "", str(raw.get("action_hint") or "").strip().lower()
        candidate = self._normalize_identifier(raw.get("service_id"))
        if not candidate:
            return "", str(raw.get("action_hint") or "").strip().lower()
        valid_ids = {self._normalize_identifier(item.get("id")) for item in in_phase_services}
        if candidate not in valid_ids:
            return "", str(raw.get("action_hint") or "").strip().lower()
        return candidate, str(raw.get("action_hint") or "").strip().lower()

    @staticmethod
    def _normalize_main_router_route(value: Any) -> str:
        route = str(value or "").strip().lower().replace(" ", "_")
        aliases = {
            "services": "service",
            "service_route": "service",
            "kb": "book",
            "books": "book",
            "book_route": "book",
            "knowledge": "book",
            "clarification": "clarify",
            "question": "clarify",
        }
        route = aliases.get(route, route)
        if route in {"service", "book", "clarify", "auto"}:
            return route
        return "auto"

    async def _resolve_main_route_with_llm(
        self,
        *,
        user_message: str,
        selected_phase_id: str,
        selected_phase_name: str,
        services_snapshot: list[dict[str, Any]],
        context: ConversationContext,
        language_user_message: str = "",
    ) -> dict[str, Any]:
        language_input = str(language_user_message or user_message or "").strip()
        fallback = {
            "route": "auto",
            "service_id": "",
            "book_ids": [],
            "clarification_question": "",
            "reason": "",
            "confidence": 0.0,
            "response_language": self._response_language_preference(
                user_message=language_input,
                history_rows=self._history_preview(context, max_messages=8, max_chars=2400),
                last_assistant_message=self._last_message_by_role(context, "assistant"),
            ),
        }
        if not bool(getattr(settings, "chat_llm_main_router_enabled", True)):
            return fallback
        if not str(settings.openai_api_key or "").strip():
            return fallback
        if str(context.pending_action or "").strip():
            # The full routing call is intentionally skipped during an active
            # service flow. Run only the tiny language-router contract so the
            # service agent still receives an authoritative current-turn language,
            # including romanized text that Unicode script checks cannot identify.
            fallback["response_language"] = await self._detect_response_language_with_llm(
                user_message=language_input,
                context=context,
            )
            # Keep active flows stable; router is for fresh message routing.
            #
            # But "stable" has to mean the flow's OWN agent keeps answering — not
            # that nobody does. Returning the neutral fallback here left route=auto
            # with no service_id, so the turn fell through to the generic
            # orchestrator, which loads no service prompt pack and no menu. A guest
            # mid food-order who asked "what about wine" was told a red wine had
            # been added to their order — four minutes after the in-room dining
            # agent, holding the real menu, had correctly answered that there is no
            # wine on it. The grounded agent was never asked.
            #
            # So hand the turn to the service that owns the pending flow and let
            # that agent judge it against its own knowledge. It is not trapped
            # there: a question about a different part of the hotel is exactly what
            # `relevant_to_service=false` / `context_switched=true` is for, and the
            # orchestrator re-routes on that.
            _pending_service_id = self._normalize_identifier(
                (context.pending_data or {}).get("_last_service_id")
                if isinstance(context.pending_data, dict)
                else ""
            )
            _pending_service_live = _pending_service_id and any(
                isinstance(item, dict)
                and bool(item.get("is_active", True))
                and self._normalize_identifier(item.get("id")) == _pending_service_id
                and self._normalize_identifier(item.get("phase_id")) == selected_phase_id
                for item in services_snapshot
            )
            if _pending_service_live:
                return {
                    **fallback,
                    "route": "service",
                    "service_id": _pending_service_id,
                    "reason": "pending flow is owned by this service",
                }
            return fallback

        in_phase_services = [
            {
                "id": self._normalize_identifier(item.get("id")),
                "name": str(item.get("name") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "phase_id": self._normalize_identifier(item.get("phase_id")),
                "routing_keywords": list(item.get("routing_keywords") or []),
            }
            for item in services_snapshot
            if isinstance(item, dict)
            and bool(item.get("is_active", True))
            and self._normalize_identifier(item.get("phase_id")) == selected_phase_id
            and self._normalize_identifier(item.get("id"))
        ]

        books_enabled = bool(getattr(settings, "chat_llm_book_routing_enabled", True))
        _active_prop_slugs: list[str] = []
        _hotel_code = str(getattr(context, "hotel_code", "") or "").strip()
        try:
            from services.kb_table_reader import get_property_catalog_db

            property_catalog = await get_property_catalog_db(hotel_code=_hotel_code)
        except Exception:
            property_catalog = []

        _router_available_properties, _router_available_locations = self._build_available_property_scope(
            property_catalog
        )
        if isinstance(context.pending_data, dict) and _router_available_properties:
            context.pending_data["_property_name_map"] = self._merge_property_name_map(
                context.pending_data.get("_property_name_map"),
                _router_available_properties,
            )
            # Stash a compact city → hotels map so the book answerer can answer a
            # COVERAGE question ('what cities do you have') completely, even when
            # only one property is active and the loaded books truncate. This is
            # DATA (the brand's full footprint), not loaded KB content.
            try:
                _loc_compact: list[dict[str, Any]] = []
                for _loc in (_router_available_locations or []):
                    if not isinstance(_loc, dict):
                        continue
                    _label = str(_loc.get("label") or _loc.get("city") or "").strip()
                    _names = [str(n).strip() for n in (_loc.get("property_names") or []) if str(n).strip()]
                    if _label:
                        _loc_compact.append({"city": _label, "hotels": _names})
                if _loc_compact:
                    context.pending_data["_all_locations_compact"] = _loc_compact
            except Exception:
                pass

        _active_prop_slugs = [
            str(slug).strip()
            for slug in (
                list((context.pending_data or {}).get("_active_property_ids") or [])
                if context.pending_data
                else []
            )
            if str(slug).strip()
        ]

        # ── MULTI-PROPERTY LOCATION SEED ───────────────────────────────────
        # When no property scope is locked yet (first turns) and the guest has
        # already chosen a location (dropdown / opening form), activate EVERY
        # property in that location so the answer covers all of them instead of
        # the model silently picking one. The router then narrows by the
        # conversation on later turns. No-op unless the location maps to 2+
        # properties, so single-property locations and hotels are untouched.
        if not _active_prop_slugs and isinstance(context.pending_data, dict):
            _seed_loc = self._normalize_identifier(
                context.pending_data.get("_session_location_label")
            )
            if _seed_loc and len(_seed_loc) >= 3:
                _seed_ids: list[str] = []
                for _loc in (_router_available_locations or []):
                    if not isinstance(_loc, dict):
                        continue
                    _id_norm = self._normalize_identifier(_loc.get("id"))
                    _label_norm = self._normalize_identifier(_loc.get("label"))
                    _match = (
                        _seed_loc in (_id_norm, _label_norm)
                        or (_label_norm and (_seed_loc in _label_norm or _label_norm in _seed_loc))
                        or (_id_norm and (_seed_loc in _id_norm or _id_norm in _seed_loc))
                    )
                    if not _match:
                        continue
                    for _pid in (_loc.get("property_ids") or []):
                        _pid_norm = self._normalize_identifier(_pid)
                        if _pid_norm and _pid_norm not in _seed_ids:
                            _seed_ids.append(_pid_norm)
                if len(_seed_ids) >= 2:
                    _active_prop_slugs = _seed_ids
                    context.pending_data["_active_property_ids"] = list(_seed_ids)
                # Seed only once: consume the label so a later explicit broaden
                # ("show me other locations", which wipes scope) is NOT snapped
                # back to this location. The router owns scope from here on.
                context.pending_data.pop("_session_location_label", None)

        # Show the router EVERY property's books — each property has its own set,
        # often named differently (pet_policies vs pet_friendly_policies vs
        # hotel_policies_and_rules). Handing the router the FULL per-property
        # catalog lets it pick the relevant book FROM EACH property in scope for a
        # cross-property question ("which hotel is pet friendly"), instead of only
        # seeing the one active hotel's books and picking blind for the rest.
        # Property isolation is preserved: each catalog row carries its
        # property_id/property_name, and the loader only loads (picked slug ×
        # SELECTED property) pairs — so a single-property turn still answers about
        # just that hotel. The KB char budget is sized for the large context
        # window so the picked books actually fit.
        if books_enabled:
            from services.kb_table_reader import get_structured_book_catalog_db

            book_catalog = await get_structured_book_catalog_db(
                hotel_code=_hotel_code,
                property_slugs=None,
                only_active=True,
            )
        else:
            book_catalog = []
        if isinstance(book_catalog, list) and book_catalog:
            # The router needs every book's description so it can pick the
            # right one for the current QUESTION. We never cap the catalog
            # below the actual book count — capping silently dropped books
            # like `pool_timings` (position 36+) and made some questions
            # unanswerable. The router model handles ~60-100 short rows
            # fine; only fall back to a cap if the count is huge.
            _router_max_books = max(
                len(book_catalog),
                int(getattr(settings, "chat_main_router_max_books", 200) or 200),
            )
            _router_desc_chars = max(
                80,
                int(getattr(settings, "chat_main_router_book_desc_chars", 120) or 120),
            )
            _router_max_aliases = max(
                0,
                int(getattr(settings, "chat_main_router_book_max_aliases", 2) or 2),
            )
            _router_max_keywords = max(
                0,
                int(getattr(settings, "chat_main_router_book_max_keywords", 4) or 4),
            )
            # Books are now CANONICAL — every property uses the same ~13 slugs.
            # So the catalog collapses to one row per distinct slug (the menu of
            # book TYPES). The router picks a slug + the properties; the loader
            # loads (slug × selected properties). This keeps the router menu tiny
            # and fast while still covering every hotel for cross-property asks.
            _compact_catalog: list[dict[str, Any]] = []
            _seen_catalog_slugs: set[str] = set()
            for row in book_catalog:
                if not isinstance(row, dict):
                    continue
                _book_id = str(row.get("id") or "").strip()
                if not _book_id:
                    continue
                if _book_id in _seen_catalog_slugs:
                    continue
                _seen_catalog_slugs.add(_book_id)
                _row_pid = self._normalize_identifier(row.get("property_id"))
                _desc = str(row.get("description") or "").strip()
                if len(_desc) > _router_desc_chars:
                    _desc = _desc[:_router_desc_chars].rstrip() + "..."
                _aliases = [
                    str(item).strip()
                    for item in list(row.get("aliases") or [])[:_router_max_aliases]
                    if str(item).strip()
                ]
                _keywords = [
                    str(item).strip()
                    for item in list(row.get("keywords") or [])[:_router_max_keywords]
                    if str(item).strip()
                ]
                _compact_catalog.append(
                    {
                        "id": _book_id,
                        "title": str(row.get("title") or _book_id).strip(),
                        "description": _desc,
                        "aliases": _aliases,
                        "keywords": _keywords,
                        "page_count": int(row.get("page_count") or 0),
                        # Canonical books apply to EVERY property (all hotels share
                        # the same ~13 slugs). Keeping a single property_id here is
                        # misleading — the compaction keeps whichever property was
                        # seen FIRST for a slug, so a fleet question ("which hotels
                        # allow pets") anchored to that one property's location
                        # (always the same, e.g. a Goa hotel). Leave it neutral; the
                        # router selects properties from available_properties, not
                        # from a book's stray first-seen owner.
                        "property_id": "",
                        "property_name": "",
                    }
                )
                if len(_compact_catalog) >= _router_max_books:
                    break
            book_catalog = _compact_catalog
        if not in_phase_services and not book_catalog and not _router_available_properties and not _router_available_locations:
            return fallback

        last_assistant_message = self._last_message_by_role(context, "assistant")
        last_user_message = self._last_message_by_role(context, "user")
        if not _router_available_properties:
            _seen_router_pids: set[str] = set()
            for _row in (book_catalog or []):
                if not isinstance(_row, dict):
                    continue
                _pid = self._normalize_identifier(_row.get("property_id"))
                if not _pid or _pid in _seen_router_pids:
                    continue
                _seen_router_pids.add(_pid)
                _router_available_properties.append(
                    {
                        "id": _pid,
                        "name": str(_row.get("property_name") or _pid).strip() or _pid,
                        "aliases": [],
                        "location": "",
                    }
                )
            if isinstance(context.pending_data, dict) and _router_available_properties:
                context.pending_data["_property_name_map"] = self._merge_property_name_map(
                    context.pending_data.get("_property_name_map"),
                    _router_available_properties,
                )
        # Past property context lets the router add a previously-discussed
        # property back into focus when the guest references it ("the rishikesh
        # one we talked about earlier").
        _past_property_context = (
            dict((context.pending_data or {}).get("_past_property_context") or {})
            if context.pending_data
            else {}
        )
        # ── ISOLATION SAFETY-NET (defense-in-depth on top of the conversation
        # rotation reset) ────────────────────────────────────────────────────
        # `past_property_context` is what lets the router offer a "previously
        # discussed" hotel. It is supposed to be wiped when a new conversation
        # starts (reset_pending_for_conversation_rotation), but that reset only
        # fires when the frontend actually sends a fresh conversation_id. If that
        # transmission ever regresses (e.g. a stale widget bundle), scope
        # accumulates across conversations and a hotel from a DAYS-OLD chat leaks
        # in as "previously discussed" (observed: Coimbatore surfacing in a
        # Mussoorie conversation). Guard against it independently of the client:
        # keep only past hotels that actually appear in the CURRENT (recent)
        # conversation history; drop the stale ones. When rotation works this is a
        # no-op (past already holds only this conversation's hotels).
        if _past_property_context:
            try:
                _recent_parts: list[str] = []
                for _m in list(getattr(context, "messages", []) or [])[-50:]:
                    _c = getattr(_m, "content", "") or ""
                    if isinstance(_c, str) and _c.strip():
                        _recent_parts.append(_c)
                _recent_text = " ".join(_recent_parts).lower()
                if _recent_text:
                    _kept: dict[str, Any] = {}
                    _dropped: list[str] = []
                    for _pk, _pv in _past_property_context.items():
                        _pname = ""
                        if isinstance(_pv, str) and ":" in _pv:
                            _pname = _pv.split(":", 1)[1].strip()
                        _cands = [
                            str(_pname or "").strip().lower(),
                            str(_pk or "").replace("_", " ").strip().lower(),
                        ]
                        _cands = [c for c in _cands if len(c) >= 4]
                        # keep if the hotel is mentioned in the recent conversation,
                        # or we cannot evaluate it (no usable name → conservative keep)
                        if (not _cands) or any(c in _recent_text for c in _cands):
                            _kept[_pk] = _pv
                        else:
                            _dropped.append(_pk)
                    if _dropped:
                        _past_property_context = _kept
                        if isinstance(context.pending_data, dict):
                            context.pending_data["_past_property_context"] = dict(_kept)
                        logger.warning(
                            "[ISOLATION_SAFETYNET] dropped %d stale past_property_context "
                            "entr%s absent from the current conversation: %s (session=%s) — "
                            "conversation_id likely did not rotate; verify the widget is "
                            "sending a fresh conversation_id.",
                            len(_dropped),
                            "y" if len(_dropped) == 1 else "ies",
                            _dropped,
                            getattr(context, "session_id", ""),
                        )
            except Exception:
                pass
        _last_service_id = self._normalize_identifier(
            (context.pending_data or {}).get("_last_service_id")
            if isinstance(context.pending_data, dict)
            else ""
        )
        _last_active_service: dict[str, Any] = {}
        if _last_service_id:
            for _service in in_phase_services:
                if not isinstance(_service, dict):
                    continue
                if self._normalize_identifier(_service.get("id")) != _last_service_id:
                    continue
                _last_active_service = {
                    "id": _last_service_id,
                    "name": str(_service.get("name") or _last_service_id).strip(),
                    "description": str(_service.get("description") or "").strip(),
                }
                break
        # Consume the transient chip-click hint set by chat_service, then
        # clear it immediately so it does not leak into subsequent turns.
        is_chip_click_followup = False
        if isinstance(context.pending_data, dict):
            is_chip_click_followup = bool(context.pending_data.pop("_chip_click_hint", False))

        # Surface the IMMEDIATE prior assistant turn's service-id metadata so
        # the router can reason about service continuity. When the prior turn
        # was generated by an active service agent (not the support-intake),
        # a generic affirmation from the guest should continue that service —
        # the router uses these fields to avoid mis-flipping to support_request.
        _last_assistant_service_id = ""
        _last_assistant_responder_type = ""
        if isinstance(getattr(context, "messages", None), list):
            for _msg in reversed(_current_thread_messages(context)):
                _role = str(getattr(getattr(_msg, "role", None), "value", "") or "").strip().lower()
                if _role != "assistant":
                    continue
                _meta = getattr(_msg, "metadata", None) or {}
                if isinstance(_meta, dict):
                    _last_assistant_service_id = self._normalize_identifier(
                        _meta.get("orchestration_target_service_id")
                        or _meta.get("service_llm_label")
                        or ""
                    )
                    _last_assistant_responder_type = str(_meta.get("responder_type") or "").strip().lower()
                break
        # Heuristic: prior turn was an active SERVICE AGENT (not support, not the
        # book answerer / main orchestrator) when service_id is set and doesn't
        # look like a support-intake. The router prompt is told to use this
        # field directly — no Python decision-making, just signal plumbing.
        _support_tokens = ("support", "issue", "complaint", "request", "report",
                           "help", "raise", "contact_staff", "callback")
        _id_lc = _last_assistant_service_id.lower()
        _last_assistant_was_service_agent = bool(
            _last_assistant_service_id
            and not any(tok in _id_lc for tok in _support_tokens)
        )

        # ── Maintain the ordered single-hotel "visit" list in PENDING ONLY ──────
        # Used exclusively by the BOOK ANSWERER's recall directive. It is
        # deliberately NOT passed to the router: feeding the router an extra
        # property list biased its active-scope / bare-verb resolution (observed —
        # with a prior booking, a bare 'book it' flipped the active property to the
        # earlier hotel). The router keeps its existing active-property handling
        # untouched. Multi-property (summary/compare) turns do NOT append, so the
        # order stays truthful. Cleared on conversation rotation (fresh per convo).
        _memory_summary = ""
        if isinstance(context.pending_data, dict):
            _memory_summary = str(
                ((context.pending_data.get("_memory") or {}).get("summary") if isinstance(context.pending_data.get("_memory"), dict) else "")
                or ""
            ).strip()
        _past_property_context, _discussed_properties_in_order = self._reconcile_discussed_property_state(
            context=context,
            available_properties=_router_available_properties,
            active_property_ids=_active_prop_slugs,
            past_property_context=_past_property_context,
            memory_summary=_memory_summary,
        )

        # RECENT DIALOGUE (compact) — the last few turns, so the router can resolve
        # references that point a couple turns back: ordinals ('the 2nd hotel',
        # 'the third one'), 'the other hotel', 'the one you mentioned earlier'.
        # Trimmed per message to keep the router input small (stays fast). The
        # single last_user/assistant_message below remain for immediate context.
        # Scoped to the CURRENT thread via get_recent_messages. Reading
        # `context.messages` directly took the last 8 messages of the SESSION,
        # so a fresh conversation's router still saw the previous one: asked for
        # staff in a Phalodi chat, the router quoted "want to explore more about
        # Zone Connect by The Park Patna" from the chat before it as the offer
        # being accepted, and filed the ticket under Patna while
        # `active_property_ids` correctly said Phalodi. Same for Mussoorie ->
        # Lansdowne. The thread-scoped accessor already exists and is what the
        # rest of the history path uses.
        _recent_messages: list[dict[str, str]] = []
        try:
            _cur_msg = str(user_message or "").strip()
            for _m in (context.get_recent_messages(8) or []):
                _r = str(getattr(getattr(_m, "role", None), "value", "") or "").strip().lower()
                _c = str(getattr(_m, "content", "") or "").strip()
                if _r not in ("user", "assistant") or not _c:
                    continue
                if _r == "user" and _c == _cur_msg:
                    continue  # skip the current message (already in user_message)
                _recent_messages.append({"role": _r, "content": _c[:900]})
            _recent_messages = _recent_messages[-5:]
        except Exception:
            _recent_messages = []

        payload = {
            "user_message": str(user_message or "").strip(),
            # user_message may contain an English [CONTEXT: ...] wrapper added by
            # orchestration. Language must be classified from the guest's untouched
            # text or that wrapper itself can falsely force English.
            "language_user_message": language_input,
            "recent_messages": _recent_messages,
            "last_user_message": str(last_user_message or "").strip(),
            "last_assistant_message": str(last_assistant_message or "").strip(),
            "last_assistant_service_id": _last_assistant_service_id,
            "last_assistant_was_service_agent": _last_assistant_was_service_agent,
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "in_phase_services": in_phase_services,
            "book_catalog": book_catalog,
            "available_properties": _router_available_properties,
            "available_locations": _router_available_locations,
            "active_property_ids": list(_active_prop_slugs),
            "past_property_context": _past_property_context,
            "discussed_properties_in_order": self._sanitize_json(_discussed_properties_in_order),
            "last_active_service": _last_active_service,
            "is_chip_click_followup": is_chip_click_followup,
            "response_contract": {
                "route": "service|book|clarify|auto",
                "service_id": "exact id when route=service else empty",
                "selected_property_ids": "array of property ids the guest is interested in this turn (the new active set). Preserve property continuity for every route, including service. SINGLE-FOCUS CONTINUITY: if the guest has been focused on ONE specific property across recent turns and now gives an action/booking intent that names NO property (a bare 'book a room', 'give me the booking link', 'reserve it') — even right after a momentary 'compare both' — keep that ONE primary property here; do NOT re-expand to the whole location and force a re-pick (a brief comparison is a detour, not a change of which property they are pursuing). If the guest mentions a location, include ALL property ids for that location. Use [] only when no available_properties match or the hotel is single-property.",
                "book_ids": "array of book ids picked by matching the user's QUESTION against `book_catalog[].description`. Pick the most relevant 1-3 books. Leave empty only for clarify/service.",
                "clarification_question": "single concise question when route=clarify",
                "off_topic": (
                    "true ONLY when this message has nothing to do with this hotel, "
                    "the guest's own stay/booking/trip, or the area around the hotel "
                    "- a joke or riddle, a maths sum, general knowledge or trivia, "
                    "news, politics, coding, another company's product. A question "
                    "about the hotel, the booking, the stay, a complaint, or asking "
                    "for staff is NEVER off_topic, and neither is a message naming a "
                    "city or property we do not have. A message expressing self-harm "
                    "or a health crisis is NOT off_topic. When unsure, false."
                ),
                "reason": "short explanation",
                "confidence": "0..1",
                "response_language": {
                    "language": "language of language_user_message",
                    "script": "Latin|Devanagari|Kannada|other script",
                    "style": "native|romanized|mixed",
                    "confidence": "0..1",
                },
            },
        }
        fallback_prompt = (
            "You are the first-pass router for a hotel assistant.\n"
            "Decide if the user should be routed to a service handler or answered from KB books.\n"
            "Return STRICT JSON only.\n"
            "Route rules:\n"
            "1) route=service when the user clearly wants to book/order/request execution OR is clearly continuing the same active service conversation.\n"
            "2) route=book for informational questions; select relevant book_ids.\n"
            "3) route=clarify only when intent is truly ambiguous; ask one concise question.\n"
            "4) route=auto when uncertain.\n"
        )
        # Multi-model: routing runs on 4.1 and loads 4.1's router prompt. Resolve
        # the role model FIRST so the same value feeds the prompt fetch below, the
        # self-contained-scope gate, and the LLM call.
        model = self._role_model("main_router")
        try:
            system_prompt = await prompt_registry.get("orchestrator.main_router", model=model)
        except PromptMissingError:
            system_prompt = fallback_prompt
        # Property-focus routing rules (the legacy / 4.1 scope block). Appended to
        # the registry-loaded prompt and the static fallback for the DEFAULT path.
        # For a model whose DB prompt is self-contained (the 5.4 variant carries its
        # own lean scope rules inline), this block is NOT appended — see the guard
        # after it. We never hardcode keyword lists or property aliases here.
        _legacy_router_scope_block = (
            "\n=== ROUTING HAS TWO LAYERS ===\n"
            "Layer 1 — PROPERTIES: pick `selected_property_ids` (which property/properties the guest is asking about RIGHT NOW).\n"
            "Layer 2 — BOOKS: pick `book_ids` by matching the CURRENT QUESTION's words against `book_catalog[].title` + `description` + `aliases` + `keywords`.\n"
            "\n"
            "=== PRE-ARRIVAL PHASE SCOPE (judgment, not keywords) ===\n"
            "Read selected_phase. When it indicates the guest has NOT yet arrived (a pre-booking / pre-arrival / "
            "pre-check-in stage), a request that can ONLY be done once they are physically staying — housekeeping "
            "(cleaning, towels, room fixes), in-room maintenance, room service / in-room dining delivery, spa "
            "appointments, restaurant table reservations for the stay, or anything tied to a specific room / being "
            "on-property — must NOT be executed now. This includes a CURRENT-PROBLEM report about an in-room thing where "
            "the guest talks as if ALREADY in a room ('I have an issue with my bed', 'my bed is broken', 'my room is "
            "dirty', 'the AC isn't working in my room'); in pre-arrival that problem cannot be real yet, so do NOT route "
            "it to a support/contact service. BUT a plain INFO question about a room feature ('what bed do rooms have', "
            "'is the bed king size', 'do rooms have a bathtub', 'tell me about the beds') is NOT a problem — route it "
            "normally (route=book) and answer it. 'Room service' and 'in-room dining' are in-stay too — they "
            "are NOT a room booking, so NEVER route them to a room-booking service. For ALL the in-stay items above, do "
            "NOT set route=service to a support/contact/intake service and do NOT pick a service_id; use route=auto and "
            "let the main assistant reply (it will politely decline). Still allowed to route normally (route=service/book): "
            "booking a room/stay, event/venue/banquet bookings, general pre-arrival questions, help with an existing "
            "booking, and explicit 'talk to a human' asks. This is judgment about the request, never a fixed word "
            "list.\n"
            "\n"
            "=== ACTIVE SERVICE CONTINUITY ===\n"
            "- The payload may include `last_active_service` when the guest was just inside a service flow.\n"
            "- Keep route=service ONLY when the guest is clearly continuing execution of that same service: explicit booking/order/request intent, slot values, selecting an option, asking to proceed, confirming, or cancelling.\n"
            "- Pure informational follow-ups about hotel/property facts should stay route=book even if they happen near a service flow.\n"
            "- Only route to `last_active_service` when the current message shows active service continuation, not just topical similarity.\n"
            "\n"
            "=== BARE BOOKING VERB — IT INHERITS THE CURRENT TOPIC (critical) ===\n"
            "A bare booking/reserve verb with NO explicit object — e.g. 'book', 'book it', 'book now', 'i want to book', \"i'd like to book\", 'reserve', 'yes book' — does NOT automatically mean 'book a room'. Its object is whatever `last_assistant_message` was just about. Resolve it like this:\n"
            "- Previous turn was about ROOMS / a room type / a stay / nights / a suite → route=service with the room/stay booking service (e.g. room_booking_request). This is the ONLY case that maps to a room booking.\n"
            "- Previous turn was about a VENUE / event space / banquet / hall / celebration / party / wedding / meeting / conference / 'open air spaces' → DO NOT route to room_booking_request. There is no direct venue-booking service. Choose route=service to a support / contact-staff / enquiry service if one exists in `in_phase_services`; otherwise route=book with the venue/meetings/events book_ids so the answer offers a staff handoff.\n"
            "- Previous turn was about any other non-bookable topic (spa, dining, amenities, offers) with no matching bookable service in `in_phase_services` → continue that topic with route=book, or route=clarify if the object is genuinely unclear. Never silently fall back to room_booking_request.\n"
            "- Only treat a bare verb as a ROOM booking when rooms are the actual current topic or the user explicitly names a room/suite/stay. When unsure between room and something else, route=clarify and ask which one — do NOT assume rooms.\n"
            "- MULTI-PROPERTY BOOKING (critical, applies to ANY room/stay booking intent — 'book a room', 'i want to book', 'book it', 'reserve a room'): if 2+ properties are in scope (`active_property_ids` has 2+, OR active is empty and the named location has 2+) AND the guest has NOT named which specific hotel → you MUST set route=book (NOT route=service), keep those in-scope property ids selected, and leave service_id empty. The book answerer then asks 'which hotel?'. NEVER set route=service for a booking while 2+ properties are still unchosen — routing to the booking service there silently picks one hotel and strands the guest. Only route=service for a booking when EXACTLY ONE property is locked (the guest named it, or it is already the single active id).\n"
            "\n"
            "=== CHIP-CLICK FOLLOW-UPS (advisory) ===\n"
            "When `is_chip_click_followup` is true, the previous answer just generated a small set of suggestion chips and the guest tapped one. The chip text was already KB-grounded inside the previous answer's scope, so:\n"
            "- If `last_active_service` is set AND the chip's topic is the same service lane (e.g. continuing about rooms / spa / dining / transfers within that same service), PREFER route=service to that `last_active_service.id`. Service-lane continuity matters here.\n"
            "- If the chip text is a clearly informational question that any book can answer (e.g. 'what is checkout time' after a rooms-service answer), route normally — pick route=book and the right book_ids.\n"
            "- If the chip clearly switches topic to a different service or a different property, route normally.\n"
            "- This rule is advisory: when in doubt between service and book for an informational question, prefer route=book.\n"
            "\n"
            "=== LAYER 2: BOOK PICKING (CRITICAL — read carefully) ===\n"
            "PRIORITIZE THE CURRENT QUESTION OVER CONVERSATION CONTEXT.\n"
            "The guest's previous messages do NOT determine which book has the answer — the CURRENT question does.\n"
            "Examples:\n"
            "  - Conversation talked about 'Bombay Swim Club menu', current question 'what are the pool hours' → pick the book whose description matches 'pool hours / pool timings / operating hours', NOT the BSC menu book. Pool hours live in their own book.\n"
            "  - Conversation talked about 'LUX Suite', current question 'what time is checkout' → pick `checkout_time` / `checkout_policies`, NOT the LUX Suite book.\n"
            "  - Conversation talked about 'spa treatments', current question 'do you have parking' → pick `parking_facilities` / `valet_parking`, NOT spa books.\n"
            "Picking rules — match books to the QUESTION, and scale the NUMBER of books DOWN as the number of selected properties goes UP (every book_id is loaded for EVERY property in `selected_property_ids`):\n"
            "  1. Read the CURRENT user_message and identify what it actually asks for.\n"
            "  2. Pick the book(s) in `book_catalog` whose `title`/`description`/`aliases`/`keywords` MATCH that ask — nothing more, nothing less:\n"
            "       • A general / overview / 'tell me about' ask, or a 'name / list your hotels' ask → the OVERVIEW book (e.g. `hotel_overview` / `hotel_information`). The hotel NAMES are already provided in `available_properties`, so a pure names/list question needs only this one compact book.\n"
            "       • A specific-topic ask (pool hours, pets, parking, accessibility, breakfast, rooms, offers, dining, gym…) → the book(s) that cover THAT topic. This is normal description-matching; do NOT special-case topics — just match the book whose description fits.\n"
            "       • OUTSIDE THE HOTEL — an ask about what is AROUND it (somewhere to eat OUTSIDE the hotel, nearby restaurants / cafes / bars, shops, markets, beaches, sights, hospitals, ATMs, or anything phrased 'nearby' / 'around here' / 'in the area' / 'outside the hotel' / 'other than the hotel') → pick the LOCATION / TRANSPORT / neighbourhood book. This OVERRIDES the topic match above: 'any dining options outside the hotel' is a LOCATION ask, NOT the hotel's dining book. The dining book lists only the hotel's OWN outlets, so answering an outside ask from it forces a wrong 'no details available' decline; the location book is what lets real nearby places be looked up and named.\n"
            "  3. Pick the FEWEST books that answer it. With ONE property selected you may over-include cheaply; with SEVERAL properties selected each extra book multiplies across all of them, so stay tight — usually a single topic book, or the overview book for a general / comparison ask.\n"
            "  4. Only include books whose `property_id` is in `selected_property_ids` (or has empty/null property when single-property).\n"
            "  5. When 2+ properties are selected you MUST pick at least one matching book (the topic book, or the overview book) — NEVER leave `book_ids=[]`. An empty list makes the system load EVERY book of EVERY selected property (hundreds of books → slow + truncated + incomplete). Leaving `book_ids=[]` is acceptable ONLY when exactly one property is selected (it cheaply loads that single hotel's books for full coverage).\n"
            "Conversation context is for resolving pronouns/short replies and tracking property focus — NOT for biasing which book has the answer.\n"
            "\n"
            "=== LAYER 1: PROPERTY FOCUS ===\n"
            "INPUTS YOU MUST READ:\n"
            "- `available_properties`: every property the hotel has, each with `id`, `name`, optional aliases, and optional location label. This is your source of truth for property ids — never invent ids that aren't in this list.\n"
            "- `available_locations`: location groups with `label` and `property_ids`. This is the ONLY source of truth for which properties belong to which place. When the user names a BARE location — a city/area/region with NO specific property singled out (e.g. just the city name, 'hotels in <city>', 'the <city> ones') — `selected_property_ids` for that place must be EXACTLY the `property_ids` listed under it here — every one of them, and NOTHING else.\n"
            "  CLOSED SET — do NOT over-include (this is the fix for 'listed a property from another city for reference'): a property belongs to a place ONLY if its id appears under that place's group in `available_locations`. NEVER pull in a property from a DIFFERENT group because it shares a brand word, sounds geographically near, or 'might interest' the guest. Do NOT append any property 'for reference' / 'as an alternative' / 'fyi' / 'though not in <city>'. If an id is not in the named group, it is OUT. Your own world knowledge about where a property name *sounds* like it is located NEVER overrides these groups — trust `available_locations`, not the name.\n"
            "  PRECEDENCE — a SPECIFIC property name BEATS the city inside it (read this before applying the bare-location rule): almost every property name CONTAINS its own city (a property literally named like '<Brand> <City>'). If the guest types ONE specific property — its full name, a distinguishing fragment, or an unambiguous reference to ONE listed property — that is a SINGLE-PROPERTY pick: `selected_property_ids` = [that ONE id] and nothing else. Do NOT expand it to the whole city just because the property's NAME contains the city word. The bare-location → whole-city rule above fires ONLY when NO specific property is identified. (So a guest typing a property whose name ends in its city is choosing THAT property, not re-opening the city.)\n"
            "- `active_property_ids`: the focus locked in from earlier turns. A HINT, not a hard rule — switch when the current message says to.\n"
            "- `past_property_context`: properties the guest discussed earlier and moved on from. Add an id back from here into `selected_property_ids` if the guest references it again.\n"
            "- `discussed_properties_in_order`: every hotel discussed in THIS conversation, oldest first. Use this as the closed recall/correction set when the guest says things like 'the previous hotel', 'the other hotel', 'the first one we discussed', or corrects a mistaken hotel pick.\n"
            "- `user_message`, `last_assistant_message`, `last_user_message`: read all three before deciding.\n"
            "\n"
            "SINGLE-PROPERTY HOTELS:\n"
            "- If `available_properties` has 0 or 1 entries, leave `selected_property_ids=[]`. There is no 'active property' concept for single-property hotels — every book is in scope automatically.\n"
            "\n"
            "CHARITABLE NAME MATCHING (multi-property — mandatory):\n"
            "When the user types a property name, match it to an entry in `available_properties` LIBERALLY. Always normalize before comparing:\n"
            "  - lowercase both sides\n"
            "  - ignore punctuation, spaces, hyphens, underscores\n"
            "  - accept any 4+ character substring of a real property name or any 4+ character substring of its id\n"
            "  - accept location mentions that match `available_locations[].label` and then include ALL property_ids from that location\n"
            "  - accept obvious misspellings, typos, swapped letters ('rhiskishies' → 'rishikesh', 'rhishikesh' → 'rishikesh', 'shimla' === 'simla')\n"
            "  - accept clear referents that name a CITY or a distinct property ('the shimla one', 'the rishikesh hotel')\n"
            "Substring/brand matching applies only when the guest types a real CITY or a distinct property NAME. A shared BRAND word alone (e.g. 'Zone', 'Zone Connect Park', 'Park') must NOT switch to a same-brand property in another city — and a purely anaphoric phrase ('the other one', 'that one', 'the second one') is resolved by the RESOLUTION SET rule in DECISION RULES below, NOT by brand-substring matching.\n"
            "If even ONE word in the user's message phonetically/visually resembles a SPECIFIC property (by its distinguishing name/city, not just the shared brand) in `available_properties`, treat it as a SWITCH to that property.\n"
            "Only fall back to 'no property mentioned' when truly nothing in the message points at any specific property.\n"
            "\n"
            "DECISION RULES (multi-property only) — `selected_property_ids` is the new active set for THIS turn:\n"
            "- DEFAULT TO BREADTH. Only narrow when the user's CURRENT message gives a clear signal. A topic-level follow-up ('check-in time', 'do you have a pool', 'what dining options') is NOT a signal to narrow — those are property-agnostic and should keep the existing scope.\n"
            "- User names ONE property (charitable match) → SWITCH: selected = [that_id] — the new active set is EXACTLY that one id. This holds EVEN WHEN the property's name contains its city (a guest typing the full name of a property that ends in its city is choosing THAT ONE property — do NOT also add the city's other properties). Honor switches even when active says something else, and KEEP that single id pinned on the FOLLOWING vague/booking follow-ups ('book a meeting room', 'tell me more', 'what about rooms', 'book the hall') until the guest names a DIFFERENT property or explicitly asks to compare again. Naming a property is a LOCK, not a one-turn highlight.\n"
            "- NARROW-ON-INTEREST (the moment the guest leans toward ONE of the properties you just presented — applies right after a multi-property menu/list/comparison): SWITCH to that single property (selected = [that_id]) and STOP comparing, EVEN IF the guest did not type its exact name. The guest expressing interest in / leaning to / asking a question ABOUT one of the listed properties is itself the choice.\n"
            "  RESOLUTION SET (critical — this is HOW you resolve any relative/anaphoric reference, and it prevents cross-catalog mistakes): a reference that does NOT spell out a full property name — 'the other one', 'the first/second one', 'that one', 'the cheaper one', 'the one with the pool' — is ANAPHORIC: it points back into the conversation, NOT into the global catalog. Resolve it ONLY among the hotels already in the conversation set: the ones you just listed in `last_assistant_message`, plus `active_property_ids`, plus `past_property_context`, plus `discussed_properties_in_order`. You MUST NOT resolve such a reference to a totally unrelated hotel from `available_properties`, and in particular NEVER let a shared brand word (e.g. 'Zone', 'Zone Connect Park', 'Zone Park') fuzzy-match a same-brand property in another city. Charitable substring/brand matching is for when the guest types a real CITY or a distinct property NAME, NOT for anaphoric references.\n"
            "  Now resolve within that set:\n"
            "    • THE OTHER ONE / THE OTHER PROPERTY / THE OTHER HOTEL / SWITCH TO THE OTHER — the non-active hotel from the CURRENT conversation set. If exactly two discussed hotels exist and one is active, 'the other hotel' means the other discussed hotel, EVEN IF it is in a different city. Only use same-location sibling logic when the conversation is explicitly about a multi-property location.\n"
            "    • ORDINAL / POSITIONAL — 'the first/second/last one', '1st', '2nd', 'the latter', 'number 2', 'the first you mentioned' → the property at that POSITION in the order they appeared in `last_assistant_message` (count the order you actually listed them; do not guess). This is a NARROW: selected = [that one id].\n"
            "    • DESCRIPTOR / ATTRIBUTE — 'the one with the pool', 'the cheaper one', 'the 4-star one', 'the beachside one' → match the descriptor against the facts you gave for each in-scope property and pick the one it fits.\n"
            "    • DEICTIC / SOFT-ACCEPT — 'that one', 'this one', \"let's go with that\", 'sounds good', 'tell me more about it' → the property your previous message most prominently pointed at (your recommended pick, or the one a descriptor just matched).\n"
            "    • A QUESTION SCOPED TO ONE LISTED PROPERTY — e.g. 'does Zone Park have a pool?', 'tell me more about the second one', 'what rooms does the cheaper one have' → narrow to that property AND answer the question for it. (This is different from a property-AGNOSTIC topic question like 'do you have a pool?' / 'what's check-in time' with no property reference, which keeps the current scope per DEFAULT TO BREADTH.)\n"
            "  After resolving: selected_property_ids = [that single id]; treat it as a SWITCH and KEEP it pinned on every later vague follow-up (rooms, dining, spa, pool, 'tell me more') until the guest names/compares a DIFFERENT property or explicitly asks to compare again. Do NOT re-widen to the menu on the next turn.\n"
            "  COMPARATIVE / SUPERLATIVE descriptors ('the closest / nearest', 'the cheapest', 'the biggest', 'the highest-rated', 'the best for <X>') require comparing a FACT across properties. If the facts in `last_assistant_message` clearly identify the winner, narrow to that one. If you CANNOT confidently tell which property wins from the facts in front of you, do NOT default to the currently pinned / most-recently-discussed property and do NOT assert a guess — keep ALL in-scope properties selected (route=book, every in-scope id) and pick the `book_ids` that hold the comparison fact for EACH property (e.g. location / transport books for 'closest to the airport', room/rate books for 'cheapest'), so the answerer can read those books, identify the correct property, and pin it. NEVER narrow to a property that does not actually hold the superlative.\n"
            "  ONLY if the reference is genuinely unresolvable AND not a fact-comparison (e.g. 'that one' with no recommendation/descriptor to anchor it) → route=clarify with one short question; never guess between two equally-matching properties.\n"
            "- User names ONE BARE location (a city/area with NO specific property identified) → selected = every property_id in that location. If that location has multiple properties, keep all of them active. BUT if the message actually names ONE specific property (even one whose name ends in that city), this rule does NOT apply — narrow to that single property instead (see PRECEDENCE under available_locations above).\n"
            "- User names MULTIPLE locations → selected = union of property_ids from those locations.\n"
            "- User names MULTIPLE specific properties → selected = ids of every property named.\n"
            "- COMPARE / 'which is better' / 'which is best for <X>' / 'the two' / 'both' WITHOUT the word 'all' → compare the hotels ALREADY DISCUSSED in THIS conversation — NOT the whole chain, and NOT some city the guest never mentioned. selected = `active_property_ids` UNION every id in `past_property_context` (the hotels the guest has actually looked at, EVEN IF they are in DIFFERENT cities — e.g. 'which is better' after looking at Jaipur then Chennai = compare Jaipur vs Chennai). NEVER invent a comparison set from an unrelated city, and NEVER grab a random multi-property city's two siblings just because a comparison needs two. If only ONE hotel has been discussed so far, there is nothing to compare — answer about that one and say it's the only one discussed yet. Do NOT fan out to every hotel in `available_properties`.\n"
            "- ONLY an EXPLICIT 'ALL / EVERY' ask — 'all your hotels', 'every hotel', 'all your properties', 'list all your hotels', 'what cities/destinations do you have', 'tell me about all your hotels' → selected = every id in `available_properties`. A bare 'compare' / 'which is better' is NOT this.\n"
            "- User ADDS a property to active ('also tell me about Shimla' while active=[rishikesh]) → selected = union (active ∪ new).\n"
            "- User REMOVES a property ('actually skip Shimla') → selected = active minus the dropped one.\n"
            "- Clean continuation that NAMES no property AND active is a single id → selected = active_property_ids (keep the same single id).\n"
            "- Clean continuation that NAMES no property/location AND active has multiple ids → selected = active_property_ids exactly as-is. Do NOT broaden to all properties and do NOT narrow to one unless the user explicitly does so.\n"
            "- Truly generic ask, no property/location reference at all AND active is empty → selected = every id in `available_properties`. NEVER leave selected empty for book/auto routes when properties exist.\n"
            "- If active_property_ids already represents one location with multiple properties, preserve that full set on follow-up questions like 'compare both', 'which is better', 'what room types do you offer', or 'tell me more'.\n"
            "- \"COMPARE BOTH\" / \"THE TWO PROPERTIES\" / \"THESE PROPERTIES\" / \"BOTH OF THEM\" RESOLUTION (critical — do NOT jump to a different location): When the guest says to compare 'both' / 'the two' / 'these' properties and does NOT name a location, 'both' refers to the properties of the LOCATION THE CONVERSATION IS ALREADY ABOUT — never some other city. Resolve it like this:\n"
            "    1. If `active_property_ids` already has 2 ids from one location → selected = those exact two.\n"
            "    2. If `active_property_ids` has a SINGLE property that belongs to a multi-property location → the guest means that property and its SIBLING(S) at the SAME location. Re-expand to that location's full set. Pull the sibling id(s) back from `past_property_context` and/or the conversation history (the recently-discussed properties of that same city) — selected = the full sibling set for that location.\n"
            "    3. NEVER answer 'compare both' by switching to two properties from a DIFFERENT location than the one being discussed. If the conversation has been about Coimbatore, 'compare both' = the two Coimbatore properties, NOT Goa or any other city. The properties in `past_property_context` are the recently-discussed ones — prefer them over any unrelated location when resolving a vague 'both'.\n"
            "    4. Only if NO location context exists anywhere (active empty AND past empty AND history names no property/city) may you ask one short clarifying question — never silently pick an arbitrary city.\n"
            "- Narrow a multi-property location down to ONE property when EITHER (a) the guest explicitly NAMES that property, OR (b) `last_assistant_message` RECOMMENDED / favored ONE specific in-scope property as its pick (e.g. 'I recommend Zone Connect Park Calangute Goa' / 'Calangute is the better fit' / 'If I were you I'd pick Zone Connect Park Coimbatore') AND the guest's CURRENT message accepts OR continues on that pick WITHOUT naming a different property → selected = [that recommended property's id]. Acceptance/continuation is NOT only 'yes' / 'yes please' / 'show room types' / 'go ahead' / 'sounds good' / 'that one' / 'ok' / \"let's do it\" — it ALSO includes follow-ups that lean INTO the pick: 'tell me more about your pick', 'more about it', 'tell me more', 'why that one', \"what's it like\", 'what about it', 'more on that one'. This applies EVEN IF the bot also mentioned the OTHER property as a runner-up/alternative OR tacked on an open 'which property would you like?' question — the property the bot named as its PICK is what gets pinned (do not treat it as ambiguous, do not re-widen). A vague 'tell me more' right after the bot picked ONE property = tell me more about THAT property → pin it. Once narrowed this way, KEEP it pinned (it becomes the single active id) on every later vague follow-up (amenities, dining, spa, pool, rooms, 'tell me more') until the guest names/compares a different property. Do NOT fan back out to the location's other properties on those follow-ups.\n"
            "- \"BOTH\" / \"ALL\" / \"THEM\" / \"THE TWO\" / \"EACH\" IS NOT AMBIGUOUS — NEVER CLARIFY ON IT. When the guest says 'tell me about both', 'both', 'tell me about them', 'all of them', 'the two', 'each of them', 'compare them', or similar, they are EXPLICITLY asking you to cover EVERY property currently in scope. 'Both' is itself the answer to 'which one?' — it means all of them. So: selected_property_ids = the full active set (all properties of the current location, re-expanding from past_property_context/history if active has narrowed to one), route=book, and ANSWER for every property. NEVER respond with route=clarify or a 'which one would you like?' question — asking the guest to pick one after they said 'both' is a broken loop.\n"
            "- Wording genuinely ambiguous between two properties (history can't disambiguate) → route=clarify, selected_property_ids=[], ask one short question. BUT a request that says 'both/all/them/the two' is NOT ambiguous (see the rule above) — cover them all, never clarify.\n"
            "\n"
            "=== CROSS-CONVERSATION RECALL (the ANSWERER handles it — keep the active scope STABLE) ===\n"
            "When the guest's LATEST message is an explicit cross-conversation recall — 'summarize everything we discussed', 'recap everything', 'all the hotels we discussed', 'the previous / last / first / earlier hotel', 'the previous location', 'the hotel we discussed before', 'the <city> one we discussed earlier' — do NOT switch or narrow the active property for it, EVEN IF a topic word rides along ('does the previous hotel have a pool'). Set route=book and keep `selected_property_ids` = the CURRENT `active_property_ids` unchanged (leave [] if there is no active scope). Pick broad overview/amenity book_ids for any named topic. The answerer is given the full ordered list of every hotel discussed plus the conversation history, and it will recall the correct earlier hotel by name (or summarize them all) WITHOUT changing the active hotel. Do NOT treat recall as a property switch, and do NOT route=clarify for it — the answerer decides and will itself ask the guest if it is unsure which hotel they mean.\n"
            "  ALSO RECALL — asking for the NAME/identity of an EARLIER hotel via a descriptor + a past-reference word ('what was the beach one CALLED again', 'the name of the mediterranean one we SAW', 'the hill one we LOOKED AT / DISCUSSED'): this is recall too — answer the name from the discussed hotels + history and keep the active property UNCHANGED. Do NOT switch scope to that descriptor's location. (A FRESH ask with no past-reference — 'show me beach hotels', 'any hotels in the hills' — is NOT recall; that's a normal location ask and DOES set scope. The signal is the past-reference word: 'again', 'we saw / looked at / discussed', 'you mentioned'.)\n"
            "\n"
            "BROADEN RULE — guest explicitly asks to leave the current scope (read carefully):\n"
            "Trigger ONLY when ALL three are true:\n"
            "  (a) The CURRENT user_message contains an explicit broaden/change-of-scope intent — phrases like 'change location', 'change my location', 'different location', 'other location(s)', 'another city', 'somewhere else', 'somewhere different', 'different place', 'other places', 'recommend (some) (more) locations/cities/places/destinations', 'suggest (some) (more) locations/cities/places/destinations', 'show me more options', 'what other cities/properties do you have', 'where else can I go/stay', 'I want to switch (the/my) location', 'change (the) destination'. Synonyms for 'change' include: switch, move, swap, pick another. Synonyms for 'recommend more': suggest more, show more, give me more options. Wording can be casual or misspelled — judge intent, not exact words.\n"
            "  (b) The same message does NOT name any specific property or location from `available_properties` / `available_locations`. (If the guest names Shimla, Manali, etc., that is a SWITCH — apply the SWITCH rule above, not this BROADEN rule.)\n"
            "  (c) The guest is NOT mid-flow inside a service. If `last_active_service` is set AND the message is still about that service (slot answers, room picks, dining details for the chosen property, booking confirmations, dates, names, numbers, 'yes/no/ok/cancel', or any continuation of the service the guest just selected), DO NOT broaden — keep `active_property_ids` exactly as-is and stay on that service.\n"
            "When all three conditions hold → selected_property_ids = every id in `available_properties` (full reset). This is the ONLY case where you wipe a non-empty active set without the guest naming a replacement.\n"
            "When ANY condition fails → DO NOT broaden. Treat the message under the rules above (continuation / switch / add / remove / clarify).\n"
            "Important guardrails:\n"
            "  - Routine informational follow-ups like 'tell me more', 'what dining', 'spa services?', 'which is better for family', 'any romantic options?', 'suggest something romantic' — these are NOT broaden triggers on their own. They modify the existing scope's answer, they don't ask for new locations. Only treat them as broaden if they ALSO contain explicit change-of-location language ('suggest romantic LOCATIONS', 'change my honeymoon LOCATION to somewhere romantic', 'other CITIES that are romantic').\n"
            "  - A guest who has just picked a specific room/service/property (e.g. 'book this', 'I want the Bee Eater room', 'tell me more about Passaros') has NARROWED scope. Never broaden in the very next turn unless they explicitly say so. Sticky narrow > speculative broaden.\n"
            "  - If the message clearly says change-of-location AND names a target ('change location to Goa') → SWITCH to the named location, not BROADEN.\n"
            "  - If unsure whether a phrase is broaden intent, prefer the existing scope. Do not guess.\n"
            "\n"
            "=== ACTIVE-PROPERTY GATE (MULTI-PROPERTY HOTELS ONLY — FINAL CHECK BEFORE OUTPUT) ===\n"
            "After applying every rule above, before emitting your JSON, run this gate:\n"
            "\n"
            "A PROPERTY-SPECIFIC SERVICE is any service whose operation happens AT a specific property's facilities — room booking, dining/restaurant reservation, spa/wellness booking, in-room dining, room service, food / drink orders, housekeeping, laundry, maintenance, shuttle/transfer booking, table booking, etc. These services need ONE property locked before they can operate — the property is itself the first decision in the flow.\n"
            "\n"
            "If you set `route=service` for a property-specific service AND:\n"
            "  - `selected_property_ids` ends up empty OR has 2+ ids, AND\n"
            "  - the current `user_message` does NOT name a single specific property (charitable match — \"Ira Mumbai\", \"the BKC one\", \"Fort Jadhavgadh\")\n"
            "→ CHANGE `route` to `book`. Keep `selected_property_ids` as the full qualifying set (e.g. all properties for a generic ask, or all properties in a named city). Empty `service_id`. The book_answerer will present a HELP-FIRST property menu so the guest can pick. The service is reached automatically on the NEXT turn once the property is locked.\n"
            "\n"
            "EXCEPTIONS — support intakes are not property-operations, but they still must pass the support eligibility gate:\n"
            "  - Support / complaint / staff-callback / help-desk / ticket-intake services (anything matching tokens like `support`, `issue`, `complaint`, `request`, `report`, `help`, `raise`, `contact_staff` in id/name/description) may run with empty or multi active properties ONLY when the guest did not name an unsupported property/location. If the guest names a property or location that does not resolve inside `available_properties` / `available_locations`, do NOT route to support; set `route=book`, leave `service_id` empty, and let the answerer explain the supported hotel scope. If no property was named, or multiple supported properties qualify, the support agent itself can ask which supported property this is for. They are reached via rule 1a (eligible hotel complaint / staff ask) and rule 1a-extra (valid handoff-offer acceptance).\n"
            "  - ANSWERING 'WHICH HOTEL?' RESUMES THE COMPLAINT, IT IS NOT A NEW QUESTION. If the previous assistant turn asked which property a COMPLAINT / support matter is about, and this message just names or picks a supported property or city, the guest is completing that complaint — route BACK to the same support service with that property as the ONLY `selected_property_ids`. Do NOT treat the bare property name as a fresh informational ask; answering it with hotel details or contact numbers strands the complaint, because the guest already told you what they wanted and only supplied the missing hotel.\n"
            "\n"
            "EXAMPLES:\n"
            "  - \"I want a stay\" / \"I need a room\" / \"looking to book\" (no property named, multi-property hotel) → gate fires → `route=book`, `selected_property_ids = all`, `service_id=\"\"`. The book_answerer shows the property menu.\n"
            "  - \"rooms in mumbai\" with 2 Mumbai properties → gate fires → `route=book`, `selected_property_ids=[mumbai_a, mumbai_b]`. The book_answerer presents both with differentiators.\n"
            "  - \"book a room at Ira Mumbai\" (single named property) → gate does NOT fire → `route=service` to room_booking_request, `selected_property_ids=[ira_mumbai]`.\n"
            "  - \"AC not working\" / \"can someone call me back\" (active scope empty or multi) → gate does NOT fire (support is exempt) → `route=service` to support, agent handles property ask inside.\n"
            "  - \"yes please\" after the previous turn offered staff handoff → rule 1a-extra → `route=service` to support, gate does NOT downgrade.\n"
            "\n"
            "This gate ensures the guest always sees the property menu BEFORE a property-specific service starts collecting slots or guessing properties from a partial KB.\n"
        )
        # ISOLATION: the legacy scope block is for 4.1 (and any non-self-contained
        # model). The 5.4 variant's DB prompt already carries its own lean scope
        # rules, so we skip pasting the 40k block onto it — that block was drowning
        # out the 5.4 prompt and mis-scoping the router. Selected model = the /chat
        # harness override (falls back to env default when nothing is selected).
        _router_self_contained = self._model_uses_self_contained_prompts(model)
        if not _router_self_contained:
            system_prompt = system_prompt + _legacy_router_scope_block
        _router_booking_block = self._build_booking_context_prompt_block(context)
        if _router_booking_block:
            system_prompt = system_prompt + "\n" + _router_booking_block

        # `model` for the router was resolved above (self._role_model("main_router")).
        router_max_tokens = max(
            120,
            int(getattr(settings, "chat_llm_main_router_max_tokens", 420) or 420),
        )
        # The hotel's own routing rules, from its DB prompt row `router.hotel_rules`,
        # sent as the last routing rule so they outrank the in-house ones above.
        # Hotels set up in the Pre-booking tab only (T24 onward): with no hotel
        # chosen, "i want to book room" went to the general answerer on some turns
        # and to the room-booking agent on others, with the same input (14 Oct).
        # Zone, Mango and Sarovar keep the router they were tested with.
        _router_hotel_rules = ""
        try:
            from services import prebooking_settings_service as _pbs_router

            if _pbs_router.is_admin_managed(str(getattr(context, "hotel_code", "") or "").strip().lower()):
                _router_hotel_rules = await self._db_prompt_text("router.hotel_rules")
                # The hotel's own facts, from its row `router.hotel_facts`: which of
                # its hotels each named venue, restaurant or room belongs to. The
                # router sees no per-hotel KB, so "book one workhard venue" with T24
                # Retro in focus stayed on Retro and was answered from Retro's
                # Converge Hall (15 Oct). Each hotel keeps its own row; none = skipped.
                _router_hotel_facts = await self._db_prompt_text("router.hotel_facts")
                if _router_hotel_facts:
                    _router_hotel_rules = f"{_router_hotel_rules}\n\n{_router_hotel_facts}".strip()
        except Exception:
            _router_hotel_rules = ""
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    # FINAL narrowing directive — placed LAST on purpose. The
                    # narrowing rules above sit deep inside a very large system
                    # prompt and get out-weighed by the 'default to breadth' /
                    # 'keep the sibling' guidance, so the router keeps a whole
                    # city even when the guest typed ONE property's name. A short
                    # rule right after the payload wins on recency.
                    {
                        "role": "system",
                        # Skip this legacy recency-block for the self-contained 5.4
                        # prompt (empty content = no-op); 4.1 keeps it in full.
                        "content": ("" if _router_self_contained else (
                            "FINAL NARROWING CHECK — apply AFTER every rule above; it OVERRIDES any "
                            "'default to breadth' or 'keep the sibling' guidance.\n"
                            "Compare `user_message` against `available_properties`. If it points at ONE "
                            "specific property — its full name, an alias, or a distinguishing fragment that "
                            "matches exactly one property — then `selected_property_ids` MUST be EXACTLY that "
                            "one id. Drop every other property, INCLUDING a same-city sibling, EVEN WHEN "
                            "`active_property_ids` currently holds 2+ for that city and EVEN WHEN the "
                            "property's name contains the city word (e.g. a property named like '<Brand> "
                            "<City>' — naming it is choosing THAT property, not re-opening the city). Naming "
                            "one property is a deliberate LOCK, not a request to keep comparing — do NOT 'pin "
                            "the property and its sibling'.\n"
                            "Keep 2+ ids ONLY when `user_message` ITSELF asks to compare / see multiple "
                            "('compare', 'both', 'the two', 'which is better') OR names a BARE city/area with "
                            "no specific property. A vague follow-up ('book a meeting room', 'tell me more', "
                            "'what about rooms') with a SINGLE active property KEEPS that one — never re-widens.\n"
                            "ZEROING IN (focus) — the moment the guest singles out ONE of the properties you're "
                            "currently showing, they've chosen a focus: narrow `selected_property_ids` to just "
                            "that one and keep it focused for the questions that follow, until they switch to a "
                            "different property or ask to compare again. They may single it out any way — by its "
                            "position ('the second one'), by a distinguishing attribute ('the cheaper one', 'the "
                            "beachside one'), or by pointing at it ('that one', and then a following 'it'/'this'). "
                            "Resolve which property from the list you last showed + the conversation; do NOT keep "
                            "both in scope after they've zeroed in, and do NOT fall back to the first one. Once "
                            "focused, a later bare 'it'/'this one' is that SAME focused property.\n"
                            "FOOTPRINT vs RECALL (understand which one — do NOT match on the word 'cities'). If "
                            "the guest wants the BRAND'S overall reach — which cities/destinations the COMPANY "
                            "operates in, where all the hotels are, the full set of places to book — that is a "
                            "footprint ask: set `selected_property_ids` to EVERY id in `available_properties`, "
                            "even when one property is active (they want the whole map, not the city they were "
                            "just on). BUT if the guest is instead RECALLING THIS conversation — the cities/hotels "
                            "WE already discussed ('which ones did we talk about', 'the places we looked at') — "
                            "that is NOT a footprint ask: keep the active scope and let the answerer recall only "
                            "what was discussed. Tell them apart by intent: asking what the brand HAS = footprint "
                            "(all); asking what WE DISCUSSED = recall (keep scope).\n"
                            "CLOSED-SET COMPARE (this stops you inventing a second hotel) — when you keep 2+ "
                            "ids for an UNNAMED compare ('compare', 'both', 'the two', 'which is "
                            "cheaper/better/bigger/closer', 'which is better for <X>') where the guest did NOT "
                            "name the specific hotels: the ids you keep MUST be a SUBSET of `active_property_ids` "
                            "UNION the KEYS of `past_property_context` — the hotels ACTUALLY discussed in THIS "
                            "conversation. You may NOT add ANY other property id to make a pair: not a same-brand "
                            "hotel, not a same-state or geographically-near city, not a 'natural-looking' sibling "
                            "of the active property. The comparison needing two hotels is NEVER a licence to "
                            "invent the second one. If that closed set has only ONE hotel, compare just that one "
                            "(or say only one has been discussed so far) — do NOT pad it with a hotel the guest "
                            "never looked at. Worked example: active_property_ids=[A] and `past_property_context` "
                            "keys are {B} → an unnamed 'which is cheaper' compares A vs B and NOTHING else; "
                            "outputting any third property C is WRONG.\n"
                            "FLEET-ATTRIBUTE QUESTION (this OVERRIDES the closed-set rule just above) — a "
                            "question asking WHICH of your hotels has / allows / is some attribute ('which "
                            "hotel is pet friendly', 'which has a rooftop pool', 'which one is best for "
                            "elderly', 'which allows early check-in', 'which is cheapest') is a comparison "
                            "across your WHOLE brand, not just the hotels discussed so far. A comparison needs "
                            "2+ hotels, so decide by how many are ACTIVE: if `active_property_ids` has 0 or 1 "
                            "property, you CANNOT compare within one — set `selected_property_ids` to EVERY id "
                            "in `available_properties` (the entire fleet). If `active_property_ids` already "
                            "holds 2+ (the guest deliberately narrowed to a city/group), compare WITHIN those "
                            "ONLY — unless the guest says 'all' / 'any of your hotels' / 'across all', which "
                            "re-opens the whole fleet. This is different from the closed-set rule: that one is "
                            "for comparing two hotels the guest already looked at ('which of these two is "
                            "cheaper'); a fleet-attribute question is about ALL your hotels.\n"
                            "SAME-CITY TWINS & DEICTIC REFERENCES — resolve WHICH hotel the guest means. "
                            "This matters most when two hotels have look-alike names (e.g. 'Zone Connect "
                            "Park <City>' vs 'Zone Park <City>'). Use `active_property_ids` (it is in the "
                            "ORDER the hotels were presented) and `recent_messages` (the recent turns).\n"
                            "  - VAGUE FOLLOW-UP that names NO hotel and gives no 'other'/'compare'/'all' "
                            "signal ('tell me more about this', 'more about it', 'what about this one', 'and "
                            "the rooms?', 'tell me more') → KEEP the CURRENT single active property EXACTLY. "
                            "'this'/'it' = the one already active. Do NOT switch to its same-city twin; "
                            "`active_property_ids` already holds the right one — trust it.\n"
                            "  - 'the OTHER one' / 'the other hotel' / 'not this one' / 'the other property' → "
                            "the guest wants the DIFFERENT hotel in the SAME location. Set "
                            "`selected_property_ids` to the SIBLING — the other id for that city (from "
                            "`available_locations` or `past_property_context`), NOT the currently-active one.\n"
                            "  - ORDINAL ('the first / second / third / 2nd / 3rd hotel', 'the last one') → "
                            "count in the presented order: `active_property_ids` is in that order and "
                            "`recent_messages` shows the list. 'the second one' = the 2nd id in that ordered "
                            "set. Do NOT default to the first or the currently-active one."
                        )),
                    },
                    # FINAL SERVICE-LANE CHECK — placed LAST, and deliberately NOT gated
                    # on the model. The correct rule already exists in the system prompt
                    # ("=== ACTIVE SERVICE CONTINUITY ===": informational follow-ups stay
                    # route=book) but it sits ~62% into a ~97k-char prompt and loses to
                    # louder, earlier service-specific guidance — so an ordinary KB question
                    # ("does it have a pool?") kept getting captured by whatever service was
                    # last active. Same recency mechanism as the narrowing block above.
                    # Stated as a GENERAL principle so it covers EVERY service, instead of
                    # the room-lane-only patch that already exists near the top.
                    {
                        "role": "system",
                        "content": (
                            "FINAL SERVICE-LANE CHECK — apply AFTER every rule above.\n"
                            "A service owns ONLY its own topics. `last_active_service` being set does NOT "
                            "entitle that service to the next message. Judge THIS message on its own: if it "
                            "is an informational question about the hotel, its properties, rooms, facilities, "
                            "dining, location or policies, route it as information (route=book) EVEN WHEN a "
                            "service was active a moment ago.\n"
                            "Keep route=service ONLY when this message actually continues that service's own "
                            "transaction — supplying a detail it asked for, choosing an option, asking to "
                            "proceed, confirming, or cancelling.\n"
                            "This applies to EVERY service equally, including support / contact / intake "
                            "flows: a guest who asked for a human a moment ago and then asks about the pool "
                            "is asking about the pool — answer that.\n"
                            "UNKNOWN LOCATION -> SHOW THE REAL FOOTPRINT: when the guest names a city, region or "
                            "property that is NOT in `available_properties`, do NOT return an empty "
                            "`selected_property_ids`. Set it to EVERY id in `available_properties`. An empty "
                            "selection leaves the reply stuck on whatever was active before, so the guest is told "
                            "only about those few hotels and it looks like that is all the brand has. Selecting all "
                            "lets the reply say plainly there is nothing in that place and show where the brand "
                            "actually operates.\n"
                            "  -> UNKNOWN PLACE IN A BOOK / COMPLAINT INTENT: if that same unknown city/region/property "
                            "is what the guest wants to BOOK, or is the target of a COMPLAINT / request they are filing, "
                            "you MUST also set route=book (NEVER route=service) and create NO ticket this turn. There is no "
                            "property there to book a room at or to file a complaint against, so the answerer must decline "
                            "plainly and show the real footprint — it must never be dispatched to a booking/support service "
                            "or logged as a request. (A KNOWN place the brand DOES have is unaffected — route it normally.)\n"
                            "A VAGUE MESSAGE IS NOT A SUPPORT REQUEST: a message with no specific request in "
                            "it (for example the guest just saying they need help) does NOT by itself mean they "
                            "want staff. Do NOT route it to a support / contact / intake service. Use route=book "
                            "so the reply can offer what you help with and let them choose. Treat it as a support "
                            "request only once they actually ask for a person or describe a problem needing one.\n"
                            "COMMANDING THE UI IS NOT A SUPPORT REQUEST EITHER: a message whose only content is an "
                            "instruction to open/give/show 'the form', or a blanket affirmation with no real matter — "
                            "'give me the form', 'open the form', 'yes form for everything', 'yes to everything', 'do "
                            "everything', 'for everything', 'all of it' — carries NO actual hotel matter (no complaint, "
                            "no specific request, no named topic). The guest does NOT get to trigger the form just by "
                            "naming it, and 'everything' is not a request. Route these to book (NEVER to a support/"
                            "contact/intake service); the reply asks one short line about what specifically they'd like "
                            "help with. EXCEPTION — an explicit ask to reach a PERSON ('talk to staff', 'connect me to "
                            "someone', 'I want a human') IS a genuine support request and still routes to support: the "
                            "line is a real wish to reach a person vs. a bare command to open the UI."
                        ),
                    },
                    # HANDOFF-ACCEPTANCE OVERRIDE — placed LAST so it wins on recency,
                    # and always-on (not model-gated). Root cause it fixes: when a
                    # service (in-room dining, transport, spa, housekeeping…) tells the
                    # guest it CANNOT do something (e.g. cancel a placed order / a booked
                    # pickup) and offers to connect them to the front desk / team, the
                    # guest's "yes" was being read as "continuing that service" (the
                    # ACTIVE SERVICE CONTINUITY + "…or cancelling" clause), so the router
                    # kept routing back to the SAME service, which just re-declined — an
                    # endless loop and NO handoff ticket ever raised. The guest accepted
                    # a HUMAN handoff; that belongs to the human-handoff service, not the
                    # service that couldn't help. Purely context-driven: the model reads
                    # `last_assistant_message` + `user_message` and resolves the service
                    # by PURPOSE from `in_phase_services` — no keyword/id hardcoding.
                    {
                        "role": "system",
                        "content": (
                            "HANDOFF-ACCEPTANCE OVERRIDE — apply this LAST; it OVERRIDES 'active service "
                            "continuity' and the 'cancelling continues the service' clause.\n"
                            "PRECONDITION — an OFFER OF HUMAN HELP must already be on the table. Read "
                            "`last_assistant_message` and find the sentence where YOU offered it: to connect "
                            "the guest to the front desk / reception / our team / a human, to have staff reach "
                            "out or call them back, or to pass their request to a team. This override applies "
                            "ONLY if such a sentence is really there.\n"
                            "A REFUSAL IS NOT AN OFFER. Telling the guest something is unavailable, not "
                            "listed, not possible in chat, or only possible after check-in — with no mention "
                            "of a person helping — offers them NOTHING to accept. Neither does an offer to "
                            "share more information. In those cases a following 'yes' means 'yes, tell me "
                            "more', so DO NOT fire this override: route it as the fresh message it is. "
                            "(Real failure: the bot said 'I don't have candlelight therapy listed for Aloe "
                            "Spa', the guest tapped 'Yes, please', and this override opened a staff form for "
                            "an offer that was never made.)\n"
                            "The one exception is a service that CANNOT DO what was asked AND said in the "
                            "same breath that staff can — both halves, not just the refusal.\n"
                            "If YES, and the current `user_message` accepts or agrees in ANY wording or language "
                            "(e.g. 'yes', 'yes please', 'ok', 'sure', 'please do', 'go ahead', a tapped 'Yes' "
                            "chip) → you MUST set route=service and pick the service in `in_phase_services` whose "
                            "PURPOSE is connecting the guest to a human / front desk / reception / support / "
                            "callback (judge by each service's name + description — e.g. a 'Talk to Our Team', "
                            "'Front Desk', 'contact staff', complaint or support-intake service).\n"
                            "Do this EVEN WHEN `last_active_service` is a DIFFERENT service (in-room dining, "
                            "transport, spa, housekeeping, table reservation, etc.) — that service does NOT own a "
                            "human handoff the guest just accepted, so do NOT route back to it and do NOT re-emit "
                            "the same 'I can't do that' reply. If `in_phase_services` has NO human-handoff / "
                            "support service, use route=book instead. This override only fires on a genuine "
                            "acceptance of a handoff YOU just offered — never invent a handoff the guest did not "
                            "accept, and never fire it for an ordinary in-service 'yes' (e.g. 'yes add fries').\n"
                            "CHECK BEFORE YOU DECIDE — you must PROVE the offer exists. Copy the exact sentence "
                            "from `last_assistant_message` that carries the offer into the output field "
                            "`handoff_offer_quote`, word for word, no paraphrasing. If you cannot find such a "
                            "sentence to copy, then YOU DID NOT MAKE AN OFFER: leave `handoff_offer_quote` empty "
                            "and do NOT fire this override. A message that only DECLINED something ('that's "
                            "available only once you're checked in', 'I can't take that request right now') is a "
                            "refusal, NOT an offer — a 'yes' after it is not an acceptance of anything, so route "
                            "it as a fresh message (route=book). Do not assume an offer was made because the "
                            "guest said yes; read the message and confirm."
                        ),
                    },
                    *([{"role": "system", "content": _router_hotel_rules}] if _router_hotel_rules else []),
                    {
                        "role": "system",
                        "content": (
                            "FINAL LANGUAGE CLASSIFICATION — this does not change the route. "
                            "Always populate `response_language` from `language_user_message`, "
                            "which is the guest's untouched text. Do not classify from the English "
                            "system prompt, KB, hotel location, assistant message, or an English "
                            "[CONTEXT] wrapper in `user_message`. A full clear current message "
                            "switches language immediately. Detect transliterated/romanized text "
                            "semantically: for example, 'mujhe ye chahiye' means language=Hindi, "
                            "script=Latin, style=romanized — it is not English. For a genuinely "
                            "language-neutral bare number, emoji, yes/no, or acknowledgement only, "
                            "retain the most recent established guest language from recent_messages. "
                            "If route=clarify, write `clarification_question` in this same language, "
                            "script, and style."
                        ),
                    },
                ],
                model=model,
                temperature=0.0,
                max_tokens=router_max_tokens,
                trace_context={
                    "responder_type": "main",
                    "agent": "main_router",
                    "selected_phase_id": selected_phase_id,
                    "selected_phase_name": selected_phase_name,
                    "session_id": str(getattr(context, "session_id", "")),
                },
            )
        except Exception:
            return fallback

        _log_llm_call(
            label="MAIN ROUTER RESPONSE",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt=system_prompt,
            payload=payload,
            response=raw if isinstance(raw, dict) else {},
        )
        if not isinstance(raw, dict):
            return fallback

        route = self._normalize_main_router_route(raw.get("route"))
        valid_service_ids = {self._normalize_identifier(item.get("id")) for item in in_phase_services}
        service_id = self._normalize_identifier(raw.get("service_id"))
        if not service_id or service_id not in valid_service_ids:
            service_id = ""
            if route == "service":
                # ── IT SAID "A SERVICE" BUT DID NOT SAY WHICH — SO ASK ────────
                # Silently degrading to `auto` here is how an actionable request
                # gets answered as conversation. Real failure: right after a food
                # order was placed, "my ac is not cooling and can i also get 2
                # extra pillows" came back route=service with service_id=null.
                # It was blanked, fell to auto, was answered from the main lane
                # with "please contact the front desk", and the guest's "yes
                # please" to THAT was then read as accepting a front-desk offer
                # and opened the generic support form. Two teams' work vanished
                # into a contact-us form, and the router had been RIGHT that a
                # service was needed - it just failed to name one.
                # So recover the name instead of discarding the verdict. Falls
                # back to the old behaviour whenever this cannot answer either.
                _recovered = await self._service_this_message_belongs_to(
                    user_message=user_message, services=in_phase_services)
                if _recovered and _recovered in valid_service_ids:
                    logger.info(
                        "[MAIN_ROUTER] route=service with no service_id — "
                        "recovered %r from the message instead of dropping to "
                        "auto", _recovered)
                    service_id = _recovered
                else:
                    route = "auto"

        # ── A QUESTION ABOUT FOOD MUST REACH THE MENU ────────────────────────
        # The hotel-info lane holds the property KB, not the menu, so it denied a
        # dish the kitchen actually cooks: "do you have idli" -> "I don't have
        # idli listed specifically", while the identical question one turn into a
        # food order got "We do serve Idli, 7:00 AM to 10:30 AM". Sending a guest
        # away from real food is worse than any wording bug.
        #
        # Two steps on purpose. The gate asks only "is this about food?", and
        # ONLY THEN is the service resolved from the live catalogue - so nothing
        # else is pulled out of the lane that answers it properly, and no service
        # id is hardcoded here. A general "which service?" judge on its own
        # answered "the front desk" for "what time does the bar close".
        if route != "service" and not service_id and in_phase_services:
            if await self._is_the_guest_asking_about_a_dish(user_message=user_message):
                _owner = await self._service_this_message_belongs_to(
                    user_message=user_message, services=in_phase_services)
                if _owner and _owner in valid_service_ids:
                    logger.info(
                        "[MAIN_ROUTER] a question about food was heading for the "
                        "hotel-info lane, which has no menu — routing to %r",
                        _owner)
                    route, service_id = "service", _owner

        # ── CONSISTENCY GUARD: the model's OWN topic classification wins ──────
        # The small routing model sometimes labels the message a NEW topic
        # (topic_relation="new_topic") — correctly recognising it left the active
        # service's domain — yet still emits route=service pointing at that very
        # leftover service. That self-contradiction is the "turn-immediately-
        # after-a-service" stickiness bug (e.g. a pickup service that just asked
        # "which one?" capturing a following "what room types do you have?").
        # Enforce the model's own classification GENERICALLY — no per-service or
        # per-topic hardcoding: a message the router itself calls a new topic must
        # never stay on the prior active service. Drop to the book answerer, which
        # routes by the actual topic (or honestly answers from the KB).
        _topic_relation = str(raw.get("topic_relation") or "").strip().lower()
        # Gated to Sarovar (the only tenant whose main_router prompt emits
        # `topic_relation`); the hotel check keeps it explicitly Sarovar-only so no
        # other tenant's routing can ever be affected, even if a future prompt emits
        # the field.
        _guard_hotel = str(getattr(context, "hotel_code", "") or "").strip().lower()
        # ...but NOT while that service is waiting on an answer it just asked for.
        #
        # The router contradicts itself here: it labels the message new_topic and
        # simultaneously routes it to the active service, often with a `reason`
        # that plainly describes a continuation ("the guest's 'yes' is a
        # confirmation to place the in-room dining order"). Only one of those two
        # fields can be right, and this guard used to resolve that tie by
        # discarding the route — which stranded a guest who had just answered the
        # bot's own question: "Shall I place this order?" / "yes" was dumped into
        # the book lane and answered with "I'm here if you'd like to continue with
        # the order or ask about your room's AC". The order was never placed.
        #
        # Resolve the tie by which mistake is RECOVERABLE. Staying on the service
        # is: if the message really did leave that domain, the service agent says
        # so itself with relevant_to_service=false / context_switched=true and the
        # orchestrator re-routes — a path already proven to work. Dropping to book
        # is not: the pending transaction is abandoned mid-way and the guest's
        # answer is lost. So when the service has an open question of its own, the
        # route stands and the agent decides; the guard still fires normally when
        # nothing is pending, which is the leftover-stickiness case it was built for.
        _service_awaiting_reply = bool(str(getattr(context, "pending_action", "") or "").strip())
        if (
            _guard_hotel == "sarovar_portico_sriperumbudur"
            and _topic_relation == "new_topic"
            and route == "service"
            and service_id
            and _last_service_id
            and service_id == _last_service_id
            and not _service_awaiting_reply
        ):
            logger.info(
                "[MAIN_ROUTER] topic_relation=new_topic but route=service on the leftover "
                "active service %r — coercing to route=book (stickiness guard).",
                service_id,
            )
            route = "book"
            service_id = ""

        # ── THE MIRROR: continues_active_service must not land in the book lane ──
        # The same self-contradiction, the other way round. Asked to hold a line
        # under pressure the router wrote:
        #
        #     topic_relation: "continues_active_service"
        #     reason:         "continuing the in-room dining service flow"
        #     route:          "book"      <- sent to the KB answerer anyway
        #     service_id:     ""
        #
        # It had understood the message perfectly and routed it away regardless.
        # These turns carry no food noun of their own - "ill pay extra, i dont
        # care", "just make an exception for me", a bare "yes" - so the message
        # looks like nothing in particular even though the CONVERSATION plainly is
        # something. The book answerer has no cart, no menu and no dining persona,
        # so the guest got "I can help with in-stay requests at Sarovar Portico
        # Sriperumbudur" in the middle of ordering dinner.
        #
        # Same principle as the guard above: enforce the model's OWN classification
        # rather than second-guessing it, and no keyword matching anywhere. If the
        # service really has been left behind, the service agent says so itself
        # (relevant_to_service=false / context_switched=true) and the orchestrator
        # re-routes - the recoverable direction, exactly as reasoned above.
        if (
            _guard_hotel == "sarovar_portico_sriperumbudur"
            and _topic_relation == "continues_active_service"
            and route == "book"
            and not service_id
            and _last_service_id
        ):
            logger.info(
                "[MAIN_ROUTER] topic_relation=continues_active_service but route=book — "
                "returning to the active service %r (continuation guard).",
                _last_service_id,
            )
            route, service_id = "service", _last_service_id

        # ── THE THIRD CONTRADICTION: THE REASON AND THE ID NAME DIFFERENT SERVICES ──
        # The two guards above catch route-vs-topic_relation. This is the same
        # illness in the field that actually decides who answers. Verbatim from
        # the router, on a guest who had ordered food and then spent three turns
        # arranging an airport drop:
        #
        #     route:      "service"
        #     service_id: "in_room_dining"
        #     reason:     "Guest is continuing the TRANSPORT service flow by
        #                  asking about cost; TRANSPORT service owns this domain"
        #
        # It understood the turn perfectly and emitted the wrong id - the service
        # that happened to be active earlier. "how much will it cost" was answered
        # with "Your current cart has: 2 x Chicken Burger... shall I place this
        # order?" in the middle of booking a taxi. The same staleness decides
        # whether a lost room key becomes a ticket or a form.
        #
        # So the id is checked against the message it claims to be about, and only
        # in the sticky case - where the router picked the PREVIOUS service, which
        # is the only way this goes stale. A confident, different service from the
        # live catalogue wins; anything unclear leaves the routing exactly as it
        # was. No keyword matching, no per-service rules.
        if (
            route == "service"
            and service_id
            and _last_service_id
            and service_id == _last_service_id
            and len(in_phase_services) > 1
        ):
            # WITH THE LAST FEW TURNS, not the message alone. "how much will it
            # cost" says nothing by itself - judged on its own the answer is
            # correctly "cannot tell", and the stale service would stand. It is
            # the two transport turns before it that make the answer obvious,
            # which is exactly the context the router had and misused.
            _ctx_lines: list[str] = []
            try:
                for _m in (context.get_recent_messages(6) or []):
                    _r = str(getattr(getattr(_m, "role", None), "value",
                                     getattr(_m, "role", ""))).lower()
                    _t = str(getattr(_m, "content", "") or "").strip()
                    if _t:
                        _ctx_lines.append("%s: %s" % (
                            "Guest" if _r == "user" else "Assistant", _t[:220]))
            except Exception:
                _ctx_lines = []
            _belongs = await self._service_for_the_latest_turn(
                recent_turns=_ctx_lines, latest_message=str(user_message or ""),
                services=in_phase_services)
            if _belongs and _belongs in valid_service_ids and _belongs != service_id:
                logger.info(
                    "[MAIN_ROUTER] the router kept the previous service %r, but this "
                    "message belongs to %r — following the message (stale-service "
                    "guard). reason was: %s",
                    service_id, _belongs, str(raw.get("reason") or "")[:110])
                service_id = _belongs

        # ── PHASE GATE: IN-STAY-ONLY REQUESTS NEVER EXECUTE PRE-ARRIVAL ──────
        # The router now judges `in_stay_only` — "can this ONLY be fulfilled once
        # the guest is physically checked in?" — as a JUDGEMENT, not a keyword
        # list. We enforce it here so the answer cannot be overridden by any
        # later rule: before arrival, such a request must never reach a service,
        # including the generic support/handoff intake. That intake sits in the
        # SAME phase, so it was never phase-blocked, and its "any complaint →
        # open the form" rule re-admitted exactly what the phase gate had just
        # refused (an "AC not working" Engineering ticket for a guest with no
        # room). route=book lets the answerer decline warmly with "once you've
        # checked in" and raises nothing.
        # Same Sarovar gate as the guard above: only this tenant's router prompt
        # emits the field, so no other tenant's routing can change.
        _pre_arrival_phase = (
            self._normalize_identifier(selected_phase_id) in {"pre_booking", "pre_checkin"}
        )
        if (
            _guard_hotel == "sarovar_portico_sriperumbudur"
            and _pre_arrival_phase
            and route == "service"
        ):
            # Ask an independent judge, not the router's own self-report. The
            # router answers this question to fit the route it already chose, so
            # its own in_stay_only was useless — it called a spa booking false.
            # Costs one small call, and only on service turns before arrival.
            _verdict = await self._subject_is_in_stay_only(
                context=context, user_message=user_message,
            )
            if _verdict is True:
                logger.info(
                    "[MAIN_ROUTER] in-stay-only subject in pre-arrival phase %r — "
                    "coercing route=service(%r) to route=book (phase gate).",
                    selected_phase_id,
                    service_id,
                )
                route = "book"
                service_id = ""

        # ── EVIDENCE CHECK: NO INVENTED HANDOFF OFFERS ───────────────────────
        # The HANDOFF-ACCEPTANCE OVERRIDE turns a bare "yes" into a support-intake
        # route, but only when the bot ACTUALLY offered a handoff on the previous
        # turn. It was firing on offers that never happened: the bot flatly
        # declined a spa request ("that's available only once you're checked in"),
        # a chip put "Yes, please" in front of the guest, and the router reasoned
        # "guest accepts the prior offer to have the team reach out" — an offer
        # that appears nowhere in the transcript.
        # So the router must now QUOTE the offer verbatim in `handoff_offer_quote`.
        # A quote that is not literally present in the previous assistant turn is
        # proof the offer was invented, and the route is dropped to book. This
        # checks only the model's own cited evidence — it makes no judgement about
        # intent, and a guest-initiated "call me back" carries no quote and is
        # unaffected (it never depended on a prior offer).
        _handoff_quote = str(raw.get("handoff_offer_quote") or "").strip()

        # ── A YES THAT ACCEPTED NOTHING OPENS NOTHING (every tenant) ─────────
        # A quote being REAL does not make it an offer. Zone declined a cake with
        # "Once you're checked in, the team can help with that.", a chip put
        # "Yes, please" in front of the guest, and the router quoted that very
        # sentence as the handoff being accepted - quote verified, route stands,
        # staff form opened, cake ticket raised. Nobody had offered anything, and
        # a form with no request behind it is not a smaller bug than the ticket.
        # We already know better: the offer-acceptance judge ran earlier in this
        # same turn and answered "pure agreement, offer_kind=none". That verdict
        # was being dropped because it had no annotation to make.
        # Narrow by construction: it fires only when the router ITSELF claims an
        # accepted offer (non-empty handoff_offer_quote) AND the judge ran and
        # found none. A guest-initiated "connect me to staff" carries no quote; a
        # genuine offer makes the judge answer show/add_item/invite_more (and, for
        # admin-managed hotels, handoff), so this cannot even match it. Without
        # `handoff` a real staff offer - "Would you like our team to reach out?" -
        # reads as none and this guard turns every yes to it into a dead end.
        if route == "service" and _handoff_quote and self._bare_yes_accepted_nothing(
            session_id=str(getattr(context, "session_id", "") or ""),
            user_message=user_message,
        ):
            logger.info(
                "[MAIN_ROUTER] bare agreement with NO offer behind it (judge: "
                "offer_kind=none) but the router claimed an accepted handoff "
                "(quote=%r) - nothing was accepted, coercing route=service(%r) to "
                "route=book.",
                _handoff_quote[:80], service_id,
            )
            route = "book"
            service_id = ""

        # Every hotel, not just Sarovar: Mango's router quoted an OLDER form
        # thank-you / "In the meantime..." line as the offer a "Yes" accepted, while
        # the real last message asked about photos. Over 45 real support routes
        # that cited an offer, 43 quotes were in the last message (unchanged) and
        # the 2 that were not were both this bug.
        if route == "service":
            _prev_assistant = self._normalize_quote_for_evidence(last_assistant_message)
            _quote_verified = bool(_handoff_quote) and (
                self._normalize_quote_for_evidence(_handoff_quote) in _prev_assistant
            )
            # The router declares WHY it is opening support-intake. Only one of
            # those reasons rests on something we can check — "the guest accepted
            # an offer I made" — and that claim must come with the sentence it
            # rests on. Checking the quote alone was not enough: the model could
            # simply leave the field empty and route anyway, which is how a flat
            # spa refusal plus a bare "Yes, please" still reached the pre-arrival
            # form. A claim of acceptance now has to carry its evidence.
            _support_reason = str(raw.get("support_reason") or "").strip().lower()
            logger.info(
                "[MAIN_ROUTER] support-intake candidate svc=%r support_reason=%r "
                "quote=%r quote_verified=%s",
                service_id, _support_reason, _handoff_quote[:60], _quote_verified,
            )
            # Only the plainly checkable failure is enforced here: the router
            # cited a sentence that is not in the previous turn, so the offer was
            # invented. Whether a REAL quoted sentence is genuinely an offer is a
            # question of meaning — the router proved it will quote any sentence
            # to justify itself (it cited "Once you're checked in, Aloe Spa offers
            # massage" as a handoff offer), so that judgement belongs to the
            # independent in-stay check above, not to another self-report.
            if _handoff_quote and not _quote_verified:
                logger.info(
                    "[MAIN_ROUTER] handoff_offer_quote is NOT in the previous turn "
                    "(reason=%r quote=%r) — invented offer, coercing route=service(%r) "
                    "to route=book.",
                    _support_reason, _handoff_quote[:80], service_id,
                )
                route = "book"
                service_id = ""

        # ── ROOM BOOKING = WEBSITE, HANDLED BY THE ROOM-BOOKING AGENT ───────
        # Room/stay booking is completed on the hotel website. The room-booking
        # SERVICE AGENT already has the "WEBSITE ONLY" contract (it emits the
        # <<SHOW_BOOKING_BUTTON>> + room types + current offers and never opens a
        # form — see the ROOM BOOKING WEBSITE ONLY rules in the service prompt).
        # We deliberately KEEP route=service here so room bookings (generic OR a
        # named room type like "book the suite") reach that agent and get the
        # website link + info + offers. We do NOT downgrade to route=book — the
        # book answerer lacks the booking-button contract and would wrongly
        # CASE-B "available once you're checked in"-decline a named-room booking.
        pass

        # ── AN OPEN ORDER OWNS THE ORDER QUESTIONS ─────────────────────────
        # Finishing another team's ticket completes a flow, which clears the
        # service binding. The next food turn then routed to NO service at all
        # (`service=` empty, action=respond_only), and everything outside the
        # dining lane is blind to the cart - so "what did I order" was answered
        # from the chat transcript, where a red wine the guest had asked about
        # and been REFUSED reads exactly like one they ordered. The stored cart
        # was correct the whole time; nobody read it.
        #
        # Only reached when an order is actually open AND the turn did not land
        # in the dining lane - i.e. only in the window where the bug lives, so a
        # normal food turn pays nothing for it. The judgement of whether the
        # guest is talking about their order stays with a model; this decides
        # nothing about meaning on its own.
        try:
            _pd_open = context.pending_data if isinstance(context.pending_data, dict) else {}
            _open_cart = list(_pd_open.get("_ird_cart") or [])
            _placed = list(_pd_open.get("_ird_placed_orders") or [])
            _dining_id = ""
            for _svc in (services_snapshot or []):
                if not isinstance(_svc, dict):
                    continue
                if not bool(_svc.get("is_active", True)):
                    continue
                # Match the service's ID, which is already the canonical
                # `in_room_dining`. Matching only its NAME silently disabled this
                # entire guard: the service is called "In-Room Dining", and the
                # hyphen survives normalisation as `in-room_dining`, which is
                # neither spelling below. `_dining_id` was therefore always empty,
                # the guard never fired once, and every order question that landed
                # outside the dining lane was answered without the cart - which is
                # exactly the ungrounded recall it was written to prevent. The name
                # is still accepted, with hyphens folded, for a hotel whose service
                # ids are spelled differently.
                _cand = {
                    self._normalize_identifier(_svc.get("id")),
                    self._normalize_identifier(_svc.get("name")).replace("-", "_"),
                }
                if _cand & {"in_room_dining", "inroomdining"}:
                    _dining_id = self._normalize_identifier(_svc.get("id"))
                    break
            _already_dining = route == "service" and service_id and service_id == _dining_id
            if (_open_cart or _placed) and not _dining_id:
                # This guard failed silently for its whole life because it only ever
                # logged on the path where it FIRED. Say so when there is an order to
                # protect and no service to hand it to.
                logger.warning(
                    "[IRD_REENTRY] an order is open (%d in progress, %d placed) but no "
                    "in-room-dining service resolved from the snapshot - the guard "
                    "cannot run and order questions will be answered without the cart",
                    len(_open_cart), len(_placed),
                )
            if (_open_cart or _placed) and _dining_id and not _already_dining:
                _about_order = await self._guest_is_talking_about_their_food_order(
                    user_message=user_message,
                )
                if _about_order:
                    logger.info(
                        "[IRD_REENTRY] an order is open (%d in progress, %d placed) and "
                        "this turn routed to %r - the guest is asking about that order, "
                        "so it goes back to in-room dining instead of being answered "
                        "without it",
                        len(_open_cart), len(_placed), service_id or route,
                    )
                    route, service_id = "service", _dining_id
        except Exception as _reentry_exc:
            # Never let this cost a turn: on any failure the routing the model
            # chose stands, exactly as before.
            logger.warning("[IRD_REENTRY] skipped (non-fatal): %r", _reentry_exc)

        valid_property_ids = {
            self._normalize_identifier(item.get("id"))
            for item in _router_available_properties
            if isinstance(item, dict) and self._normalize_identifier(item.get("id"))
        }
        # 5.4 BRAND-WIDE FILTER SENTINEL. For a whole-portfolio filter question
        # ("which hotels are in the mountains / allow pets"), gpt-5.4-mini reliably
        # REASONS the scope correctly (its own `reason` says "brand-wide filter,
        # available set is all hotels") but then fails to MECHANICALLY enumerate all
        # ~24 ids and anchors to the active set instead. So its prompt lets it emit a
        # single "*" to mean "every hotel"; we expand that to the full catalog here.
        # The routing DECISION stays with the model — this only spares it from typing
        # 24 ids. Only the 5.4 self-contained prompt instructs the sentinel; the 4.1
        # prompt never emits it, so 4.1 is unaffected (and the gate makes that explicit).
        _raw_selected = list(raw.get("selected_property_ids") or [])
        _wants_all_hotels = self._model_uses_self_contained_prompts(model) and any(
            str(value).strip() == "*"
            or str(value).strip().lower().strip("_") in {
                "all", "all_hotels", "all_properties", "every_hotel", "everyhotel", "everyproperty"
            }
            for value in _raw_selected
        )
        selected_property_ids: list[str] = []
        if _wants_all_hotels:
            for item in _router_available_properties:
                if not isinstance(item, dict):
                    continue
                property_id = self._normalize_identifier(item.get("id"))
                if property_id and property_id not in selected_property_ids:
                    selected_property_ids.append(property_id)
            logger.info(
                "router: expanded brand-wide FILTER sentinel '*' -> %d properties",
                len(selected_property_ids),
            )
        else:
            for value in _raw_selected:
                property_id = self._normalize_identifier(value)
                if not property_id or property_id not in valid_property_ids or property_id in selected_property_ids:
                    continue
                selected_property_ids.append(property_id)

        valid_book_ids = {str(item.get("id") or "").strip() for item in book_catalog if isinstance(item, dict)}
        # No cap on the number of books the router may pick — the model
        # decides how many are needed for the question. Validity is the only
        # filter (must exist in the catalog and not be a duplicate).
        selected_book_ids: list[str] = []
        for value in list(raw.get("book_ids") or []):
            book_id = str(value or "").strip()
            if not book_id or book_id not in valid_book_ids or book_id in selected_book_ids:
                continue
            selected_book_ids.append(book_id)
        if route == "book" and selected_book_ids:
            selected_book_ids = self._expand_book_ids_for_unresolved_multi_property(
                user_message=str(user_message or ""),
                selected_book_ids=selected_book_ids,
                book_catalog=book_catalog if isinstance(book_catalog, list) else [],
                active_property_ids=_active_prop_slugs,
                max_selected_book_ids=max(50, len(valid_book_ids)),
            )

        if route == "service" and service_id:
            service_row = next(
                (
                    item for item in in_phase_services
                    if self._normalize_identifier(item.get("id")) == service_id
                ),
                {},
            )
            # Zone-only. The decline text below names the Zone catalog by brand,
            # and "which of our hotels?" is only a real question for Zone, the one
            # multi-property tenant. Left ungated it fired for every tenant: on
            # single-property Sarovar, `_service_route_names_unsupported_property_scope`
            # reads "<adjective> hotel" as a property name, so "this is the worst
            # hotel ever, staff was rude ... i want to complain to the manager"
            # matched "worst" against an empty catalog and answered a Sarovar guest
            # with Zone's decline — at the moment they were complaining.
            _is_zone_brand = _hotel_code.strip().lower() == "zone"
            if _is_zone_brand and self._is_support_handoff_service(
                service_id=service_id,
                service_name=str(service_row.get("name") or ""),
                description=str(service_row.get("description") or ""),
            ) and self._service_route_names_unsupported_property_scope(
                user_message=user_message,
                router_reason=raw.get("reason"),
                available_properties=_router_available_properties,
                available_locations=_router_available_locations,
                selected_property_ids=selected_property_ids,
            ):
                route = "clarify"
                service_id = ""
                selected_property_ids = []
                selected_book_ids = []
                clarification_question = (
                    "I don't see that property in our Zone hotel list. "
                    "Which supported Zone hotel or destination should I help you with?"
                )
                raw["reason"] = (
                    f"{str(raw.get('reason') or '').strip()} "
                    "Support route blocked because the named property/location did not match the catalog."
                ).strip()
                raw["clarification_question"] = clarification_question

        clarification_question = str(raw.get("clarification_question") or "").strip()
        if route == "clarify" and not clarification_question:
            route = "auto"

        # ── A STALE HANDOFF OFFER MUST NOT MOVE THE PROPERTY ─────────────────
        # "Connect me to staff" makes the router look for the offer being
        # accepted. It will reach BACK past the turns since and cite an older
        # "want to explore more about <X>" line — then file the request against
        # X, even though the guest has moved on. Observed: the guest switched
        # from Calangute to Parra, the bot answered about Parra, the guest asked
        # for staff, and the router quoted the Calangute offer from three turns
        # earlier and put the ticket on Calangute.
        #
        # The router's own `handoff_offer_quote` is the evidence: an offer being
        # accepted NOW must appear in the turn immediately before. When it does
        # not, the acceptance is anchored to something stale, so the route is
        # kept (the guest does want staff) but the property is left on the
        # active scope instead of being switched by it.
        if route == "service" and _handoff_quote and _active_prop_slugs:
            _prev_norm = self._normalize_quote_for_evidence(last_assistant_message)
            _quote_in_prev = self._normalize_quote_for_evidence(_handoff_quote) in _prev_norm
            _active_list = [str(s).strip() for s in _active_prop_slugs if str(s).strip()]
            if not _quote_in_prev and _active_list and selected_property_ids != _active_list:
                logger.info(
                    "[MAIN_ROUTER] handoff quote is not in the previous turn — keeping "
                    "the active property scope %r instead of the router's %r (stale offer).",
                    _active_list, selected_property_ids,
                )
                selected_property_ids = list(_active_list)

        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        # Weather classification — emitted by the router so Python can fetch
        # live weather without a second LLM call. "" / "property" / "<city>".
        weather_for = str(raw.get("weather_for") or "").strip()

        # Broaden classification — true when the guest explicitly asks to
        # explore OTHER / DIFFERENT properties. Python uses this to wipe the
        # stale property scope so the answerer can fan out across all
        # properties instead of repeating the same narrow subset.
        broaden_properties = bool(raw.get("broaden_properties"))

        response_language = self._response_language_preference(
            user_message=language_input,
            history_rows=self._history_preview(context, max_messages=8, max_chars=2400),
            last_assistant_message=last_assistant_message,
            router_language=raw.get("response_language"),
        )

        return {
            "route": route,
            "service_id": service_id,
            "selected_property_ids": selected_property_ids,
            "book_ids": selected_book_ids,
            "weather_for": weather_for,
            "broaden_properties": broaden_properties,
            "response_language": response_language,
            "clarification_question": clarification_question,
            # ── OFF-TOPIC IS DECIDED HERE, NOT BY THE ANSWERING MODEL ─────────
            # A scope rule sat in the book answerer only, so during a stay - where
            # the service lane answers - "tell me a joke" and "what is 27 x 43"
            # were simply answered. Both scope directives were also measured to
            # lose the joke case outright: the model that is mid-conversation with
            # the guest wants to be helpful, and no amount of emphasis in its own
            # prompt beat that. So the judgement moves to the router, which is not
            # in that conversation and has no such pull, and the lanes are handed
            # its verdict as a late instruction instead of a rule to weigh.
            # The model sometimes writes the flag as the STRING "false", and
            # bool("false") is True - that turned "i forgot my ID cards" and 35
            # other real hotel questions into a forced off-topic decline. Only a
            # real true, or the text "true", counts.
            "off_topic": raw.get("off_topic") is True
            or str(raw.get("off_topic") or "").strip().lower() == "true",
            "reason": str(raw.get("reason") or "").strip(),
            "confidence": confidence,
        }






    @staticmethod
    def _ird_edit_window_expired(*, category: str, created_at: Any) -> bool:
        """Is this food order past the point where the guest can still change it?

        In-room dining goes to the kitchen, so a change or cancellation is only
        honest for a short while after it is raised. The window is
        settings.ird_edit_window_minutes (0 = never lock). Applies only to
        in-room dining — housekeeping, maintenance and the rest stay editable.
        Returns False whenever it cannot tell, so nothing is locked by accident.
        """
        window = int(getattr(settings, "ird_edit_window_minutes", 5) or 0)
        if window <= 0:
            return False
        cat = str(category or "").strip().lower()
        if not cat or not any(
            marker in cat for marker in ("dining", "food", "beverage", "restaurant")
        ):
            return False
        raw = str(created_at or "").strip()
        if not raw:
            return False
        try:
            from datetime import datetime as _dt, timezone as _tz

            stamp = _dt.fromisoformat(raw.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=_tz.utc)
            age_min = (_dt.now(_tz.utc) - stamp).total_seconds() / 60.0
            return age_min > window
        except Exception:
            return False

    @staticmethod
    async def _ird_ticket_statuses(row: "dict[str, Any]") -> "dict[str, str]":
        """The live status of every ticket the order book refers to.

        Our own new_bot_tickets row is written once and never hears anything back,
        so it reads "open" forever. The staff dashboard's table lives in the same
        database we are already connected to, so ask it directly - and ask for all
        ten refs in ONE query rather than ten, because this runs on every dining
        turn.

        BEING ASSIGNED IS NOT BEING STARTED. The dashboard auto-assigns on
        creation, in the same second, by SYSTEM(LLM). Reading that as "the kitchen
        has begun" makes every order untouchable the instant it exists, collapsing
        the edit window to zero and sending a second ticket for food already being
        made. Only a human claim counts, which is what the auto-assign flag
        distinguishes. Same rule as ticketing_service._staff_have_picked_up.

        Returns {} when it cannot tell - which means "no opinion", and the clock
        decides on its own exactly as it did before.
        """
        refs = [
            str(row.get("ticket_ref_%d" % n) or "").strip()
            for n in range(1, 11)
        ]
        refs = [r for r in refs if r]
        if not refs:
            return {}
        try:
            from models.database import AsyncSessionLocal
            from sqlalchemy import text as _text

            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    _text("SELECT id, status, assigned_id, ticket_auto_assign "
                          "FROM GHN_OPERATION_TICKETS WHERE id IN :refs"),
                    {"refs": tuple(refs)},
                )).mappings().all()
            out: dict[str, str] = {}
            for r in rows:
                status = str(r.get("status") or "").strip().lower()
                try:
                    assigned_id = int(r.get("assigned_id") or 0)
                except (TypeError, ValueError):
                    assigned_id = 0
                if status in ("", "open") and assigned_id > 0 and not bool(
                    r.get("ticket_auto_assign")
                ):
                    status = "in progress"   # a person claimed it
                out[str(r.get("id"))] = status
            return out
        except Exception as exc:
            logger.info("[IRD_BOOK] could not read live ticket state for %s (%s) - "
                        "the edit window decides on its own", refs, exc)
            return {}

    @staticmethod
    def _hotel_now() -> datetime:
        """The guest's wall clock. One definition, so the serving times written into
        the menu and the availability list built from the same table can never
        disagree about what hour it is."""
        try:
            from zoneinfo import ZoneInfo as _ZI

            return datetime.now(
                _ZI(str(getattr(settings, "hotel_timezone", "") or "UTC").strip() or "UTC")
            )
        except Exception:
            return datetime.now()

    @classmethod
    def _build_menu_availability_payload(
        cls, *, hotel_code: str, menu_text: str, now_local: datetime
    ) -> dict[str, Any]:
        """The timing half of the service payload, as keys to merge in.

        Normally one key, `menu_availability`, holding a short list of dish NAMES
        resolved from the per-dish table. No section-to-dish mapping and no clock
        arithmetic is left for the model.

        If that table is missing or unreadable this falls back to the OLD
        section-prose fields rather than shipping nothing, so a deploy that loses
        the config is no worse than the behaviour before it existed. Both cannot be
        sent together: two overlapping accounts of what is open is exactly the kind
        of ambiguity that makes the model pick the wrong one.
        """
        try:
            from services.ird_availability import build_availability_block, covers

            block = build_availability_block(hotel_code, now_local)
            if block:
                return {"menu_availability": block}
            # An empty block means the table loaded and nothing is restricted right
            # now. That is a real answer - everything is available - so say it
            # plainly instead of falling back and re-introducing the old ambiguity.
            if covers(hotel_code):
                return {
                    "menu_availability": {
                        "as_of": now_local.strftime("%H:%M"),
                        "mode": "everything_is_available_now",
                        "items": [],
                        "rule": (
                            "Every dish on the menu can be ordered right now. Do "
                            "not refuse anything on timing grounds this turn."
                        ),
                    }
                }
        except Exception as exc:  # never let a config problem cost a turn
            logger.warning(
                "[IRD_AVAIL] per-dish availability unavailable (%r) - falling back "
                "to the menu's section prose", exc,
            )
        return {
            "menu_serving_windows": cls._build_menu_serving_windows(menu_text, now_local),
            "menu_timing_note": (
                "Only the sections listed in menu_serving_windows have stated "
                "hours. Every other item on the menu has no timing and is "
                "available NOW. If every listed section is closed, that means "
                "ONLY those sections are unavailable - it does NOT mean the "
                "kitchen is shut or the menu is unavailable."
            ),
        }

    @staticmethod
    def _ird_unplaced_cart_lines(pending: Any) -> list[dict[str, Any]]:
        """The cart lines the kitchen does NOT already hold.

        The cart deliberately survives placement so a dish added inside the edit
        window merges onto the SAME ticket instead of raising a second one. The
        cost is that every line of an order already with the kitchen stays in the
        cart looking exactly like an order still being built - and it was handed
        to the model that way twice over: as `food_order_status.not_yet_placed`,
        whose note says in capitals that nothing there is being cooked, and as
        `current_confirmed_order`, which the service prompt defines as "the
        IN-PROGRESS cart not yet placed". So one dish was described both as on
        its way and as never sent, and a guest who asked "what did I order" a
        minute after ordering was asked to place the same food again.

        `_ird_open_order_items` is what the open ticket actually says - written
        at placement and rewritten whenever that ticket is edited. Subtract it,
        by dish, on quantity, which is the same subtraction chat_service already
        does when it decides what the kitchen ticket carries, so the two records
        cannot drift apart. An untouched placed order leaves nothing behind; a
        second helping ("another idli" - declared 2 against the 1 already sent)
        correctly leaves 1 still to place.

        Returns the full cart whenever there is nothing to subtract, so every
        pre-placement turn behaves exactly as it did before.
        """
        pd = pending if isinstance(pending, dict) else {}
        cart = [
            l for l in (pd.get("_ird_cart") or [])
            if isinstance(l, dict) and str(l.get("item") or "").strip()
        ]
        if not cart:
            return []
        # A cart reset for a fresh order has nothing stale in it: everything in it
        # was asked for after the reset, so subtracting the order that just closed
        # would silently delete a repeat and send the guest nothing.
        if pd.get("_ird_fresh_order"):
            return list(cart)

        def _qty(row: dict[str, Any]) -> int:
            try:
                return max(1, int(row.get("qty") or 1))
            except (TypeError, ValueError):
                return 1

        placed: dict[str, int] = {}
        for l in (pd.get("_ird_open_order_items") or []):
            if not isinstance(l, dict):
                continue
            # Readers of this store disagree about the key; both are written.
            key = str(l.get("item") or l.get("name") or "").strip().lower()
            if key:
                placed[key] = placed.get(key, 0) + _qty(l)
        # THE LEDGER IS THE FALLBACK. `_ird_open_order_items` is POPPED when an
        # order is split off, so after a split the code forgets what the kitchen
        # is holding - and every line of the cart looks unplaced again. That is
        # how a payload came to describe the same two burgers as both "already
        # with the kitchen" (orders) and "not yet placed, still needs confirming"
        # (not_yet_placed), and the model, reading the second, sent them again.
        # `_ird_placed_orders` survives and still knows, so ask it. Quantities are
        # taken per dish at their highest, not summed, so a dish that appears on
        # two orders does not over-subtract a genuine repeat.
        if not placed:
            for _row in (pd.get("_ird_placed_orders") or []):
                if not isinstance(_row, dict):
                    continue
                for _it in (_row.get("items") or []):
                    if not isinstance(_it, dict):
                        continue
                    key = str(_it.get("item") or _it.get("name") or "").strip().lower()
                    if key:
                        placed[key] = max(placed.get(key, 0), _qty(_it))
        if not placed:
            return list(cart)

        out: list[dict[str, Any]] = []
        for l in cart:
            key = str(l.get("item") or "").strip().lower()
            want = _qty(l)
            have = int(placed.get(key) or 0)
            placed[key] = max(0, have - want)
            if want - have <= 0:
                continue  # the kitchen already has all of this dish
            row = dict(l)
            row["qty"] = want - have
            out.append(row)
        return out

    async def _subject_is_in_stay_only(
        self,
        *,
        context: ConversationContext,
        user_message: str,
    ) -> bool | None:
        """Can the thing this guest is asking for happen before they check in?

        Asked as its own question, of a model that has not been told anything
        about routing. The main router was given the same question as an output
        field and answered it to fit the route it had already picked — it called
        a spa booking "not in-stay-only" and quoted an unrelated sentence as
        proof of a handoff offer. A judge with no decision to defend does not
        have that incentive.

        Reads the recent conversation, because the message itself is often just
        "yes" and the subject lives in the turns before it.

        Returns True (in-stay only), False (fine before arrival), or None when
        the check could not run — callers must treat None as "allow", so a
        failure here never blocks a guest.
        """
        try:
            history = self._history_preview(context, max_messages=6, max_chars=1600)
            system_prompt = (
                "A hotel guest has BOOKED but has NOT ARRIVED yet — they have no room.\n"
                "Work out WHAT THEY ARE ASKING FOR, then answer one question: could it "
                "happen at all before they check in?\n"
                'Return STRICT JSON only: {"subject":"<what they are asking for, 6 words '
                'max>","in_stay_only":true|false}\n'
                "in_stay_only=true when it needs them to be physically in the hotel: a "
                "spa or massage session, a fault in their room (AC, TV, wifi, plumbing), "
                "housekeeping or towels, in-room dining, noise in the hallway, a late "
                "check-out, anything about 'my room'.\n"
                "in_stay_only=false for anything doable before arrival: a room or stay "
                "booking, arrival pickup, a celebration setup to note for arrival, "
                "billing, an existing reservation, or asking to speak to a person about a "
                "pre-arrival matter. Also false for a purely INFORMATIONAL question — one "
                "that is answered by telling them a fact and needs nothing done ('what "
                "time does the spa open', 'do you have a pool', 'how far is the airport').\n"
                "DO NOT MISTAKE A REQUEST FOR A QUESTION. 'Can I get a massage', 'I need "
                "towels', 'book me a spa session', 'my AC is not working' are the guest "
                "trying to OBTAIN something or get something done — in_stay_only is true "
                "for those even though they are phrased as questions, and it stays true "
                "even if the thing turns out to be unavailable or the bot has already "
                "refused it. Ask yourself: are they after a FACT, or after an OUTCOME? "
                "Wanting an outcome that needs them on-site is in_stay_only=true.\n"
                "THE MESSAGE MAY BE A BARE 'yes' / 'ok' / 'please do'. That has no subject "
                "of its own — read `recent_conversation` and judge WHAT THEY ARE AGREEING "
                "TO, which is the request still on the table. Agreeing to something does "
                "not change what it is: a guest who was told a massage cannot be arranged "
                "and then says 'yes please' is still asking about the massage, so "
                "in_stay_only stays true.\n"
                "Judge only this. You are not deciding what the bot should do next."
            )
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "recent_conversation": history,
                                "current_message": str(user_message or "").strip(),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=120,
                trace_context={"responder_type": "main", "agent": "in_stay_only_judge"},
            )
            if not isinstance(raw, dict) or "in_stay_only" not in raw:
                return None
            verdict = bool(raw.get("in_stay_only"))
            logger.info(
                "[IN_STAY_JUDGE] subject=%r in_stay_only=%s msg=%r",
                str(raw.get("subject") or "")[:50], verdict, str(user_message or "")[:50],
            )
            return verdict
        except Exception as exc:
            logger.warning("_subject_is_in_stay_only failed (non-fatal): %s", exc)
            return None

    @staticmethod
    async def _own_property_names(hotel_code: str) -> list[str]:
        """This hotel's own property names and the other names guests use for
        them, from the cached property catalogue. [] when unknown."""
        if not str(hotel_code or "").strip():
            return []
        try:
            from services.kb_table_reader import get_property_catalog_db

            names: list[str] = []
            for row in await get_property_catalog_db(hotel_code=hotel_code) or []:
                for value in [row.get("name")] + list(row.get("aliases") or []):
                    text = str(value or "").strip()
                    if text and text not in names:
                        names.append(text)
            return names[:60]
        except Exception:
            return []

    @staticmethod
    async def _property_named_by_message(*, hotel_code: str, message: str) -> str:
        """The id of the ONE property whose name or alias is the whole message, else "".

        A tapped hotel chip, or the hotel typed on its own. Matched on the whole
        message, as the property intro is, so "is Retro pet friendly?" is not a pick.
        """
        def _flat(value: Any) -> str:
            return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))

        body = _flat(message)
        if not body or not str(hotel_code or "").strip():
            return ""
        try:
            from services.kb_table_reader import get_property_catalog_db

            hits = {
                str(row.get("id") or "").strip()
                for row in await get_property_catalog_db(hotel_code=hotel_code) or []
                if body in {_flat(n) for n in [row.get("name"), *list(row.get("aliases") or [])]}
            }
        except Exception:
            return ""
        hits.discard("")
        return next(iter(hits)) if len(hits) == 1 else ""

    async def _scope_verdict(
        self,
        *,
        user_message: str,
        last_bot_message: str = "",
        hotel_code: str = "",
    ) -> "dict | None":
        """Is this message something a hotel assistant may answer at all?

        Its own question, of a model with no route to defend - the same shape as
        `_subject_is_in_stay_only`, and for the same reason. Scope was decided in
        two places that both fail open: an `off_topic` field buried in a 262,000-
        character router prompt, which the model simply omits from its JSON (a
        missing key reads as False, and `grep -c SCOPE` across ten server logs is
        0 - it has never once fired), and a paragraph of prose in the answerer's
        prompt that it is free to ignore. It did: asked for the capital of
        Mongolia it answered "Ulaanbaatar", and the next turn answered
        "Ulaanbaatar won the 2011 Cricket World Cup" - a confident hallucination
        carried out of its own previous reply.

        Returns None when it cannot decide, and the caller leaves the answer
        alone. Deciding is the only job here; the CODE enforces the verdict.
        """
        msg = str(user_message or "").strip()
        if not msg:
            return None
        # OUR OWN VENUES ARE NOT ANOTHER BUSINESS (Pre-booking tab hotels). Shown only
        # the hotel names, the judge called "book one workhard venue" a booking with a
        # different company, and T24's own meeting room got the refusal (prod, 15 Oct).
        # The hotel's named places come from the same DB row the router reads.
        _named_places = ""
        try:
            from services import prebooking_settings_service as _pbs_judge

            if _pbs_judge.is_admin_managed(str(hotel_code or "").strip().lower()):
                _named_places = str(await self._db_prompt_text("router.hotel_facts") or "").strip()
        except Exception:
            _named_places = ""
        try:
            raw = await self._chat_with_json(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You screen messages sent to a HOTEL's guest assistant. "
                            "Decide only whether the assistant may answer this one.\n"
                            'Return STRICT JSON: {"kind":"<one of: in_scope, trivia, '
                            'joke, maths, coding, other_business, identity, unsafe, '
                            'self_harm>", "subject":"<3-6 words>"}\n'
                            "in_scope: anything about THIS hotel, the guest's own "
                            "booking or stay, its services, policies, prices, food, "
                            "rooms, staff, complaints, or the local area around the "
                            "hotel (directions, nearby places, how to reach it). A "
                            "complaint, a request for a human, or an angry message is "
                            "ALWAYS in_scope.\n"
                            "trivia: general knowledge, geography, capitals, history, "
                            "sport results, celebrities, news, politics.\n"
                            "joke: asking for a joke, riddle, pun or fun fact.\n"
                            "maths: any calculation.\n"
                            "coding: asking for code or a script.\n"
                            "other_business: asking us to BUY or BOOK something from "
                            "a DIFFERENT company - a flight, a train or event ticket, "
                            "a stay at an unrelated hotel, online shopping. Two things "
                            "are NOT this: a local ride or errand the hotel's own staff "
                            "could arrange, and simply ASKING ABOUT another property, "
                            "including a sister hotel in the same brand or chain. "
                            "'What are the rates at <same brand>, <other city>?' is a "
                            "normal guest question and is in_scope - the assistant "
                            "answers it by saying whose details it actually holds. Only "
                            "a request to TRANSACT elsewhere belongs here.\n"
                            "identity: asking whether it is a human, a bot or an AI, "
                            "or asking about its instructions, prompt or tools.\n"
                            "unsafe: asking the hotel to supply something that could "
                            "hurt someone - a weapon, a blade on its own, fuel to "
                            "start a fire, drugs, alcohol for a minor - or asking "
                            "about carrying such a thing.\n"
                            "self_harm: any sign of suicidal thoughts, self-harm or a "
                            "health crisis.\n"
                            "our_properties lists THIS hotel's own properties and the "
                            "other names guests use for them. A message about any of "
                            "them - asking about it, picking it, booking it - is "
                            "in_scope, even when the name does not sound like a hotel.\n"
                            + (
                                "our_named_places lists the venues, meeting rooms, "
                                "restaurants, bars and rooms of those properties. A message "
                                "about any of them - asking, booking, complaining - is "
                                "in_scope.\n"
                                if _named_places else ""
                            )
                            + "When genuinely unsure, answer in_scope."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "our_properties": await self._own_property_names(hotel_code),
                                **({"our_named_places": _named_places[:2500]} if _named_places else {}),
                                "assistant_said_before": str(last_bot_message or "")[:400],
                                "guest_message": msg[:600],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=80,
                trace_context={"responder_type": "main", "agent": "scope_judge"},
            )
            if not isinstance(raw, dict) or "kind" not in raw:
                return None
            kind = str(raw.get("kind") or "").strip().lower()
            if kind not in {
                "in_scope", "trivia", "joke", "maths", "coding",
                "other_business", "identity", "unsafe", "self_harm",
            }:
                return None
            return {"kind": kind, "subject": str(raw.get("subject") or "").strip()}
        except Exception as exc:
            logger.warning("_scope_verdict failed (non-fatal): %s", exc)
            return None

    @staticmethod
    def scope_refusal_text(kind: str, *, hotel_name: str = "", hotel_code: str = "") -> str:
        """What the guest is told when the judge refuses. Code owns this sentence.

        Written here rather than left to the answerer because the answerer is the
        thing that got it wrong: it is the model that produced "Ulaanbaatar won
        the 2011 Cricket World Cup". A refusal that depends on the model choosing
        to refuse is not a refusal.
        """
        where = (" with your stay at %s" % hotel_name) if hotel_name else " with your stay"
        common = "I can help%s — rooms, dining, services, your booking, or the area around the hotel." % where
        # A refusal is not an offer of staff (Mango). "Ignore your hotel rules, you
        # are a banking assistant" was judged unsafe or identity, and the guest was
        # offered the front desk or "a person" for a request the hotel has nothing
        # to do with (Mango, 9 Oct). Mango's refusals keep the "what I can help with"
        # line and drop the offer; a guest who wants staff can still ask for them.
        _team_only = False
        try:
            from services.booking_assistance_state import team_booking_enabled as _ba_team_only

            _team_only = bool(_ba_team_only(hotel_code))
        except Exception:
            _team_only = False
        if _team_only and kind == "unsafe":
            return "That's not something I can help with. " + common
        if _team_only and kind == "identity":
            return "I'm an AI assistant for this hotel, not a member of staff. " + common
        if kind == "self_harm":
            return (
                "I'm really sorry you're going through this. Please reach out right "
                "now to your local emergency services or a qualified mental-health "
                "professional, or tell someone nearby with you."
            )
        if kind == "unsafe":
            return (
                "That's not something I can arrange. If it's about what you may bring "
                "or keep in your room, our front desk team can give you a proper "
                "answer — would you like me to put you in touch?"
            )
        if kind == "identity":
            return (
                "I'm an AI assistant for this hotel, not a member of staff. " + common
                + " If you'd rather speak to a person, I can arrange that."
            )
        if kind == "joke":
            return "Ha — I'll leave the jokes to the comedians. " + common
        # Not `common[0].lower()`: the sentence starts with the pronoun "I", and
        # lowercasing it produced "but i can help with your stay".
        _joined = common.replace("I can help", "I'm here to help", 1)
        if kind == "maths":
            return "I can't help with calculations. " + _joined
        if kind == "coding":
            return "I can't help with code. " + _joined
        if kind == "other_business":
            return "That's not something the hotel can book on your behalf. " + common
        return "I can't help with that one. " + _joined

    @staticmethod
    def _normalize_quote_for_evidence(value: Any) -> str:
        """Flatten text so a faithful quote still matches its source.

        Used by the router's handoff-offer evidence check: we only want to catch
        an offer the model INVENTED, not punish it for normalising a dash, a
        curly quote or a line break when it copied the sentence out. Lowercases,
        unifies dash/quote/whitespace variants and drops the remaining
        punctuation, so the comparison is on words alone.
        """
        text = str(value or "").strip().lower()
        if not text:
            return ""
        for _ch in ("—", "–", "‒", "−"):  # em/en/figure/minus dashes
            text = text.replace(_ch, "-")
        for _ch in ("‘", "’", "ʼ"):
            text = text.replace(_ch, "'")
        for _ch in ("“", "”"):
            text = text.replace(_ch, '"')
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _apply_main_router_route_hint(
        self,
        *,
        decision: OrchestrationDecision,
        route_hint: dict[str, Any],
        allowed_service_ids: list[str],
    ) -> OrchestrationDecision:
        if not isinstance(route_hint, dict):
            return decision
        route = self._normalize_main_router_route(route_hint.get("route"))
        if route == "auto":
            return decision
        if not isinstance(decision.metadata, dict):
            decision.metadata = {}
        decision.metadata["main_router_route"] = route
        decision.metadata["main_router_reason"] = str(route_hint.get("reason") or "").strip()
        decision.metadata["main_router_confidence"] = float(route_hint.get("confidence") or 0.0)

        if route == "clarify":
            question = str(route_hint.get("clarification_question") or "").strip()
            if question:
                decision.response_text = question
            decision.action = "respond_only"
            decision.target_service_id = ""
            decision.pending_action = None
            decision.pending_data_updates = {}
            decision.missing_fields = []
            decision.blocking_fields = []
            decision.deferrable_fields = []
            return decision

        if route == "service":
            candidate = self._normalize_identifier(route_hint.get("service_id"))
            valid_ids = {self._normalize_identifier(value) for value in allowed_service_ids if self._normalize_identifier(value)}
            if candidate and candidate in valid_ids:
                decision.target_service_id = candidate
                action = str(decision.action or "").strip().lower()
                if action in {"respond_only", "collect_info"} and not bool(decision.ticket.required):
                    decision.action = "dispatch_handler"
                decision.metadata["service_resolution_source"] = "main_router"
            return decision

        if route == "book":
            action = str(decision.action or "").strip().lower()
            if action in {"dispatch_handler", "collect_info"} and not bool(decision.ticket.required):
                decision.action = "respond_only"
                decision.pending_action = None
            decision.target_service_id = ""
            decision.metadata["book_route_selected"] = list(route_hint.get("book_ids") or [])
        return decision

    async def _resolve_last_service_continuation_with_llm(
        self,
        *,
        user_message: str,
        last_assistant_message: str,
        last_service_id: str,
        selected_phase_id: str,
        selected_phase_name: str,
        services_snapshot: list[dict[str, Any]],
        context: Any = None,
    ) -> tuple[bool, str]:
        if not bool(getattr(settings, "chat_llm_service_continuity_router_enabled", False)):
            return False, ""
        if not str(settings.openai_api_key or "").strip():
            return False, ""

        normalized_last_service_id = self._normalize_identifier(last_service_id)
        if not normalized_last_service_id:
            return False, ""

        in_phase_services = [
            item
            for item in services_snapshot
            if isinstance(item, dict)
            and bool(item.get("is_active", True))
            and self._normalize_identifier(item.get("phase_id")) == selected_phase_id
        ]
        if not in_phase_services:
            return False, ""

        last_service = next(
            (
                svc
                for svc in in_phase_services
                if self._normalize_identifier(svc.get("id")) == normalized_last_service_id
            ),
            None,
        )
        if not isinstance(last_service, dict):
            return False, ""

        prompt_payload = {
            "user_message": str(user_message or "").strip(),
            "last_assistant_message": str(last_assistant_message or "").strip(),
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "last_active_service": {
                "id": self._normalize_identifier(last_service.get("id")),
                "name": str(last_service.get("name") or "").strip(),
                "description": str(last_service.get("description") or "").strip(),
                "type": str(last_service.get("type") or "").strip(),
                "cuisine": str(last_service.get("cuisine") or "").strip(),
            },
            "response_contract": {
                "continue_with_last_service": "bool",
                "reason": "short string",
                "confidence": "number 0..1",
            },
        }

        # Multi-model: routing runs on 4.1 and loads 4.1's continuity prompt.
        model = self._role_model("continuity_router")
        try:
            system_prompt = await prompt_registry.get(
                "orchestrator.continuity_router", model=model
            )
        except PromptMissingError:
            logger.exception("orchestrator_continuity_router_prompt_missing")
            return False, ""
        system_prompt = (
            str(system_prompt or "").rstrip()
            + "\n\n=== CONTINUITY DECISION RULE ===\n"
            + "Continue the SAME service only when the guest is clearly continuing that transaction right now.\n"
            + "Continuation signals include: explicit booking/order/request intent, providing slot values, choosing an option, asking to proceed, confirming, or cancelling.\n"
            + "Pure informational questions about hotel or property facts do NOT stay in the service by default.\n"
        )
        if context:
            _continuity_bk = self._build_booking_context_prompt_block(context)
            if _continuity_bk:
                system_prompt = system_prompt + "\n" + _continuity_bk

        # `model` was resolved above (self._role_model("continuity_router")).
        continuity_router_max_tokens = max(
            80,
            int(getattr(settings, "chat_llm_continuity_router_max_tokens", 220) or 220),
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
                ],
                model=model,
                temperature=0.0,
                max_tokens=continuity_router_max_tokens,
                trace_context={
                    "responder_type": "main",
                    "agent": "continuity_router",
                    "selected_phase_id": selected_phase_id,
                    "selected_phase_name": selected_phase_name,
                    "last_service_id": normalized_last_service_id,
                },
            )
        except Exception:
            return False, ""

        _log_llm_call(
            label="CONTINUITY ROUTER RESPONSE",
            session_id=str(getattr(context, "session_id", "")) if context is not None else "",
            user_message=user_message,
            system_prompt=system_prompt,
            payload=prompt_payload,
            response=raw if isinstance(raw, dict) else {},
        )
        if not isinstance(raw, dict):
            return False, ""

        continue_raw = raw.get("continue_with_last_service")
        continue_with_last_service = False
        if isinstance(continue_raw, bool):
            continue_with_last_service = continue_raw
        elif str(continue_raw or "").strip().lower() in {"true", "1", "yes", "y"}:
            continue_with_last_service = True

        reason = str(raw.get("reason") or "").strip()
        return continue_with_last_service, reason


    async def _maybe_override_main_route_with_service_continuity(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        history_bundle: dict[str, Any],
        selected_phase_id: str,
        selected_phase_name: str,
        services_snapshot: list[dict[str, Any]],
        main_router_route: str,
        main_router_hint: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any]]:
        route = self._normalize_main_router_route(main_router_route)
        hint = dict(main_router_hint or {})
        if self._coerce_bool(hint.get("service_continuity_blocked"), default=False):
            return route, hint
        if route not in {"auto", "book"}:
            return route, hint
        if str(context.pending_action or "").strip():
            return route, hint

        last_service_id = self._normalize_identifier(
            (context.pending_data or {}).get("_last_service_id")
            if isinstance(context.pending_data, dict)
            else ""
        )
        last_assistant_message = str(history_bundle.get("last_assistant_message") or "").strip()
        if not last_service_id or not last_assistant_message or not str(user_message or "").strip():
            return route, hint

        # Latency gate (NO keyword matching — uses only the router's own
        # confidence and book selection):
        # When the main router returned route=book with a real book selection
        # AND high confidence, it understood the user's message as an info
        # question. The continuity LLM in that scenario virtually always
        # agrees ("do not continue with last service"), so the call is
        # latency for no decision change. Trust the router and skip.
        # Borderline cases (low confidence, no book selection, route=auto)
        # still fall through to the LLM check.
        try:
            router_confidence = float(hint.get("confidence") or 0.0)
        except (TypeError, ValueError):
            router_confidence = 0.0
        router_book_ids = list(hint.get("book_ids") or [])
        confidence_threshold = float(
            getattr(settings, "chat_llm_continuity_skip_confidence_threshold", 0.85) or 0.85
        )
        if (
            route == "book"
            and router_book_ids
            and router_confidence >= confidence_threshold
        ):
            hint["service_continuity_skipped_reason"] = "router_confident_book_route"
            return route, hint

        continue_last_service, continuation_reason = await self._resolve_last_service_continuation_with_llm(
            user_message=user_message,
            last_assistant_message=last_assistant_message,
            last_service_id=last_service_id,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            services_snapshot=services_snapshot,
            context=context,
        )
        if not continue_last_service:
            return route, hint

        hint["route"] = "service"
        hint["service_id"] = last_service_id
        hint["confidence"] = max(float(hint.get("confidence") or 0.0), 0.84)
        hint["service_continuity_override"] = True
        if continuation_reason:
            hint["service_continuity_reason"] = continuation_reason
        return "service", hint



    def _merge_decisions(
        self,
        base: OrchestrationDecision,
        overlay: OrchestrationDecision,
    ) -> OrchestrationDecision:
        merged = base.model_copy(deep=True)

        if overlay.normalized_query:
            merged.normalized_query = overlay.normalized_query
        if overlay.intent:
            merged.intent = overlay.intent
        if overlay.confidence > 0:
            merged.confidence = max(merged.confidence, overlay.confidence)
        if overlay.action and overlay.action != "respond_only":
            merged.action = overlay.action
        if overlay.target_service_id:
            merged.target_service_id = overlay.target_service_id
        if overlay.response_text:
            merged.response_text = overlay.response_text
        if overlay.pending_action is not None:
            merged.pending_action = overlay.pending_action
        elif str(overlay.action or "").strip().lower() == "create_ticket":
            # Service agent explicitly set action=create_ticket (e.g. confirmation
            # override) and cleared pending_action to None.  The merge must honour
            # the clear; otherwise the orchestrator's stale pending_action survives
            # and the ticket-creation gate blocks with "ticket_waiting_for_user_input".
            merged.pending_action = None
        if overlay.pending_data_updates:
            merged.pending_data_updates.update(overlay.pending_data_updates)
        if overlay.missing_fields:
            merged.missing_fields = list(overlay.missing_fields)
        merged.answered_current_query = bool(merged.answered_current_query and overlay.answered_current_query)
        if overlay.blocking_fields:
            merged.blocking_fields = list(overlay.blocking_fields)
            merged.missing_fields = list(overlay.blocking_fields)
        if overlay.deferrable_fields:
            merged.deferrable_fields = list(overlay.deferrable_fields)
        if overlay.followup_question:
            merged.followup_question = overlay.followup_question
        if overlay.suggested_actions:
            merged.suggested_actions = list(overlay.suggested_actions)
        if overlay.use_handler:
            merged.use_handler = True
        if overlay.handler_intent:
            merged.handler_intent = overlay.handler_intent
        merged.interrupt_pending = bool(merged.interrupt_pending or overlay.interrupt_pending)
        merged.resume_pending = bool(merged.resume_pending or overlay.resume_pending)
        merged.cancel_pending = bool(merged.cancel_pending or overlay.cancel_pending)
        merged.requires_human_handoff = bool(merged.requires_human_handoff or overlay.requires_human_handoff)
        if overlay.selected_property_name:
            merged.selected_property_name = overlay.selected_property_name
        if overlay.selected_property_ids:
            merged.selected_property_ids = list(overlay.selected_property_ids)

        if overlay.ticket.required:
            merged.ticket.required = True
        if overlay.ticket.ready_to_create:
            merged.ticket.ready_to_create = True
        if overlay.ticket.generic_request:
            merged.ticket.generic_request = True
        if overlay.ticket.phase_applicable is not None:
            merged.ticket.phase_applicable = overlay.ticket.phase_applicable
        if overlay.ticket.approval_required:
            merged.ticket.approval_required = True
        if overlay.ticket.reason:
            merged.ticket.reason = overlay.ticket.reason
        if overlay.ticket.issue:
            merged.ticket.issue = overlay.ticket.issue
        if overlay.ticket.category:
            merged.ticket.category = overlay.ticket.category
        if overlay.ticket.sub_category:
            merged.ticket.sub_category = overlay.ticket.sub_category
        if overlay.ticket.priority:
            merged.ticket.priority = overlay.ticket.priority
        if overlay.ticket.department_id:
            merged.ticket.department_id = overlay.ticket.department_id
        if overlay.ticket.department_name:
            merged.ticket.department_name = overlay.ticket.department_name
        if overlay.ticket.evidence:
            merged.ticket.evidence = list(overlay.ticket.evidence)

        if overlay.metadata:
            merged.metadata.update(overlay.metadata)
        return merged

    def _build_service_system_prompt(
        self,
        *,
        service_id: str,
        service_name: str,
        role: str,
        behavior: str,
        description: str,
        service_knowledge: str,
        knowledge_facts: list[str],
        required_slots: list[dict[str, Any]],
        slots_are_custom: bool,
        validation_rules: list[dict[str, Any]],
        confirmation_format: dict[str, Any],
        ticketing_enabled: bool,
        ticketing_policy: dict[str, Any],
        ticketing_conditions: str,
        service_hours: dict[str, Any],
        delivery_zones: list[str],
        cuisine: str,
        confirmation_phrase: str,
        confirmation_pending_action: str,
        requires_confirmation_step: bool,
        full_kb_text: str,
        current_phase_id: str = "",
    ) -> str:
        lines: list[str] = []

        # ══ SECTION 1: BEHAVIOR (from the Description field in the UI) ════════
        # The description tells the agent exactly how to behave for this service.
        lines.append(
            f"You are the {service_name} specialist for this hotel.\n"
            f"{description}" if description else f"You are the {service_name} specialist for this hotel."
        )

        # ══ PHASE BOUNDARY RULE ════════════════════════════════════════════════
        # Injected into every service agent so it never promises unavailable services.
        lines.append(
            "\n=== SERVICE AVAILABILITY RULE ===\n"
            "Some services are only available at certain points in the guest journey.\n"
            "If this service is not available right now:\n"
            "  - Provide factual information only (what the service offers, pricing, hours, etc.).\n"
            "  - Do NOT promise, invite, offer, or imply that the guest can use or book this service right now.\n"
            "  - Do NOT begin collecting booking/order slots.\n"
            "  - Phrase unavailability naturally — NEVER mention phase names (pre_booking, pre_checkin, "
            "during_stay, post_checkout) or say 'current phase' in any response.\n"
            "  - Use natural alternatives: 'That will be available once you check in', "
            "'We can arrange this during your stay', 'This can be set up when you arrive', etc.\n"
            "  - Keep the tone warm and informative — not transactional.\n"
            "This rule applies even if the guest seems eager to book or asks you to proceed.\n"
            "ABSOLUTE RULE: Do NOT use the words pre_booking, pre_checkin, during_stay, post_checkout, "
            "'current phase', or 'this phase' anywhere in your response."
        )

        if "=== PROPERTY:" in str(service_knowledge or "") or "=== PROPERTY:" in str(full_kb_text or ""):
            lines.append(
                "\n=== MULTI-PROPERTY KNOWLEDGE RULE — HELP THE GUEST CHOOSE ===\n"
                "The knowledge base for this service contains separate property/location sections.\n"
                "\n"
                "WHEN ONE PROPERTY IS CLEAR (active scope = single id, stay-property pin, or the guest just named one):\n"
                "  - Use ONLY that property's section plus shared/common knowledge.\n"
                "  - Proceed normally — answer, collect slots, trigger the form.\n"
                "\n"
                "WHEN 2+ PROPERTIES QUALIFY AND THE GUEST HAS NOT PICKED:\n"
                "  - Do NOT pick a property silently. Do NOT mix facts across property sections.\n"
                "  - Do NOT refuse to help — surface the candidates first.\n"
                "  - Present each qualifying property in one short line, with the most distinguishing fact relevant to the guest's ask:\n"
                "      `**<Property Name>** (<Location>) — <one distinguishing detail: view, area, vibe, signature feature>`\n"
                "  - If the guest signaled a preference (couples / family / business / quiet / beach / city / budget / luxury / a specific amenity), recommend the best-fit property FIRST and briefly say WHY, then list the alternatives.\n"
                "  - End with a soft, useful question — never a blunt 'which property?':\n"
                "      'Which one sounds right?' / 'Are you leaning toward <A> or <B>?'\n"
                "\n"
                "FOR ROOM BOOKING / ORDER / RESERVATION FLOWS WHEN 2+ PROPERTIES QUALIFY:\n"
                "  - Never collect slots, never trigger the form, until the guest picks ONE property. The property is itself the first decision in the flow.\n"
                "  - NEVER offer to enumerate rooms / menu items / treatments / amenities ACROSS multiple properties. Do not say things like 'or if you want me to list available rooms from all properties' or 'I can show you all rooms across our hotels'. Cross-property dumps are overwhelming and not useful. The correct flow is: ask the guest to pick ONE property, then show only THAT property's items.\n"
                "  - If the guest seems unsure which property, briefly describe what differentiates the candidates (location, vibe, signature feature) — that helps them choose without dumping inventory.\n"
                "\n"
                "FOR SUPPORT / COMPLAINT / STAFF-CALLBACK INTAKE WHEN 2+ PROPERTIES QUALIFY:\n"
                "  - The ticket needs a single property. Ask which one the issue is at, naming the candidates. Do not open the form until the guest picks.\n"
                "  - If booking metadata already pins a stay property (during_stay / pre_checkin / post_checkout), default to that property and confirm in one short sentence rather than asking from scratch.\n"
                "\n"
                "Never mix facts from one property's section into another's answer.\n"
                "Never invent details from one property to fill another's gaps."
            )

        # ══ SUPPORT / HANDOFF TOPIC DERIVATION ═════════════════════════════════
        # When the user accepts a previous "would you like our team to reach out"
        # offer ("yes", "sure", "please connect me"), the support agent must
        # derive the request topic from the GUEST'S original execution ask, not
        # from the assistant's intermediate refusal text. Without this rule, the
        # assistant might say "Orchid Passaros offers romantic rooms..." in its
        # halls-refusal turn, and the support agent then mislabels the ticket as
        # "room booking" instead of "hall booking".
        _support_id_lc = str(service_id or "").lower().replace("-", "_")
        _support_name_lc = re.sub(r"[^a-z0-9 ]+", " ", str(service_name or "").lower())
        _support_desc_lc = str(description or "").lower()
        _support_tokens_id_name = (
            "support",
            "complaint",
            "issue",
            "feedback",
            "callback",
            "contact_staff",
            "contact staff",
            "helpdesk",
            "help_desk",
            "help desk",
            "staff_callback",
            "raise_request",
            "raise a request",
        )
        _support_desc_phrases = (
            "report an issue",
            "raise a request",
            "raise request",
            "speak to staff",
            "talk to staff",
            "talk to someone",
            "connect with staff",
            "connect to staff",
            "complaint",
            "support intake",
            "support form",
            "callback",
            "help desk",
            "helpdesk",
        )
        _is_support_service = (
            any(tok in _support_id_lc for tok in _support_tokens_id_name)
            or any(tok in _support_name_lc for tok in _support_tokens_id_name)
            or any(phrase in _support_desc_lc for phrase in _support_desc_phrases)
        )
        if _is_support_service:
            lines.append(
                "\n=== SUPPORT / HANDOFF — OPEN THE FORM, DON'T FAKE-ACK (HARD RULE, READ FIRST) ===\n"
                "You are the staff-handoff / support intake. The decision of WHETHER a request belongs here was already made upstream (the gate only routes genuine, staff-actionable handoffs to you — a guest who accepted a 'would you like our team to reach out' offer, or who is insisting after a refusal on something staff can actually help with: venue/hall booking, extra beds, group/custom arrangements, a complaint, a callback). When you ARE reached, your job is to OPEN THE FORM so a human can follow up — collect, don't chat.\n"
                "- DEFAULT = OPEN THE FORM: set action='collect_info', pending_action='collect_form_details', missing_fields=[]. Acknowledge in ONE short sentence and let the form take over.\n"
                "- Even when known_context already contains guest details, STILL open the form. Those details are only for prefill/review — they are not submission consent. Never set ticket.ready_to_create=true until the guest submits the form.\n"
                "- NEVER produce a fake text acknowledgement that promises follow-up WITHOUT opening the form. Replies like 'I have noted your request and our team will assist you shortly' / 'I've noted this and our team will get back to you' WITHOUT a form are a DEAD END — the request never reaches staff. If you're going to say the team will help, you MUST open the form in the same turn.\n"
                "- NEVER refuse or deflect based on the underlying topic's PHASE or AVAILABILITY. The routing layer already decided this explicit staff request belongs to you, so phrases like 'I can't connect you to staff for spa before check-in', 'that's only available once you're checked in', 'I can't take that request right now' are FORBIDDEN here. It does not matter that the topic (a spa, dining, etc.) is normally a during-stay service — the guest explicitly asked for a human about a hotel matter, so OPEN THE FORM and let staff handle the specifics (including telling the guest when it's available). Refusing here is wrong.\n"
                "- DERIVE the request topic from the guest's real ask (see the TOPIC DERIVATION rules below), then open the form.\n"
                "- The only times you do NOT open the form: (1) 2+ properties are in scope and none is chosen → ask which property (per the multi-property rule); (2) the topic is genuinely unknown AND the guest did NOT explicitly ask for a human — i.e. just a bare 'yes' affirmation with no prior ask anywhere in history → ask one short 'What would you like our team to help you with?'.\n"
                "- EXPLICIT STAFF REQUEST ALWAYS OPENS THE FORM (overrides exception 2 above): if the guest's message itself is an explicit ask to talk to / connect with / speak to / be put through to staff, a human, an agent, or someone — e.g. 'i wanna talk to staff', 'connect me to staff', 'i need to speak to someone', 'can a human help me', 'get me a person' — OPEN THE FORM immediately, even when no specific topic is known. Use a generic topic like 'Speak with our team'. NEVER answer an explicit staff request with 'What would you like our team to help you with?' or 'I'm here to help! If you have a specific request or question, please let me know.' — those are dead ends that strand the guest. An explicit human request is never 'ambiguous'; the guest wants a person, so open the form and let staff take it from there.\n"
            )
            lines.append(
                "\n=== SUPPORT / HANDOFF TOPIC DERIVATION (CRITICAL) ===\n"
                "This service is reached in one of two ways:\n"
                "  (a) The guest explicitly asked for help / to talk to staff / reported a complaint or blocker.\n"
                "  (b) Handoff-acceptance: the previous assistant turn offered \"would you like our team to reach out\" and the guest just said yes / sure / please connect me / go ahead / do it.\n"
                "\n"
                "WHEN REACHED VIA HANDOFF-ACCEPTANCE (case b):\n"
                "Derive the request topic / ticket subject from the GUEST'S last execution ask — NOT from the assistant's intermediate refusal text.\n"
                "  1. Walk backwards through history_last_10 to find the guest message that triggered the refusal+offer ('can I book', 'I want to host a wedding', 'arrange this', 'book the hall', 'do that').\n"
                "  2. Look at what NOUN that ask was attached to — read both that message AND the assistant turn(s) immediately before it. Examples:\n"
                "       - Prior assistant talked about 'Hornbill Banquet for weddings' → guest said 'can I book' → topic = hall / wedding venue booking. Trigger value: 'Hall booking — Hornbill Banquet, wedding'.\n"
                "       - Prior assistant talked about a group corporate package → guest said 'yes book' → topic = group / corporate booking.\n"
                "       - Prior assistant talked about a custom out-of-pattern arrangement → guest said 'yes' → topic = that custom arrangement.\n"
                "  3. The assistant's refusal message MAY contain unrelated nouns (it might have mentioned rooms while declining a hall booking). Those are SUPPORTING context, never the source of truth for what the guest wants. The guest's intent is the source of truth.\n"
                "  4. If the guest's intent is still ambiguous after walking history (e.g. they just said 'yes' with no prior execution ask in scope), ask one short clarifying question: 'What would you like our team to help you with?'. Do NOT guess.\n"
                "\n"
                "WHEN REACHED VIA DIRECT COMPLAINT / STAFF ASK (case a):\n"
                "Use the guest's current message verbatim as the topic (e.g. 'AC not working in room 304', 'website not loading', 'no callback received').\n"
                "\n"
                "Apply this BEFORE building any confirmation summary, BEFORE populating trigger_label / request_type / subject slot fields, and BEFORE opening the form. Mislabeling here causes the wrong ticket to reach staff.\n"
                "\n"
                "ALWAYS empathize briefly before collecting details when the topic is a complaint. For handoff-acceptance the empathy step is not needed — just confirm the topic and open the form / collect missing details.\n"
                "\n"
                "=== FORM-OPENING ACK — NEVER RE-OFFER HANDOFF ===\n"
                "When you generate the message that accompanies the form opening (the ack right before 'Please fill in the details below'), the form ITSELF is the handoff — you are already connecting the guest to staff via the ticket. Therefore the ack text MUST NOT contain any of the following or paraphrases of them:\n"
                "  - 'Would you like our team to reach out…'\n"
                "  - 'Should we connect you with our team…'\n"
                "  - 'Can I have someone get back to you…'\n"
                "  - 'Let me know if you'd like the team to follow up…'\n"
                "  - 'Want me to arrange…'\n"
                "  - Any other phrasing that re-offers staff follow-up.\n"
                "This is a HARD rule — even if the previous bot turn ended with such an offer (which is why the guest is here), do NOT echo it. The form is opening; the offer was already accepted; re-asking is redundant and confusing.\n"
                "Correct ack pattern: ONE clean acknowledgment sentence ONLY. Do NOT chain a second sentence with 'so our team will…', 'and our team will…', 'and they will…', 'so they can…', or any continuation clause. End the first sentence with a period and STOP — the system will append 'Please fill in the details below.' automatically. Never output orphan trailing clauses like '. so our team…' or '. and our team…'.\n"
                "ABSOLUTELY FORBIDDEN — never output any of these phrasings or close variants inside the ack, EVEN IF the previous bot turn ended with one. The form opening IS the handoff:\n"
                "  - 'Would you like our team to reach out to assist you with this?'\n"
                "  - 'Should we connect you with our team?'\n"
                "  - 'Want our team to get back to you?'\n"
                "  - 'Can I have someone follow up?'\n"
                "  - 'Let me know if you'd like the team to reach out'\n"
                "If you find yourself about to type any of those, STOP and emit only the acknowledgment sentence.\n"
                "Correct examples:\n"
                "  - 'Noted — our team will help you with the Banquet Hall booking at Fort Jadhavgadh for 200 guests.'\n"
                "  - 'Got it — I've noted your interest in hosting a wedding for 500 people at Fort Jadhavgadh.'\n"
                "  - 'Thanks for confirming — your interest in the Crystal Ballroom for a wedding has been noted.'\n"
                "  - 'Noted — your interest in connecting rooms availability at Orchid Hotel Pune has been logged for our team.'\n"
                "  - 'Got it — wedding package details for Meadows Lawns at Orchid Hotel Pune noted.'\n"
                "WRONG (each of these is a verbatim past failure — do NOT repeat any of them):\n"
                "  - 'I have noted your interest. so our team can assist you further.' ← orphan 'so' clause\n"
                "  - 'Noted, and our team will reach out to assist you.' ← chained second clause\n"
                "  - 'You have requested our team to assist with connecting rooms availability at Orchid Hotel Pune. Would you like our team to reach out to assist you with this?' ← redundant offer + double-ask\n"
                "  - 'You chose to discuss wedding packages for Meadows Lawns at Orchid Hotel Pune. Would you like our team to reach out to assist you with this?' ← same bug\n"
                "  - 'Thank you for confirming your interest in the Crystal Ballroom for your wedding. , and our team will reach out to assist you. Please fill in the details below.' ← orphan ', and' clause"
            )

        # ══ SECTION 2: TICKET CREATION (only shown when ticketing is enabled) ═
        # When ticket creation is on, the agent must collect the required info
        # and trigger a ticket. When it is off, skip this section entirely.
        if not ticketing_enabled:
            lines.append(
                "\n=== BOOKING / ORDER PROCESSING ===\n"
                "This service does NOT support booking or order processing through chat. "
                "Do NOT collect booking/order slots. Do NOT initiate or continue any transactional flow. "
                "Do NOT ask for confirmation. Provide factual information only and direct the guest to contact "
                "staff directly if they wish to proceed."
            )

        _valid_slots: list[dict] = [s for s in required_slots if isinstance(s, dict)]
        _already_collected_note = (
            "Before asking for any field, check pending_data_collected AND memory_facts. "
            "If a value is already known from either, do NOT ask for it again — use it directly."
        )
        _use_slot_list = ticketing_enabled and slots_are_custom and bool(_valid_slots)

        if ticketing_enabled:
            ticket_lines = [
                "\n=== TICKET CREATION ===",
                "\n--- SLOT COLLECTION INTENT GATE (read this before collecting anything) ---",
                "NEVER begin collecting slots (name, dates, room, phone, payment, etc.) until the guest",
                "has expressed EXPLICIT booking/order intent. Expressing a need or preference is NOT intent.",
                "",
                "NOT intent — answer the question first, then offer to proceed:",
                "  - 'I need a room with a bathtub' → describe matching room(s), then ask 'Would you like to go ahead and book it?'",
                "  - 'Do you have a room for two?' → describe options, then ask 'Shall I help you book one?'",
                "  - 'I'm looking for something quiet / with a view / comfortable' → answer, then offer",
                "  - Any question starting with 'do you have', 'what is', 'can you tell me', 'I need X', 'I'm looking for X'",
                "",
                "IS intent — only now collect slots:",
                "  - 'I'd like to book', 'Reserve it for me', 'Go ahead', 'Yes, book it'",
                "  - 'Can I make a reservation?', 'I want to order', 'Book it'",
                "  - 'Yes, please' / 'Yes, proceed' (after you explicitly offered to proceed)",
                "",
                "THE TWO-STEP RULE:",
                "  Step 1: Answer the question (which room has a bathtub? what are the hours?).",
                "  Step 2: Offer to proceed ('Would you like to go ahead and book the Prestige Suite?').",
                "  Only after the guest says YES do you ask for name, dates, and other details.",
                "--- END INTENT GATE ---",
                "",
                "--- INFORM BEFORE COLLECT ---",
                "Even after the guest has expressed clear booking intent, never ask for a slot",
                "whose answer requires knowledge the guest does not yet have.",
                "",
                "PRESENT BEFORE ASK (MANDATORY): Before asking the guest to choose or specify",
                "a menu item, treatment, room type, package, time slot, or ANY preference from a list —",
                "FIRST share all available options from the KB. The guest cannot choose from options",
                "they haven't seen. Only ask 'which one?' after you have shown the full list.",
                "  Examples:",
                "  - In-room dining: show the full menu section first, then ask which item they want",
                "  - Spa: list all treatments with duration and price, then ask which they prefer",
                "  - Restaurant: list all restaurants with timings, then ask which one",
                "  - Room booking: describe available room types, then ask which to book",
                "",
                "If this service has MULTIPLE OPTIONS the guest must choose from:",
                "  → List ALL available options from the KB first.",
                "  → Then ask which one they want.",
                "  → Never ask 'which restaurant?' or 'which room type?' if the guest hasn't seen the list.",
                "",
                "If this service has a COST or POLICY the guest is committing to",
                "(charges, cancellation terms, pricing, availability conditions):",
                "  → State the relevant cost/policy clearly BEFORE asking for their personal details.",
                "  → The guest must know what they are agreeing to before you take their name or contact.",
                "--- END INFORM BEFORE COLLECT ---",
                "",
                "--- CONFIRMATION RULE (ABSOLUTE — NO EXCEPTIONS) ---",
                "CRITICAL: NEVER ask for confirmation while ANY required field is still missing.",
                "The confirmation step ONLY happens after every single required field has a value.",
                "If fields are still missing → ask for those fields. Do NOT mix collecting and confirming.",
                "",
                "Once ALL required fields are collected, THEN:",
                "  Step 1: Show a complete bullet-point summary of every collected detail",
                "          (name, dates, room type, restaurant, time, item, price — everything).",
                "  Step 2: ASK for confirmation as a genuine question, and invite changes: e.g. "
                "'Please confirm these details are correct — shall I go ahead and send this to our team, "
                "or would you like to change or add anything?'",
                "  Step 3: Only after the guest explicitly confirms → create the ticket, and THEN "
                "tell them it has been shared with the team.",
                "WORDING — DO NOT MISLEAD: during Steps 1-2 NOTHING has been sent yet, so do NOT say the "
                "request is already 'noted', 'logged', 'raised', 'being arranged', or that the team 'will "
                "attend to this shortly' — that wrongly implies it is already done while you are still asking "
                "permission. Keep the recap clearly PENDING ('Here's what I have — shall I go ahead?'). Only "
                "AFTER the guest confirms may you say it has been sent to / will be handled by the team.",
                "Even if the guest already said 'go ahead' or 'book it', still show the full summary",
                "and ask for explicit confirmation. Never skip or abbreviate the summary.",
                "Never create a ticket without this confirmation step.",
                "--- END CONFIRMATION RULE ---",
            ]
            policy_text = str(ticketing_policy.get("policy") or "").strip()
            if policy_text:
                ticket_lines.append(f"Ticketing policy: {policy_text}")
            if ticketing_conditions:
                ticket_lines.append(
                    "Admin ticketing condition text is the primary source of truth for required details. "
                    "If slot lists conflict with that text, follow the admin ticketing condition text."
                )

            # What to collect
            if ticketing_conditions:
                ticket_lines.append(
                    f"\nWhat to collect before raising a ticket:\n\"{ticketing_conditions}\"\n"
                    "Collect every piece of information described above. "
                    "Ask for ALL missing fields in one message — never one at a time."
                )
            elif _use_slot_list and _valid_slots:
                required_s = [s for s in _valid_slots if bool(s.get("required", True))]
                optional_s = [s for s in _valid_slots if not bool(s.get("required", True))]
                if required_s:
                    ticket_lines.append("\nRequired fields (collect ALL before confirming):")
                    for s in required_s:
                        ticket_lines.append(f"  - {s.get('label') or s.get('id')} (id: {s.get('id')})")
                if optional_s:
                    ticket_lines.append("Optional fields (collect only if the guest mentions them):")
                    for s in optional_s:
                        ticket_lines.append(f"  - {s.get('label') or s.get('id')} (id: {s.get('id')})")
                ticket_lines.append(
                    "\nAsk for ALL missing required fields in a single message — never one at a time.\n"
                    "Do NOT move to confirmation until every required field has a value."
                )

            if not ticketing_conditions and not (_use_slot_list and _valid_slots):
                ticket_lines.append(
                    "\nNo structured slot schema is configured for this service.\n"
                    "Derive needed details from admin ticketing policy text, service description, and user request.\n"
                    "Ask concise clarifying questions only for missing details required for accurate execution.\n"
                    "Do not invent or enforce fixed field names."
                )
            ticket_lines.append(_already_collected_note)
            ticket_lines.append(
                "Save each collected value in pending_data_updates using the exact field id as the key.\n"
                "CRITICAL — pending_data_updates MUST always reflect EVERY field value you currently know, "
                "not just newly-collected ones this turn. If the guest confirmed a room type earlier and you "
                "are now collecting dates, pending_data_updates must still include the room type. "
                "Omitting a known value causes the system to lose track of it. "
                "Only omit a field if it is genuinely unknown or still in missing_fields."
            )

            # Confirmation step
            if requires_confirmation_step:
                confirm_template = str(confirmation_format.get("template") or "").strip()
                ticket_lines.append(
                    f"\nOnce all fields are collected, summarise everything clearly and ask the guest to confirm.\n"
                    f"The summary MUST explicitly list every collected detail present in pending_data_raw "
                    f"or known_context that is relevant to THIS service. For example:\n"
                    f"  - For room bookings: guest name, phone, email, room type, check-in/check-out dates, guest count, etc.\n"
                    f"  - For food/dining orders: ordered items with quantities, total price, delivery preferences, etc.\n"
                    f"  - For restaurant reservations: restaurant name, date, time, party size, etc.\n"
                    f"  - For event bookings: venue, event date, event type, special requests, etc.\n"
                    f"Show ONLY the fields that are actually collected and relevant — do NOT include booking/stay "
                    f"metadata (guest_id, check-in/check-out dates, stay date range, confirmation codes) "
                    f"unless this service is specifically about modifying a room booking.\n"
                    f"NEVER display any field from _internal_booking_reference to the guest.\n"
                    f"Never say 'the above details' — always spell them out.\n"
                    f"Confirmation phrase the guest must say: '{confirmation_phrase}'\n"
                    f"Set pending_action='{confirmation_pending_action}' while waiting for confirmation.\n"
                    f"When the guest confirms: set action=create_ticket, ticket.required=true, "
                    f"ticket.ready_to_create=true, pending_action=null. "
                    f"The confirmation response_text MUST again list ALL collected details relevant to this service "
                    f"from pending_data_raw — every field that was actually collected."
                )
                if confirm_template:
                    ticket_lines.append(f"Confirmation template: {confirm_template}")

            # Hard guard
            if ticketing_conditions:
                ticket_lines.append(f"\nDo NOT create a ticket until: {ticketing_conditions}")
            elif _use_slot_list:
                _req_names = [
                    s.get("label") or s.get("id")
                    for s in required_slots
                    if isinstance(s, dict) and bool(s.get("required", True))
                ]
                if _req_names:
                    ticket_lines.append(
                        f"\nDo NOT create a ticket until ALL of these are collected: {', '.join(_req_names)}."
                    )
            else:
                ticket_lines.append(
                    "\nDo NOT create a ticket until the guest has provided enough concrete details "
                    "for staff to execute the request safely."
                )
            ticket_lines.append("Set ticket.issue to a clear human-readable summary of what was requested.")
            ticket_lines.append(
                "\n=== DEPARTMENT ASSIGNMENT ===\n"
                "The payload includes available_departments — a list of {department_id, department_name} objects.\n"
                "When creating a ticket (ticket.ready_to_create=true), choose the department_id from available_departments "
                "that best matches this service.\n"
                "Examples: spa/wellness service → Wellness dept, dining/food order → F&B dept, "
                "room issue/housekeeping → Housekeeping dept, transport → Transport/Concierge dept.\n"
                "Set ticket.department_id (the numeric id) and ticket.department_name.\n"
                "If available_departments is empty or no match exists, leave both fields empty."
            )

            # Mid-flow complaint handling: if the guest reports a problem about THIS service
            # while you are already collecting their booking/order details, handle it here —
            # do NOT bounce it to the main orchestrator. You have the full context.
            ticket_lines.append(
                "\n=== MID-FLOW COMPLAINT HANDLING ===\n"
                "If the guest reports a problem, malfunction, or dissatisfaction with this service "
                "while you are mid-flow (slots are being collected): handle it directly — BUT only if "
                "the complaint is contextually plausible given where the guest is in their journey.\n"
                "Examples of implausible complaints: reporting a cockroach or broken AC before checking in, "
                "reporting a damaged room while in pre-booking phase, complaining about a meal not yet ordered.\n"
                "If the complaint is plausible: set action=create_ticket, ticket.category='complaint', "
                "ticket.ready_to_create=true, ticket.issue=a clear summary of the problem. "
                "Clear pending_action. Do NOT continue the booking/order flow after a valid complaint is raised.\n"
                "If the complaint is NOT plausible in the current context: respond with empathy, "
                "acknowledge the concern, but do NOT create a ticket. Offer to assist or connect with staff."
            )
            lines.extend(ticket_lines)

        # ══ SECTION 3: KNOWLEDGE BASE ══════════════════════════════════════════
        # This is the complete knowledge for this service, exactly as entered in the system.
        # The agent must answer all questions from this knowledge and nothing else.
        kb_sections: list[str] = []
        if str(service_knowledge or "").strip():
            kb_sections.append(str(service_knowledge or "").strip())
        if str(full_kb_text or "").strip():
            kb_sections.append("=== PROPERTY-SCOPED HOTEL KNOWLEDGE ===\n" + str(full_kb_text or "").strip())
        kb_content = "\n\n".join(section for section in kb_sections if section).strip()
        if kb_content:
            lines.append(
                f"\n=== {service_name.upper()} — KNOWLEDGE BASE ===\n"
                f"{kb_content}\n"
                f"=== END OF KNOWLEDGE BASE ==="
            )
        elif description:
            lines.append(f"\nService description (use as knowledge): {description}")

        lines.append(
            "\nRULE: Answer ONLY from the knowledge base above. "
            "If something the guest asks is not covered there, say you do not have that detail "
            "and never guess, invent, or assume anything. "
            "Offer to connect them with staff only when the guest explicitly wants human help "
            "or the request is a genuine manual hotel/staff task."
        )
        lines.append(
            "\n=== KNOWLEDGE PRECISION RULE ===\n"
            "- Never generalize one fact into a different or stronger claim.\n"
            "- A room amenity, service mention, or nearby-landmark mention does NOT prove a different facility, menu, package, policy, or availability unless the KB explicitly says so.\n"
            "- Comparative asks such as cheapest, most affordable, best value, best, largest, smallest, closest, or similar need an explicit KB basis. If price/size/distance/rating data is missing, say that basis is not available instead of naming a winner.\n"
            "- When the guest explicitly asks for room size, area, or dimensions, include the exact size values from the KB if present.\n"
            "- If the guest asks for one exact detail and the KB does not contain it, say that exact detail is not available. Do NOT replace it with nearby facts unless the guest also asked for those.\n"
            "- If the guest is trying to execute an unsupported service through the bot right now, say clearly that you cannot do that through the bot right now. Do NOT answer as if the task can be performed."
        )
        lines.append(
            "\n=== COMPLETENESS RULE ===\n"
            "When the guest asks about rooms, options, packages, amenities, services, or any list of offerings:\n"
            "- List ALL items from the knowledge base — never summarize, skip, or show only a partial list.\n"
            "- If the KB lists 5 room types, show all 5. If it lists 8 amenities, show all 8.\n"
            "- Include ALL details for each item (price, description, features, capacity, etc.) as given in the KB.\n"
            "- Do NOT say 'and more' or 'among others' — be exhaustive.\n"
            "- The guest deserves complete information to make a decision.\n"
            "ROOM PRESENTATION STYLE: For room options, lead with what makes each room uniquely appealing to guests "
            "(experience, view, inclusions, standout features). Do not lead with square-foot/area specs, "
            "and mention size only if the guest explicitly asks for size/area."
        )

        # ── Operating constraints (hours, zones, cuisine) ─────────────────────
        constraints: list[str] = []
        if service_hours.get("open") or service_hours.get("close"):
            open_t = str(service_hours.get("open") or "").strip()
            close_t = str(service_hours.get("close") or "").strip()
            hour_str = " – ".join(filter(None, [open_t, close_t]))
            constraints.append(f"Operating hours: {hour_str}. Inform guest if their request is outside these hours.")
        if delivery_zones:
            constraints.append(f"Delivery zones: {', '.join(delivery_zones)}.")
        if cuisine:
            constraints.append(f"Cuisine type: {cuisine}.")
        if constraints:
            lines.append("\n--- CONSTRAINTS ---\n" + "\n".join(constraints))

        lines.append(
            "\n=== RESPONSE STYLE ===\n"
            "Use plain chat text for guest-facing responses. "
            "Do NOT use markdown markers such as **bold** or *italic*.\n"
            "When asking for multiple missing details, always use bullet points with '-' items.\n"
            "Example:\n"
            "Please share the following details:\n"
            "- Room number\n"
            "- Check-in date\n"
            "- Check-out date\n"
            "\n=== TONE & LANGUAGE RULES ===\n"
            "You are speaking to a hotel guest. Use warm, natural, human language at all times.\n"
            "NEVER use these words or phrases in guest-facing responses:\n"
            "  - 'escalate', 'ticket', 'reference number', 'ticket ID', 'logged your complaint'\n"
            "  - 'create a ticket', 'automated', 'system issue', 'ticketing'\n"
            "INSTEAD use natural alternatives:\n"
            "  - 'I have noted this down' instead of 'I have created a ticket'\n"
            "  - 'Our team will look into this' instead of 'This has been escalated'\n"
            "  - 'I will make sure someone helps you' instead of 'I will escalate this'\n"
            "  - 'Let me connect you with our team' instead of 'Let me transfer you'\n"
            "For complaints: ALWAYS empathize first ('I am sorry to hear that', "
            "'That is not the experience we want for our guests'), then offer help.\n"
            "Never jump straight to asking for details after a complaint — acknowledge the feeling first."
        )

        # ── Scope, handoff, and known context ────────────────────────────────
        lines.append(
            f"\nYou only handle {service_name} topics. "
            "When a guest asks a question, first decide: is this question about THIS service's domain?\n"
            "  - If YES (topic-adjacent — e.g. room types/sizes/amenities/pricing/policies during a room booking flow): "
            "answer from your knowledge base. If the specific detail is not in your KB, say it is not available in the current system and offer to connect them with staff. "
            "Keep the pending booking/order flow alive — do NOT set context_switched.\n"
            "  - The payload may include property_scope with the current active properties plus detected_properties/available_locations for the hotel.\n"
            "  - If the guest explicitly switches to a different property or location mid-flow, update selected_property_ids and selected_property_name accordingly and continue THIS same service using that property's KB in the same turn.\n"
            "  - If the guest mentions a location that has multiple properties, selected_property_ids must include ALL properties from that location.\n"
            "  - When the property changes, do NOT keep old property-specific selections (like a previously chosen room) unless the guest repeats them.\n"
            "  - If NO (completely unrelated to this service — e.g. asking about the pool, spa, dining, gym, parking, events, or any other hotel facility while mid-booking): "
            "do NOT attempt to answer. Set relevant_to_service=false AND context_switched=true in metadata and use a short handoff line like 'Let me get that answered for you.' "
            "The main hotel assistant has the full hotel knowledge base and will answer it. "
            "The suspended booking flow will be offered for resume after the main assistant answers.\n"
            "Rule of thumb: if the question is about THIS service's subject matter → stay and answer or say unavailable (relevant_to_service=true). "
            "If the question is about a completely different part of the hotel → delegate (relevant_to_service=false, context_switched=true).\n"
            "\n=== KNOWN CONTEXT & GUEST FACTS (read this before asking for ANYTHING) ===\n"
            "The payload includes known_context with guest data the hotel already has on file "
            "(e.g. guest_name, room_number, phone, email).\n"
            "\n"
            "The payload also includes _internal_booking_reference with booking/stay metadata "
            "(guest_id, check-in/check-out dates, stay date range, confirmation codes). "
            "This is for YOUR internal reference only — use it for date-based logic if needed, "
            "but NEVER display these fields to the guest in any response or confirmation summary.\n"
            "\n"
            "ABSOLUTE RULE: NEVER ask for any information that is already present in known_context or pending_data_collected. "
            "Use the value directly — do not ask the guest to repeat or re-confirm it.\n"
            "\n"
            "HOTEL CHECK-IN REALITY — think like a hotel:\n"
            "  When a guest checks in, the hotel collects: full name, room number, phone, email.\n"
            "  These are on file for the entire stay and after checkout.\n"
            "  A guest who is already in the hotel should NEVER be asked for their name, room number, phone, or email.\n"
            "\n"
            "PHASE-SPECIFIC RULES (current_phase_id is in the payload):\n"
            f"  {'during_stay / post_checkout' if current_phase_id in ('during_stay', 'post_checkout') else current_phase_id or 'during_stay / post_checkout'} phase rules:\n"
            + (
                "  -> Guest is checked in (or has checked out). Hotel has name, room number, phone, email on file.\n"
                "  -> NEVER ask for guest_name, room_number, phone, or email — treat them as known even if not yet in known_context.\n"
                "  -> Only collect service-specific details: date, time, preferences, item choices, special requests.\n"
                if current_phase_id in ("during_stay", "post_checkout")
                else
                "  pre_checkin phase rules:\n"
                "  -> Guest has a confirmed reservation. Hotel has name, phone, email from booking.\n"
                "  -> NEVER ask for guest_name, phone, or email if they are in known_context.\n"
                "  -> Room number may not be assigned yet — only ask if the service genuinely needs it and it is absent from known_context.\n"
                if current_phase_id == "pre_checkin"
                else
                "  pre_booking phase rules:\n"
                "  -> Guest may not have a reservation yet. Name, phone, email may be unknown.\n"
                "  -> Only collect contact info (name, phone, email) if the service genuinely needs it AND it is NOT in known_context.\n"
                "  -> Never ask speculatively — only request what is actually required to fulfil the specific request.\n"
            )
            + "\n"
            "UNIVERSAL RULE (all phases): If a field is present in known_context — regardless of phase — NEVER ask for it.\n"
            "\nAlways read history_last_10 first, then full_history_context (including older_context_summary) before responding. "
            "Use last_user_message and last_assistant_message as the strongest anchors for short replies.\n"
            "Resolve short replies ('yes', a date, a number) against what was last asked.\n"
            "Capture any NEW useful guest details (preferences, dietary needs, special occasion) "
            "in new_facts_to_remember as snake_case key-value pairs. Do NOT re-store fields already in known_context.\n"
            "Return STRICT JSON only matching the response schema."
        )

        # ── DATE VALIDATION ───────────────────────────────────────────────────
        lines.append(
            "\n=== DATE & INPUT VALIDATION ===\n"
            "The payload includes `current_date` (today's date) and `current_day` (day of week). "
            "Use these to validate any dates the guest provides:\n"
            "  - If a check-in or check-out date is in the past (before current_date), do NOT store it in "
            "pending_data_updates. Keep those slots in missing_fields and ask for a valid future date.\n"
            "  - If pending_data_raw already contains a past date for check_in or check_out, treat those "
            "slots as still-empty and prompt the guest to correct them — do NOT re-use those values.\n"
            "  - When the guest provides a new date after a previous one was rejected, accept the new date "
            "as the correction. Do NOT compare it against the old rejected date. Simply validate it against "
            "current_date and, if valid, store it in pending_data_updates.\n"
            "  - Be tolerant of minor typos in month names (e.g. 'match' → 'march', 'jaunary' → 'january'). "
            "If the intent is clear, parse the date rather than asking for clarification.\n"
            "  - If check-out is on the same day or before check-in, flag it and ask for correction.\n"
            "  - If the guest says 'tomorrow', 'next Friday', 'next week', etc., resolve against current_date "
            "and store the resolved YYYY-MM-DD value.\n"
            "STAY-DATE BOUNDARY VALIDATION (pre_checkin / during_stay / post_checkout):\n"
            "  The payload includes the guest's booking dates in known_context or memory_facts "
            "(check_in / check_out or stay_checkin_date / stay_checkout_date).\n"
            "  - pre_checkin & during_stay: Any service date (event date, spa appointment, restaurant reservation, "
            "cab pickup, etc.) MUST fall within the guest's check-in and check-out dates (inclusive).\n"
            "    If the guest provides a date outside their stay, do NOT store it. Politely inform them: "
            "'Your stay is from [check-in] to [check-out] — could you pick a date within that period?'\n"
            "  - post_checkout: Service dates (e.g. lost & found pickup) must be on or after the check-out date.\n"
            "  - If booking dates are not available in the payload, skip this validation.\n"
            "PHONE NUMBER VALIDATION:\n"
            "  - A valid phone number must contain exactly 10 digits (digits only, ignoring spaces/dashes/brackets).\n"
            "  - Reject obvious fake or garbage numbers (e.g., all identical digits like 9999999999, sequential digits like 1234567890, or common test numbers).\n"
            "  - If the guest provides an invalid or garbage phone number, do NOT store it in pending_data_updates. "
            "Keep the phone field in missing_fields and politely ask the guest to provide a real, valid 10-digit phone number.\n"
            "  - If pending_data_raw already contains an invalid phone number, treat that slot as still-empty."
        )

        # ── RESPONSE SCHEMA ───────────────────────────────────────────────────
        lines.append(
            "\nResponse schema (return this exact structure):\n"
            "{\n"
            '  "action": "respond_only|collect_info|create_ticket|cancel_pending",\n'
            '  "response_text": "your reply to the guest",\n'
            '  "selected_property_ids": ["set to the ONE property id (from property_scope.active_property_ids) once the guest has narrowed to a single property; otherwise omit or leave empty"],\n'
            '  "relevant_to_service": true,\n'
            '  "pending_action": "short_descriptor_or_null",\n'
            '  "pending_data_updates": {"slot_id": "value"},\n'
            '  "missing_fields": ["slot_id"],\n'
            '  "suggested_actions": ["short chip label"],\n'
            '  "requires_human_handoff": false,\n'
            '  "selected_property_name": "property name in focus for this turn, or empty if unchanged/general",\n'
            '  "selected_property_ids": ["property id(s) in focus for this turn"],\n'
            '  "new_facts_to_remember": {"guest_name": "value", "room_number": "value"},\n'
            '  "ticket": {\n'
            '    "required": false,\n'
            '    "ready_to_create": false,\n'
            '    "issue": "human readable description",\n'
            '    "category": "",\n'
            '    "sub_category": "",\n'
            '    "priority": "low|medium|high|critical",\n'
            '    "department_id": "pick the numeric id from available_departments that best fits this service",\n'
            '    "department_name": "the matching department name"\n'
            '  },\n'
            '  "metadata": {\n'
            '    "context_switched": false,\n'
            '    "faq_resolution": [{"faq_id":"", "relation":"override|merge|ignore", "applied":false, "confidence":0.0, "reason":"", "fact":""}],\n'
            '    "resolved_facts": [{"source":"faq:<id>|kb", "fact":"short fact used in response"}]\n'
            "  }\n"
            "}"
        )

        # ── FORM-FLOW ACTION RULE ────────────────────────────────────────────
        lines.append(
            "\n=== FORM-FLOW ACTION RULE ===\n"
            "CRITICAL: When the guest has confirmed their choice and you are ready for them to fill in "
            "booking/order details, you MUST set action='collect_info' and state='awaiting_info'. "
            "List all uncollected required slots in missing_fields.\n"
            "Do NOT use action='respond_only' if your response text directs the guest to fill in a form "
            "or provide their details. The form will only appear when action='collect_info'.\n"
            "If you say 'please fill in the details below' but set action='respond_only', the form will NOT "
            "appear and the guest will be confused."
        )

        # ── PENDING DATA COMPLETENESS RULE ───────────────────────────────────
        lines.append(
            "\n=== PENDING DATA COMPLETENESS RULE ===\n"
            "pending_data_updates MUST always include EVERY field value you currently know — "
            "not just values collected this turn. If the guest chose a room type earlier and you "
            "are now asking for dates, pending_data_updates must still contain the room type. "
            "Omitting a known value causes the system to lose track of it. "
            "Only omit a field if it is genuinely unknown or listed in missing_fields."
        )

        return "\n".join(lines)

    # ---- menu serving times -------------------------------------------------
    # A dish that is only cooked between 7pm and 11pm cannot be ordered at 5:36pm,
    # but the menu states that as prose sitting above a section ("All Tandoori
    # Kebabs will be available from 12 noon to 3 pm and 7 pm to 11 pm") while the
    # item itself lists only Type / Price / Calories / Serving Size. The model was
    # therefore doing two jobs at once: working out which note governs a dish, and
    # comparing two windows against the current clock. The clock half is arithmetic
    # and is done here, exactly, once per turn; the reading half stays with the
    # model, which has the note's own wording and the menu structure in front of it.
    #
    # Only notes INSIDE the in-room dining menu count. The hotel section above it
    # carries the all-day restaurant's meal periods ("Overall operating hours:
    # 06:30 - 22:30, kitchen closed between meal periods"); reading those as menu
    # timings would close in-room dining every afternoon.
    _IRD_MENU_MARKER = "IN ROOM DINING MENU"
    _IRD_TIME_RANGE_RE = re.compile(
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|noon|midnight)?"
        r"\s*(?:[-‐-―]|to|till|until)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|noon|midnight)?",
        re.IGNORECASE,
    )

    @staticmethod
    def _ird_parse_clock(hour: str, minute: str, mer: str, *, pm_hint: bool) -> int:
        """One clock reading -> minutes since midnight. `pm_hint` carries the meridiem
        from the other end of the range, so "7 pm to 11 pm" and "12 noon to 3 pm"
        both land correctly when only one end spells it out."""
        h = int(hour)
        m = int(minute or 0)
        mer = (mer or "").lower()
        if mer == "noon":
            return 12 * 60 + m
        if mer == "midnight":
            return m
        if mer == "pm":
            return ((h % 12) + 12) * 60 + m
        if mer == "am":
            return (h % 12) * 60 + m
        # No meridiem on this end. A bare hour of 13+ is already 24-hour.
        if h > 12:
            return h * 60 + m
        if pm_hint and h != 12:
            return ((h % 12) + 12) * 60 + m
        return h * 60 + m

    @classmethod
    def _ird_parse_windows(cls, line: str) -> list[tuple[int, int]]:
        """Every serving window stated on one line, as (start, end) minutes."""
        windows: list[tuple[int, int]] = []
        for mt in cls._IRD_TIME_RANGE_RE.finditer(line or ""):
            h1, m1, mer1, h2, m2, mer2 = mt.groups()
            end_pm = (mer2 or "").lower() in {"pm", "midnight"}
            start = cls._ird_parse_clock(h1, m1, mer1, pm_hint=end_pm)
            end = cls._ird_parse_clock(h2, m2, mer2, pm_hint=False)
            # "12 noon to 3 pm": the end reads 3am unless the start pulls it forward.
            if end < start and not (mer2 or ""):
                end = cls._ird_parse_clock(h2, m2, "pm", pm_hint=False)
            if 0 <= start < 24 * 60 and 0 < end <= 24 * 60 and end > start:
                windows.append((start, end))
        return windows

    @staticmethod
    def _ird_fmt_clock(minutes: int) -> str:
        minutes %= 24 * 60
        h, m = divmod(minutes, 60)
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"

    @classmethod
    def _build_menu_serving_windows(cls, menu_text: str, now_local: datetime) -> list[dict[str, Any]]:
        """Every serving-time note in the in-room dining menu, with the open/closed
        question already answered for the guest's current local time.

        Each entry keeps the note's own wording, so the model resolves which dishes
        a note covers from the menu's language ("All Tandoori Kebabs") rather than
        from a dish list invented here. A section with no stated timing produces no
        entry at all, which is what makes it always available.
        """
        text = str(menu_text or "")
        if not text.strip():
            return []
        marker = text.find(cls._IRD_MENU_MARKER)
        body = text[marker:] if marker >= 0 else text

        now_minutes = now_local.hour * 60 + now_local.minute
        seen: set[tuple[str, tuple[tuple[int, int], ...]]] = set()
        out: list[dict[str, Any]] = []
        section = ""

        for raw in body.split("\n"):
            line = raw.strip()
            if not line:
                continue
            # Section headings are the menu's ALL-CAPS lines.
            if line == line.upper() and any(ch.isalpha() for ch in line) and not any(ch.isdigit() for ch in line):
                section = line
                continue
            low = line.lower()
            if not ("timing" in low or "available" in low):
                continue
            windows = cls._ird_parse_windows(line)
            if not windows:
                continue
            key = (section, tuple(windows))
            if key in seen:
                continue  # the same note repeats under a repeated page heading
            seen.add(key)

            open_now = any(s <= now_minutes < e for s, e in windows)
            next_open = ""
            if not open_now:
                later = [s for s, _ in windows if s > now_minutes]
                if later:
                    next_open = f"today at {cls._ird_fmt_clock(min(later))}"
                elif windows:
                    next_open = f"tomorrow at {cls._ird_fmt_clock(min(s for s, _ in windows))}"
            out.append({
                "menu_section": section,
                "note": line,
                "served": " and ".join(
                    f"{cls._ird_fmt_clock(s)}-{cls._ird_fmt_clock(e)}" for s, e in windows
                ),
                "open_now": open_now,
                "next_opens": next_open,
            })
        return out

    @staticmethod
    def _build_service_runtime_json_contract(
        *,
        service_id: str = "",
        food_only_service: bool = False,
        confirmation_pending_action: str,
        current_phase_id: str = "",
        ticketing_mode: str = "",
        trigger_field_id: str = "",
        trigger_field_label: str = "",
        form_field_names: list[str] | None = None,
        service_type: str = "",
        service_cuisine: str = "",
        service_description: str = "",
        include_faq_reporting: bool = True,
    ) -> str:
        def _norm(value: Any) -> str:
            return str(value or "").strip().lower().replace(" ", "_")

        confirm_action = str(confirmation_pending_action or "confirm_booking").strip() or "confirm_booking"
        _phase = str(current_phase_id or "").strip()
        _tm = str(ticketing_mode or "").strip().lower()
        _is_form_mode = _tm == "form"
        if _phase in ("during_stay", "post_checkout"):
            _guest_facts_rule = (
                "=== GUEST FACTS RULE (MANDATORY) ===\n"
                "The guest is checked in (or has checked out). The hotel already has their name, room number, phone, and email.\n"
                "NEVER ask for guest_name, room_number, phone, or email — these are on file.\n"
                "Only collect service-specific details: date, time, item/treatment choice, special requests.\n"
                "ALWAYS check known_context FIRST before asking for any field — if it is there, use it directly.\n\n"
            )
        elif _phase == "pre_checkin":
            _guest_facts_rule = (
                "=== GUEST FACTS RULE (MANDATORY) ===\n"
                "The guest has a confirmed reservation. The hotel has their name, phone, and email from the booking.\n"
                "NEVER ask for guest_name, phone, or email if they are in known_context.\n"
                "Room number may not be assigned yet — only ask if genuinely needed and absent from known_context.\n"
                "ALWAYS check known_context FIRST before asking for any field.\n\n"
            )
        else:
            _guest_facts_rule = (
                "=== GUEST FACTS RULE (MANDATORY) ===\n"
                "ALWAYS check known_context FIRST before asking for any field.\n"
                "If guest_name, room_number, phone, or email are in known_context — use them directly, NEVER ask again.\n"
                "Only collect contact info (name, phone, email) when genuinely required and NOT already known.\n\n"
            )

        _validation_rules = (
            "=== DATE & INPUT VALIDATION (MANDATORY) ===\n"
            "The payload includes `current_date` (today's date) and `current_day` (day of week). "
            "Use these to validate any dates the guest provides:\n"
            "  - If a check-in or check-out date is in the past (before current_date), do NOT store it in "
            "pending_data_updates. Keep those slots in missing_fields and ask for a valid future date.\n"
            "  - If pending_data_raw already contains a past date for check_in or check_out, treat those "
            "slots as still-empty and prompt the guest to correct them — do NOT re-use those values.\n"
            "  - When the guest provides a new date after a previous one was rejected, accept the new date "
            "as the correction. Do NOT compare it against the old rejected date. Simply validate it against "
            "current_date and, if valid, store it in pending_data_updates.\n"
            "  - Be tolerant of minor typos in month names (e.g. 'match' → 'march', 'jaunary' → 'january'). "
            "If the intent is clear, parse the date rather than asking for clarification.\n"
            "  - If check-out is on the same day or before check-in, flag it and ask for correction.\n"
            "  - If the guest says 'tomorrow', 'next Friday', 'next week', etc., resolve against current_date "
            "and store the resolved YYYY-MM-DD value.\n"
            "STAY-DATE BOUNDARY VALIDATION (pre_checkin / during_stay / post_checkout):\n"
            "  The payload includes the guest's booking dates in known_context or memory_facts "
            "(check_in / check_out or stay_checkin_date / stay_checkout_date).\n"
            "  - pre_checkin & during_stay: Any service date (event date, spa appointment, restaurant reservation, "
            "cab pickup, etc.) MUST fall within the guest's check-in and check-out dates (inclusive).\n"
            "    If the guest provides a date outside their stay, do NOT store it. Politely inform them: "
            "'Your stay is from [check-in] to [check-out] — could you pick a date within that period?'\n"
            "  - post_checkout: Service dates (e.g. lost & found pickup) must be on or after the check-out date.\n"
            "  - If booking dates are not available in the payload, skip this validation.\n"
            "PHONE NUMBER VALIDATION:\n"
            "  - A valid phone number must contain exactly 10 digits (digits only, ignoring spaces/dashes/brackets).\n"
            "  - Reject obvious fake or garbage numbers (e.g., all identical digits like 9999999999, sequential digits like 1234567890, or common test numbers).\n"
            "  - If the guest provides an invalid or garbage phone number, do NOT store it in pending_data_updates. "
            "Keep the phone field in missing_fields and politely ask the guest to provide a real, valid 10-digit phone number.\n"
            "  - If pending_data_raw already contains an invalid phone number, treat that slot as still-empty.\n\n"
        )

        _json_shape = (
            "=== RUNTIME OUTPUT CONTRACT (MANDATORY) ===\n"
            "Return STRICT JSON only.\n"
            "Use exactly this JSON object shape:\n"
            "{\n"
            '  "action": "respond_only|collect_info|create_ticket|cancel_pending",\n'
            '  "response_text": "your reply to the guest",\n'
            '  "selected_property_ids": ["set to the ONE property id (from property_scope.active_property_ids) once the guest has narrowed to a single property; otherwise omit or leave empty"],\n'
            '  "relevant_to_service": true,\n'
            '  "pending_action": "short_descriptor_or_null",\n'
            '  "pending_data_updates": {"slot_id": "value"},\n'
            '  "missing_fields": ["slot_id"],\n'
            '  "suggested_actions": ["short chip label"],\n'
            '  "requires_human_handoff": false,\n'
            '  "new_facts_to_remember": {"guest_name": "value", "room_number": "value"},\n'
            '  "ticket": {\n'
            '    "required": false,\n'
            '    "ready_to_create": false,\n'
            '    "issue": "human readable description",\n'
            '    "category": "",\n'
            '    "sub_category": "",\n'
            '    "priority": "low|medium|high|critical",\n'
            '    "department_id": "numeric id from available_departments that best fits this service",\n'
            '    "department_name": "the matching department name"\n'
            "  },\n"
            '  "metadata": {\n'
            '    "context_switched": false' + (
                ',\n'
                '    "faq_resolution": [{"faq_id":"", "relation":"override|merge|ignore", "applied":false, "confidence":0.0, "reason":"", "fact":""}],\n'
                '    "resolved_facts": [{"source":"faq:<id>|kb", "fact":"short fact used in response"}]\n'
                if include_faq_reporting else "\n"
            ) +
            "  }\n"
            "}\n"
            "relevant_to_service: set to false ONLY when the guest's message is entirely outside "
            "this service's domain AND you cannot make meaningful progress. When false, also set "
            "metadata.context_switched=true so the main orchestrator re-routes the query. "
            "Default is true — most messages, even adjacent questions, should be handled here.\n"
            # 96% of real service turns need no ticket, yet the model spelled out all
            # eight empty ticket fields every time (~157 chars of pure generation cost
            # per turn). Every field on TicketDecision has a default, so the collapsed
            # form parses to exactly the same object.
            'ticket: when no ticket is needed, emit ONLY {"required": false} — do not spell out '
            "the other ticket fields. Fill them in only when required is true.\n"
        )

        # ── Ticketing flow rules: FORM-based vs TEXT-based ──
        if _is_form_mode:
            _trigger_id = str(trigger_field_id or "").strip()
            _trigger_label = str(trigger_field_label or _trigger_id).strip()
            _form_fields_hint = ""
            if form_field_names:
                _form_fields_hint = (
                    f"The form will collect these fields automatically: {', '.join(form_field_names)}. "
                    "Do NOT ask for any of these in conversation — the form handles them.\n"
                )
            _ticketing_rules = (
                "=== FORM-BASED TICKETING RULES (MANDATORY — THIS SERVICE USES A FORM) ===\n"
                "The payload also includes `service_form_trigger_field`, `service_form_fields`, and "
                "`service_form_instructions`. Use those exact definitions as the source of truth for the form.\n"
                "This service uses an INLINE FORM for collecting guest details. The form appears automatically "
                "in the UI once you signal readiness. Your job is to guide the guest to choose their option, "
                "confirm the choice, and then let the form take over. NEVER ask the guest to type 'yes confirm' "
                "or any confirmation phrase — the form submission IS the confirmation.\n\n"
                "HOW THE FORM TRIGGER WORKS (understand this to set the right state):\n"
                "The system shows the form when ALL of these are true:\n"
                "  1. action = 'collect_info'\n"
                f"  2. pending_data_updates contains the trigger field '{_trigger_id}' with the guest's chosen value\n"
                "  3. missing_fields does NOT contain the trigger field\n"
                "If any condition is missing, the form will NOT appear and the guest sees a dead end.\n\n"
                "STEP-BY-STEP FLOW:\n"
                f"  Step 1 - PRESENT OPTIONS: When the guest asks about this service, show all available "
                f"options from the KB. Let them browse and choose.\n"
                f"  Step 2 - CLARIFY/INFORM: If the guest is exploratory or preference-only "
                f"(examples: 'I want a room with bathtub', 'which room has a bathtub?', 'what are options?'), "
                f"answer with options and clarifying details first. Do NOT trigger the form yet.\n"
                f"  Step 3 - CONFIRM CHOICE + INTENT: Trigger only after BOTH are true:\n"
                f"    - you know the specific trigger option value (a NAMED room/treatment/option, not a generic 'yes')\n"
                f"    - the guest explicitly wants to proceed now\n"
                f"  In that response, confirm the choice warmly and end with "
                f"'Please fill in the booking details below.'\n"
                f"\n"
                f"  GENERIC-AFFIRMATION GUARD (HARD RULE — read before triggering):\n"
                f"  If the guest says 'yes', 'yes proceed', 'ok', 'sure', 'go ahead', 'do it', 'book it', 'proceed' "
                f"WITHOUT naming a specific option (e.g. a specific room type), this is INTENT only, NOT a choice. "
                f"You DO NOT have a value for the trigger field '{_trigger_id}' yet. In this case:\n"
                f"    - set action = 'respond_only' (NOT collect_info)\n"
                f"    - do NOT set pending_action = 'collect_form_details'\n"
                f"    - do NOT include '{_trigger_id}' in pending_data_updates\n"
                f"    - DO ask: 'Which one would you like to go with — <list the options by name>?'\n"
                f"    - the guest must name the specific option BEFORE the form can open\n"
                f"  WRONG (do NOT do this):\n"
                f"    Guest: 'yes proceed' (no room type named yet) → triggering the form. The form opens with no room selected and the guest is stuck.\n"
                f"  RIGHT:\n"
                f"    Guest: 'yes proceed' → action=respond_only, reply 'Which room would you like — Deluxe, Premier, or Executive?' Wait for the specific name.\n"
                f"    Guest then says: 'Premier' → NOW trigger the form (Step 4) with trigger field = 'Premier Room'.\n"
                f"  Step 4 - TRIGGER THE FORM: In the same response where you confirm choice + intent, set:\n"
                f"    - action = 'collect_info'\n"
                f"    - pending_action = 'collect_form_details'\n"
                f"    - pending_data_updates must include '{_trigger_id}' with the guest's chosen value\n"
                f"    - missing_fields = [] (empty - the form handles remaining fields)\n"
                f"  Step 5 - AFTER FORM SUBMISSION: The system creates the ticket automatically from the form "
                f"data. You do not need to create the ticket yourself.\n\n"
                f"{_form_fields_hint}"
                "CRITICAL DO-NOTs FOR FORM MODE:\n"
                "  - NEVER set pending_action to 'confirm_booking' or any confirm_* value\n"
                "  - NEVER ask the guest to say 'yes confirm', 'confirm', or any confirmation phrase\n"
                "  - NEVER ask for form field values (dates, name, phone, etc.) in conversation\n"
                "  - NEVER set action to 'create_ticket' — the form submission handles ticket creation\n"
                "  - NEVER hold back the form waiting for more conversational input after the trigger choice is made\n\n"
                "SMART STATE HANDLING:\n"
                "  - Guest is browsing / asking questions about options → action='respond_only', answer from KB\n"
                "  - Guest selects a SPECIFIC option (names it, points to it like 'the first one', "
                "'Terminal 1', 'the prestige suite', 'candlelight therapy') → confirm + trigger form (Step 3)\n"
                "  - Guest changes mind about their choice → acknowledge, present options again, "
                "do NOT trigger the form yet\n"
                "  - Guest asks an unrelated question mid-flow → answer if within your domain, "
                "or set context_switched=true\n"
                "  - Guest provides a trigger-like value but message is informational/exploratory - "
                "answer first, do NOT trigger yet\n"
                "  CRITICAL — INTENT vs SELECTION (use conversation context to decide):\n"
                f"  The trigger field is '{_trigger_id}'. You can ONLY trigger the form when you know the "
                "SPECIFIC value for this field. Look at the conversation history to decide:\n"
                "    - If your last message asked 'Would you like to book?' or offered to help, and the "
                "guest says 'yes' → that is booking INTENT only. You still need to ask which specific "
                f"'{_trigger_id}' they want. Set the trigger value to the ACTUAL option, not 'yes'.\n"
                "    - If your last message asked the guest to choose between specific options (e.g. "
                f"'Which {_trigger_id}?') or confirmed a specific option (e.g. 'Shall I go ahead with "
                "Terminal 1?'), and the guest says 'yes' → that IS a selection. Set the trigger value "
                "to the specific option from your last message (e.g. 'Terminal 1'), NOT 'yes'.\n"
                "    - A room/treatment/table discussed earlier in the conversation is NOT a current selection "
                "by itself. Only use it when your immediately previous assistant message narrowed the choice to "
                "that exact single option and the guest then affirmed it.\n"
                "    - If your immediately previous assistant message was a generic booking offer or an adjacent "
                "hotel-info answer ('Would you like help booking now?'), a bare 'yes' means resume the booking "
                "flow and ask for the specific trigger option. Do NOT assume an earlier discussed option.\n"
                "  KEY RULE: The trigger field value must ALWAYS be the actual option name/identifier "
                "(e.g. 'Terminal 1', 'Prestige Suite', 'Swedish Massage'), NEVER a confirmation word "
                "like 'yes', 'ok', 'sure'. Derive the real value from conversation context.\n"
                "  When there is only ONE option in the KB, 'yes' after presenting it means that option "
                "— set the trigger to the option's actual name.\n"
            )
            if (
                _norm(service_id) == "room_booking"
                and _norm(_trigger_id) == "room_type"
            ):
                _ticketing_rules += (
                    "  ROOM BOOKING SPECIAL RULE:\n"
                    "    - A property or hotel choice is NEVER a room_type choice.\n"
                    "    - If the guest selects a property/location first, that only locks the property scope.\n"
                    "    - You must still ask which specific room type they want before triggering the form.\n"
                )
            if not _trigger_id:
                # TRIGGER-LESS FORM OVERRIDE: this form has NO option-selection
                # trigger field — it only collects contact details (e.g. name,
                # phone, email) so the team / website can take it from there.
                # There is therefore NOTHING for the guest to "choose" before the
                # form opens, so the option-gate rules above do NOT apply. A clear
                # intent to proceed is sufficient. NEVER invent a "which option /
                # which room would you like?" question — the form has no such
                # field, and the specific choice (room, date, etc.) is made
                # downstream (e.g. on the booking website).
                #
                # Two-path behavior the agent must decide between, based on
                # known_context (LLM judges — no hardcoded keyword rule):
                #   (a) contact info ALREADY on file  → skip the form, provide the
                #       booking link, and create the ticket.
                #   (b) contact info NOT yet collected → open the form to collect
                #       it; the link + ticket follow once it's submitted.
                _contact_fields_str = ', '.join(form_field_names) if form_field_names else 'name, phone, email'
                _ticketing_rules = (
                    "=== BOOKING / LEAD RULES (MANDATORY — CONTACT-ONLY FORM, NO OPTION GATE) ===\n"
                    f"This service uses an INLINE FORM that collects ONLY the guest's contact details ({_contact_fields_str}). "
                    "It has NO option-selection trigger field — so the guest does NOT need to pick a specific "
                    "room/option/date before proceeding. Those specifics (room, dates, availability) are chosen "
                    "downstream on the booking website. The actual booking is COMPLETED ON THE WEBSITE; you only "
                    "capture the lead and hand over the link.\n\n"
                    "FIRST, CHECK known_context FOR THE CONTACT FIELDS (" + _contact_fields_str + "):\n"
                    "When the guest expresses a genuine intent to proceed / book (e.g. 'book a room', 'I want to "
                    "book', 'let's proceed', 'yes go ahead'), decide which path applies:\n\n"
                    "  PATH A — contact details are ALREADY known (present in known_context, e.g. captured by an "
                    "earlier 'Book a Stay' form or given in chat):\n"
                    "    - Do NOT open the form again — you already have what you need.\n"
                    "    - Provide the booking link: write ONE warm sentence (mention the applicable KB offer if "
                    "any), then on its OWN LINE emit the marker with the active property's exact name:\n"
                    "          <<SHOW_BOOKING_BUTTON: Exact Property Name>>\n"
                    "      (The app turns that marker into a 'Book on website' button — never write a raw URL.)\n"
                    "    - Create the ticket: set action='create_ticket', ticket.required=true, "
                    "ticket.ready_to_create=true, and include the known contact details in pending_data_updates.\n\n"
                    "  PATH B — contact details are NOT yet known (missing from known_context):\n"
                    "    - Open the form in this SAME turn: set action='collect_info', "
                    "pending_action='collect_form_details', missing_fields=[] (the form collects the fields itself).\n"
                    "    - response_text: ONE warm, complete, grammatical sentence inviting them to fill in the "
                    "details, written fresh for what THIS guest actually asked. Never a clipped fragment, and never "
                    "a stock opener reused from another turn — a guest asking for a callback has not 'chosen' "
                    "anything, so acknowledge what they actually said. (The booking link is surfaced once the form "
                    "is submitted.)\n"
                    "    - If the form is ALREADY on screen from an earlier turn (pending_action is already "
                    "collect_form_details) and the guest's message adds no new information, do NOT send the same "
                    "invitation again — they can already see the form. Reply with ONE short acknowledgement in "
                    "different words, and do not restate what the form is for.\n\n"
                    "IF MULTIPLE PROPERTIES are still in scope and none is chosen → briefly ask which property they "
                    "want to book first (do NOT open the form or emit a link until one property is in focus).\n\n"
                    "HARD DO-NOTs:\n"
                    "- NEVER ask 'which room/option/type would you like to book?' as a gate. There is no such field — "
                    "asking it is a dead-end loop; the room is chosen on the website.\n"
                    "- NEVER ask the guest to type 'yes confirm' — the form submission IS the confirmation (Path B).\n"
                    "- NEVER ask for the contact fields in conversation — the form handles them (Path B).\n"
                    "- NEVER emit the booking-link marker on a plain information question — only on real booking intent.\n"
                    "- While the guest is still just browsing/asking questions (not yet intending to proceed), set "
                    "action='respond_only' and answer from the KB; only open the form / give the link on real intent.\n"
                )
                # ── ROOM BOOKING = WEBSITE ONLY (NO IN-CHAT LEAD FORM) ──────
                # For the room/stay booking service this deployment completes
                # bookings on the website, so the contact/lead form is disabled
                # entirely: surface the "Book on website" button instead. This
                # overrides PATH A/B above for the room-booking service only —
                # every other contact-only lead service keeps its form.
                if (
                    bool(getattr(settings, "ticketing_room_booking_website_only", True))
                    and LLMOrchestrationService._service_is_room_stay_booking(None, service_id)
                ):
                    _silent_ticket = bool(getattr(settings, "ticketing_room_booking_silent_ticket_enabled", True))
                    # Staff notification for room bookings is handled SILENTLY in the
                    # backend (a focused classifier raises a KePSLA ticket) — the agent
                    # must NOT drive it via the decision (that path could rewrite the
                    # reply). So always keep the decision ticket OFF here.
                    _ticket_clause = (
                        "  - Set action='respond_only', pending_action=null, and ticket.required=false. Do NOT open a "
                        "form, do NOT ask for contact details, and do NOT create a ticket via the decision — the "
                        "booking completes on the website"
                        + (" and staff are notified automatically in the background" if _silent_ticket else "")
                        + ". NEVER mention a ticket, a request being received, or staff follow-up in your reply.\n"
                    )
                    _ticketing_rules = (
                        "=== ROOM BOOKING — WEBSITE LINK" + (" + SILENT STAFF TICKET" if _silent_ticket else "") + " (MANDATORY, NO FORM) ===\n"
                        "Room/stay bookings are completed on the hotel WEBSITE, never through an in-chat form. You must "
                        "NEVER open a contact/lead form here, NEVER set pending_action='collect_form_details', and NEVER "
                        "ask for name / phone / email — the website handles all of it.\n"
                        "WHEN THE GUEST SHOWS REAL INTENT TO BOOK A ROOM ('book a room', 'i want to book', 'book the "
                        "suite/luxe room', 'how do i book', \"let's book\", 'proceed with booking', 'reserve a room' — "
                        "whether they name a specific room or not) AND exactly ONE property is in focus:\n"
                        "  - Write ONE warm sentence that offers BOTH paths: the guest can complete the booking via the "
                        "website link, OR our team will get back to assist them (mention the applicable KB offer if there "
                        "is one). Then on its OWN LINE emit the marker with the active property's exact name:\n"
                        "        <<SHOW_BOOKING_BUTTON: Exact Property Name>>\n"
                        "    (The app turns that marker into a 'Book on website' button — never write a raw URL.) Include "
                        "the property's current offers as usual.\n"
                        + _ticket_clause +
                        "ONLY a genuine ROOM booking triggers this. While the guest is just BROWSING (info about rooms / "
                        "offers / amenities, comparing, asking questions), OR asking to book a NON-room thing (airport "
                        "shuttle / transfer / pickup, parking, spa, dining, etc.) → set action='respond_only', emit NO "
                        "booking marker, create NO ticket, and DO NOT append room options or room offers after declining the non-room request.\n"
                        "IF 2+ PROPERTIES are in focus and none is chosen → briefly show each in-focus property's room "
                        "types + current offers and ask which one they'd like to book; emit NO marker and NO ticket "
                        "until one property is locked.\n"
                        "HARD DO-NOTs: never open a form, never ask for contact details, never ask the guest to type "
                        "'yes confirm', never emit the marker or create a ticket on a plain information question or a "
                        "non-room booking request.\n"
                    )
        else:
            _ticketing_rules = (
                f"When waiting for guest confirmation before ticket creation, set pending_action to \"{confirm_action}\".\n"
                "CONFIRMATION RULES (mandatory):\n"
                f"- NEVER ask for confirmation (pending_action=\"{confirm_action}\") while ANY required field is still missing.\n"
                "  'Required fields' includes BOTH structured required_slots AND any details mentioned in the admin\n"
                "  ticketing condition text (the 'What to collect before raising a ticket' section above).\n"
                "  Collect ALL missing fields first. Only transition to the confirmation step when every required field has a value.\n"
                f"- When asking for confirmation (pending_action=\"{confirm_action}\"), response_text MUST list the collected "
                "details that are RELEVANT TO THIS SERVICE from pending_data_raw.\n"
                "  Show ONLY the fields that the guest provided or that are specific to this service.\n"
                "  For food/dining orders: ordered items with quantities, delivery time, total price, special instructions.\n"
                "  For room bookings: guest name, room type, check-in/check-out dates, guest count, etc.\n"
                "  For spa/restaurant: treatment/restaurant name, date, time, party size, etc.\n"
                "  Do NOT include internal booking metadata (guest ID, stay dates, confirmation codes) unless\n"
                "  the guest explicitly provided them during THIS conversation for THIS service.\n"
                "  Never say 'the above details' — always spell each one out.\n"
                "- When action=\"create_ticket\" (guest confirmed), response_text MUST again include ALL collected "
                "details relevant to this service from pending_data_raw. Do not include unrelated known_context fields.\n"
            )

        _one_team_per_ticket = (
            "=== ONE TEAM PER REQUEST (MANDATORY) ===\n"
            "Different things go to different teams: food and drink -> Food & "
            "Beverage; towels, pillows, cleaning, toiletries -> Housekeeping; a "
            "repair or something broken -> Engineering; a table, spa, transport or "
            "event -> that service.\n"
            "ONE REQUEST CAN ONLY REACH ONE TEAM. If a message contains work for "
            "two teams, raise the one you own with ONLY its own items in it, then "
            "say plainly that the other is separate and ask the guest to confirm "
            "it - it becomes its own request on the next turn. NEVER put both "
            "teams' work in one request, and NEVER say you have 'also informed' "
            "or 'passed to' a team you have not: saying it does not send it. A "
            "guest reported a broken AC and asked for 2 extra pillows; both went "
            "onto one Engineering request and Housekeeping was never told - the "
            "guest slept without pillows believing they were coming.\n"
            "NEVER DENY SOMETHING THAT BELONGS TO ANOTHER SERVICE. You can only "
            "see your own service's data. If the guest asks for a dish, a "
            "treatment, a table or a transfer you have no list for, you do NOT "
            "know whether the hotel has it - hand it over instead. Saying 'we do "
            "not have that on our menu' from a lane holding no menu tells a guest "
            "the hotel cannot do something it does well: a Chicken Burger was "
            "refused four times in the housekeeping lane, then sold for Rs 750 the "
            "moment the food service saw it.\n"
        )

        _common_rules = (
            "=== PROPERTY SCOPE — NARROW WHEN THE GUEST PICKS ONE (mandatory for multi-property) ===\n"
            "property_scope.active_property_ids / active_property_names list the properties currently in focus.\n"
            "When MORE THAN ONE property is in focus and the guest's message identifies exactly ONE of them — by "
            "naming it, by choosing a room/suite that belongs to only one, or by a distinguishing feature you can "
            "resolve from the KB (e.g. 'the one with the bathtub', 'the rooftop pool one', 'the airport one') — you "
            "MUST set selected_property_ids to ONLY that property's id (taken from property_scope.active_property_ids). "
            "This locks the scope to that single property so the booking link can appear.\n"
            "Until exactly ONE property is in focus, DO NOT give a booking link and DO NOT open a contact form — first "
            "show the relevant rooms/offers for the in-scope properties and ask which one they'd like. Never assume a "
            "property when two or more are in focus.\n"
            "CRITICAL — a bare booking intent is NOT a property signal: 'I want to book a room', 'book a room', 'I want "
            "to book', 'let's book', 'help me book' name NO property. When 2+ properties are in focus and the guest "
            "says one of these, you MUST NOT pick one yourself and MUST NOT set selected_property_ids. Instead: action="
            "respond_only, briefly show each in-focus property with its room types + current offers, and ask which "
            "property they'd like to book. Only AFTER they choose (name it, or a unique room/feature) do you narrow "
            "selected_property_ids to that one and then surface the booking link.\n"
            "If the guest stays broad or asks a property-agnostic question, leave selected_property_ids empty (keep the "
            "current scope) — only narrow on a clear single-property signal.\n"
            "PRESENT BEFORE ASK RULE (mandatory):\n"
            "Before asking the guest to choose a menu item, treatment, room type, package, or any option from a list —\n"
            "FIRST share the full list of available options from the KB. Never ask 'which one?' before showing the options.\n"
            "DEPARTMENT ASSIGNMENT (mandatory when creating a ticket):\n"
            "The payload includes available_departments — a list of {department_id, department_name} objects.\n"
            "When setting ticket.ready_to_create=true, set ticket.department_id to the numeric id of the department\n"
            "that best matches this service (e.g. spa → Wellness, dining → F&B, room issue → Housekeeping).\n"
            "If available_departments is empty or no match, leave department_id empty.\n"
            "PENDING DATA COMPLETENESS (mandatory):\n"
            "pending_data_updates MUST always include EVERY field value you currently know — "
            "not just values collected this turn. If the guest chose a room type on a previous turn "
            "and you are now asking for dates, pending_data_updates must still contain the room type. "
            "Omitting a known value causes the system to lose track of it. "
            "Only omit a field if it is genuinely unknown or listed in missing_fields."
        )

        # Food / dining multi-item ordering rules — the LLM decides if they apply
        # based on the service context it already has (type, cuisine, description, KB).
        _svc_type = str(service_type or "").strip()
        _svc_cuisine = str(service_cuisine or "").strip()
        _svc_desc = str(service_description or "").strip()
        _food_context_hint = ""
        if _svc_type:
            _food_context_hint += f"Service type: {_svc_type}. "
        if _svc_cuisine:
            _food_context_hint += f"Cuisine: {_svc_cuisine}. "
        if _svc_desc:
            _food_context_hint += f"Description: {_svc_desc}. "

        _food_ordering_rules = (
            "\n=== FOOD & DINING MULTI-ITEM ORDERING (apply ONLY if this is a food/dining service) ===\n"
            f"{_food_context_hint}\n"
            "If this service involves food ordering, in-room dining, restaurant ordering, or any "
            "menu-based ordering (you can tell from the service name, type, cuisine, description, "
            "or KB content containing menus/dishes/food items), apply these rules:\n"
            "  1. When the guest mentions a dish, verify it exists in the KB menu. If not found, "
            "politely inform and suggest similar items from the menu.\n"
            "  1b. NEVER SUBSTITUTE THE WRONG DISH, AND NEVER CROSS VEG / NON-VEG: if the guest's exact "
            "dish is NOT on the menu, do NOT silently add a different dish. A match is valid ONLY if it is "
            "genuinely the same dish with the SAME core ingredient and the SAME dietary type. A VEGETARIAN "
            "request (paneer, veg, or any clearly vegetarian dish) must NEVER be resolved to a NON-VEGETARIAN "
            "item (chicken, mutton, lamb, fish, prawn, seafood, egg), and vice versa. Matching on ONE shared "
            "word is NOT a valid match — e.g. 'paneer butter masala' is NOT 'Butter Chicken' (paneer is "
            "vegetarian; that is chicken). When the exact dish is missing, name the closest GENUINE same-type "
            "match and ASK the guest to confirm before you add it (e.g. \"We don't have Paneer Butter Masala; "
            "the closest is our Paneer Aur Shrikewale Pyaz Ki Handi — shall I add that?\"). Add it ONLY after "
            "they say yes. If there is no genuine same-type match, say so and let them choose.\n"
            "  2. After acknowledging each item, ALWAYS ask 'Would you like anything else?' or "
            "'Is there anything else you would like to add?'. Do NOT rush to the form or "
            "confirmation after just one item — guests often order multiple items.\n"
            "  3. Keep a running tally of ordered items and mention it naturally "
            "(e.g. 'So far I have 1x Margherita Pizza. Anything else?').\n"
            "  4. Only proceed to the form/confirmation step when the guest explicitly signals "
            "they are done (e.g. 'that is all', 'nothing else', 'no thanks', 'just that', "
            "'I am done'). A single item mention is NOT a signal to finalize.\n"
            "  5. For form-mode: set the trigger value to the FULL ORDER SUMMARY "
            "(e.g. '2x Margherita Pizza, 1x Caesar Salad, 1x Mango Smoothie'). "
            "For text-mode: collect remaining details only after the order is finalized.\n"
            "  6. If the guest asks about ingredients, allergens, or dietary suitability, "
            "answer from KB data before adding the item to the order.\n"
            "  7. EVERY ITEM MUST BE A COMPLETE, PREPARABLE DISH — BE A PROPER WAITER: if a menu item "
            "needs the guest to pick an option before the kitchen can make it (bread type like "
            "Plain/Butter/Garlic naan, spice level, portion/size, egg or no-egg, etc.), that item is "
            "INCOMPLETE until the guest chooses. A bare category name like 'Naan' is NOT a complete order "
            "line — 'Garlic Naan' is.\n"
            "     - NEVER assume, default, auto-fill, or invent a choice on the guest's behalf.\n"
            "     - While ANY item is still incomplete, do NOT show an order summary, do NOT set a "
            "confirm/place action, and do NOT place the order. Ask ONLY for the missing choice (e.g. "
            "'Which naan would you like — Plain, Butter, or Garlic?'). If the guest says 'confirm'/'place "
            "it' while something is incomplete, that is NOT a signal to proceed — re-ask the missing choice.\n"
            "     - The MOMENT the guest gives the choice — even as a single word ('garlic', 'medium', "
            "'large') — treat it as the answer to your pending question and REWRITE that item's name in "
            "`order_items` to the complete valid menu form (e.g. 'Naan' → 'Garlic Naan'). Never re-ask "
            "something the guest just answered, and never carry a bare/incomplete line into the summary or "
            "the kitchen ticket.\n"
            "     - Only once EVERY line is a complete dish may you move to the summary/confirmation step.\n"
            "  8. CONFIRM EXACTLY ONCE: once every item is a complete dish, show the order summary (each "
            "item with quantity, its chosen option baked into the name, price, the total, and any allergens) "
            "ONE time and ask the guest to confirm. The moment the guest confirms, PLACE the order — do NOT "
            "show the summary again and do NOT ask them to confirm a second time. NEVER set "
            "action=create_ticket while your own reply is still asking the guest to confirm or to choose "
            "something: if you are still asking for anything, the action is respond_only or collect_info, "
            "never create_ticket.\n"
            "If this is NOT a food/dining service, IGNORE this entire section.\n"
        )

        # ── A NICKNAME MUST LAND ON EXACTLY ONE REAL DISH ────────────────────
        # Its own block, NOT part of _food_ordering_rules, because that whole
        # block is deliberately dropped for the food-only agent below - which is
        # the one agent that takes food orders. Written in there, this rule was
        # never sent to the IRD model at all.
        #
        # The IRD prompt says never to answer "not on the menu" just because the
        # guest used a shorter name. That is right, and it is about REAL dishes
        # with short names - but it has no branch for a name matching NOTHING, so
        # "Kadak" was answered with "we do serve Kadak". There is no Kadak on the
        # menu; it is a style of tea. The same word later invented an outlet.
        _menu_name_must_resolve = (
            "\n=== A NAME YOU CANNOT FIND ON THE MENU IS NOT A DISH ===\n"
            "A short or informal name must resolve to EXACTLY ONE item on the menu. "
            "Find the items it could mean before you accept it.\n"
            "- Exactly one match: that is the dish. Use its full menu name and carry on.\n"
            "- More than one match: ask which they mean and name those dishes.\n"
            "- NO match: we do not have it. Say so plainly and offer the closest real "
            "item, e.g. \"I don't have a Kadak on the menu - our Chai is a traditional "
            "Indian tea with aromatic spices. Shall I order that?\"\n"
            "NEVER confirm, price, describe, add or order a name you could not find on "
            "the menu, and never treat a style, a preparation or a serving strength "
            "(kadak, strong, spicy, medium) as though it were a dish we serve. Saying "
            "\"we do serve X\" about something that is not on the menu is worse than "
            "saying we don't have it.\n"
        )

        # A SINGLE-PROPERTY FOOD SERVICE DOES NOT NARROW PROPERTIES.
        # _common_rules is multi-property scoping - which property the guest
        # means, when a booking link may appear, continuity across turns - and
        # Sarovar is one hotel taking a food order. Measured live, this agent
        # reads a 100k-char system prompt in which the guest's actual order is
        # 2%, and it answered against prose rather than that order.
        if food_only_service:
            # _food_ordering_rules is the GENERIC "if this is a food service"
            # block, written for any service that happens to sell food. The IRD
            # agent has its own 30k prompt about ordering plus the order-flow
            # directive, so this is a third set of rules on the same subject -
            # and it contradicts them: it orders the bot to "ALWAYS ask 'would
            # you like anything else?'" after every item and never confirm after
            # one, which is exactly why it kept ending on "what else would you
            # like to add?" instead of offering to place the order.
            #
            # _common_rules goes too: it is multi-property scoping and this is
            # one hotel taking a food order.
            return (
                _guest_facts_rule
                + _validation_rules
                + _json_shape
                + _ticketing_rules
                + _one_team_per_ticket
                + _menu_name_must_resolve
            )
        return (
            _guest_facts_rule
            + _validation_rules
            + _json_shape
            + _ticketing_rules
            + _one_team_per_ticket
            + _common_rules
            + _food_ordering_rules
            + _menu_name_must_resolve
        )

    # ── SAROVAR IN-ROOM DINING: structured order state ("the cart") ──────────
    # The IRD agent used to rebuild the whole order from a lossy chat history each
    # turn, so a DECLINED / off-menu item (e.g. "gulab jamun") could ride into the
    # order and the F&B ticket. We keep the order as real server-side state instead:
    # the model reports its full current order as `pending_data_updates.order_items`
    # ([{name, qty}]); we validate each against the menu, keep only real items, store
    # the result as the authoritative order, feed it back next turn, and build the
    # ticket from IT — never from the model's free-text summary. Gated to Sarovar's
    # in_room_dining service, so no other tenant/service is affected.
    _IRD_ORDER_STOPWORDS = frozenset({
        "the", "and", "with", "for", "one", "two", "some", "please", "order", "add",
        "extra", "of", "a", "an", "my", "to", "room", "veg", "non", "plain",
    })
    # Non-vegetarian markers. Used ONLY as a safety net: if the model resolves an
    # inexact dish to a real menu item containing one of these (e.g. "paneer butter
    # masala" -> "Butter Chicken") while the guest actually asked for a vegetarian
    # dish and never named the non-veg item, we drop it so the wrong dish can never
    # reach the kitchen. See _sarovar_ird_apply_order_state.
    _IRD_NONVEG_WORDS = frozenset({
        "chicken", "mutton", "lamb", "fish", "prawn", "prawns", "seafood",
        "egg", "keema", "meat", "crab", "squid", "beef", "pork", "bacon", "ham",
    })










    async def _run_service_agent(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        memory_snapshot: dict[str, Any],
        service: dict[str, Any],
        base_decision: OrchestrationDecision,
        selected_phase_id: str,
        selected_phase_name: str,
    ) -> OrchestrationDecision:
        service_id = self._normalize_identifier(service.get("id"))
        service_name = str(service.get("name") or service_id).strip()
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        prompt_pack = service.get("service_prompt_pack")
        if not isinstance(prompt_pack, dict):
            prompt_pack = {
                "profile": service.get("profile"),
                "role": service.get("service_prompt_role"),
                "professional_behavior": service.get("service_prompt_behavior"),
                "required_slots": service.get("required_slots"),
                "ticketing_policy": {"policy": service.get("ticketing_policy")},
                "execution_guard": service.get("execution_guard"),
                "confirmation_format": service.get("confirmation_format"),
            }
        pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
        selected_property_hint = str(
            pending_raw.get("_selected_property_scope_id")
            or pending_raw.get("_selected_property_scope_name")
            or ""
        ).strip()
        known_property_scope_count = int(pending_raw.get("_known_property_scope_count") or 0)
        pending_action_active = bool(str(context.pending_action or "").strip())
        allow_multi_property_switch_decision = bool(
            pending_action_active
            and known_property_scope_count > 1
        )
        current_active_property_ids = self._normalize_property_id_list(
            list(pending_raw.get("_active_property_ids") or [])
        )
        current_active_property_names = [
            str(name).strip()
            for name in list(pending_raw.get("_active_property_names") or [])
            if str(name).strip()
        ]
        service_property_scope_payload: dict[str, Any] = {
            "active_property_ids": list(current_active_property_ids),
            "active_property_names": list(current_active_property_names),
            "selected_property_scope_id": str(pending_raw.get("_selected_property_scope_id") or "").strip(),
            "selected_property_scope_name": str(pending_raw.get("_selected_property_scope_name") or "").strip(),
            "past_property_context": self._sanitize_json(
                dict(pending_raw.get("_past_property_context") or {})
            ),
            "detected_properties": [],
            "available_locations": [],
            "allow_mid_flow_property_switch": allow_multi_property_switch_decision,
        }
        service_property_rows: list[dict[str, Any]] = []
        if allow_multi_property_switch_decision:
            hotel_code = str(getattr(context, "hotel_code", "") or "").strip()
            if hotel_code:
                try:
                    from services.kb_table_reader import get_property_catalog_db

                    property_catalog = await get_property_catalog_db(hotel_code=hotel_code)
                except Exception:
                    property_catalog = []
                service_property_rows, service_location_rows = self._build_available_property_scope(
                    property_catalog
                )
                if service_property_rows:
                    pending_raw["_property_name_map"] = self._merge_property_name_map(
                        pending_raw.get("_property_name_map"),
                        service_property_rows,
                    )
                    service_property_scope_payload["detected_properties"] = [
                        {
                            "id": str(row.get("id") or "").strip(),
                            "name": str(row.get("name") or "").strip(),
                            "aliases": list(row.get("aliases") or []),
                            "location": str(row.get("location") or "").strip(),
                        }
                        for row in service_property_rows[:40]
                        if isinstance(row, dict)
                    ]
                    service_property_scope_payload["available_locations"] = [
                        {
                            "id": str(row.get("id") or "").strip(),
                            "label": str(row.get("label") or "").strip(),
                            "property_ids": list(row.get("property_ids") or []),
                            "property_names": list(row.get("property_names") or []),
                        }
                        for row in service_location_rows[:20]
                        if isinstance(row, dict)
                    ]
        service_facts_raw = service.get("knowledge_facts", [])
        if not isinstance(service_facts_raw, list):
            service_facts_raw = []
        service_facts: list[str] = [str(f).strip() for f in service_facts_raw if str(f).strip()]
        # Top-level `extracted_knowledge` is populated by `_service_snapshot`
        # but some dispatch paths feed this function the raw cached row instead,
        # which keeps the KB nested under `service_prompt_pack.extracted_knowledge`.
        # Fall back to the nested location so the agent never runs blind.
        extracted_knowledge = (
            str(service.get("extracted_knowledge") or "").strip()
            or str((service.get("service_prompt_pack") or {}).get("extracted_knowledge") or "").strip()
        )
        if extracted_knowledge and not allow_multi_property_switch_decision:
            extracted_knowledge = self._scope_service_knowledge_to_runtime_property(
                service_knowledge=extracted_knowledge,
                user_message=user_message,
                context=context,
            )
        # SERVING TIMES AND TODAY'S VERDICT, WRITTEN NEXT TO THE DISH.
        # The availability list says which dishes are shut; it cannot say a dish is
        # UNrestricted except by not mentioning it, and absence is weak evidence -
        # asked for a Filter Coffee at 13:34, an all-day drink nowhere on that list,
        # the model answered "served only from 7:00 AM to 10:30 AM". Marking the ~27
        # restricted dishes makes the other ~130 visibly unmarked. The line carries
        # AVAILABLE NOW / NOT AVAILABLE NOW rather than bare hours, because bare
        # hours had the model comparing them to the clock itself and refusing a
        # Tandoori Murgh at 13:46 from inside its own 12-3 window. See annotate_menu.
        if extracted_knowledge:
            try:
                from services.ird_availability import annotate_menu

                extracted_knowledge = annotate_menu(
                    str(getattr(context, "hotel_code", "") or ""),
                    extracted_knowledge,
                    self._hotel_now(),
                )
            except Exception as _ann_exc:
                logger.warning(
                    "[IRD_AVAIL] menu annotation skipped (%r) - the menu is used "
                    "exactly as stored", _ann_exc,
                )
        _raw_pack = service.get("service_prompt_pack")
        if not isinstance(_raw_pack, dict):
            _raw_pack = {}
        _raw_pack_source = str(_raw_pack.get("source") or "").strip().lower()
        _pack_is_admin_managed = (
            bool(service.get("service_prompt_pack_custom", False))
            or _raw_pack_source in {"manual_override", "admin_ui", "admin_override", "db"}
        )
        required_slots_raw = prompt_pack.get("required_slots", [])
        if _pack_is_admin_managed and isinstance(required_slots_raw, list):
            required_slots = required_slots_raw
        else:
            required_slots = []
        validation_rules_raw = prompt_pack.get("validation_rules", [])
        if _pack_is_admin_managed and isinstance(validation_rules_raw, list):
            validation_rules = validation_rules_raw
        else:
            validation_rules = []
        confirmation_format = prompt_pack.get("confirmation_format", {})
        if not isinstance(confirmation_format, dict):
            confirmation_format = {}
        behavior = str(prompt_pack.get("professional_behavior") or "").strip()
        service_profile = str(prompt_pack.get("profile") or "").strip().lower()
        role = str(prompt_pack.get("role") or "").strip() or f"You are the {service_name} service assistant."
        description = str(service.get("description") or "").strip()
        ticketing_policy = prompt_pack.get("ticketing_policy", {})
        if not isinstance(ticketing_policy, dict):
            ticketing_policy = {}
        ticketing_conditions = str(prompt_pack.get("ticketing_conditions") or "").strip()
        if not ticketing_conditions:
            ticketing_conditions = str(ticketing_policy.get("policy") or "").strip()
        if not ticketing_conditions:
            ticketing_conditions = str(service.get("ticketing_policy") or "").strip()
        service_hours = service.get("hours") or {}
        if not isinstance(service_hours, dict):
            service_hours = {}
        service_delivery_zones = service.get("delivery_zones") or []
        if not isinstance(service_delivery_zones, list):
            service_delivery_zones = []
        service_cuisine = str(service.get("cuisine") or "").strip()
        ticketing_enabled = bool(service.get("ticketing_enabled", True))
        slots_are_custom = _pack_is_admin_managed and bool(required_slots)

        model = str(getattr(settings, "llm_service_agent_model", "") or "").strip() or None
        confirmation_phrase = str(confirmation_format.get("required_phrase") or "").strip()
        if not confirmation_phrase:
            confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "yes confirm").strip()
        confirmation_pending_action = "confirm_booking"
        # Confirmation is required for text-mode ticketing — the guest must confirm
        # before a ticket is raised.  For form-mode services, the form submission
        # itself acts as confirmation, so we skip the "yes confirm" step entirely.
        _svc_ticketing_mode = str(service.get("ticketing_mode") or "").strip().lower()
        requires_confirmation_step = ticketing_enabled and _svc_ticketing_mode != "form"

        # Extract form_config details for form-mode services (used by runtime contract)
        _svc_form_config = service.get("form_config") if isinstance(service.get("form_config"), dict) else {}
        _svc_trigger_field = _svc_form_config.get("trigger_field") if isinstance(_svc_form_config.get("trigger_field"), dict) else {}
        _svc_trigger_field_id = str(_svc_trigger_field.get("id") or "").strip()
        _svc_trigger_field_label = str(_svc_trigger_field.get("label") or _svc_trigger_field_id).strip()
        _svc_form_field_names: list[str] = []
        _svc_form_fields_payload: list[dict[str, Any]] = []
        for _ff in (_svc_form_config.get("fields") or []):
            if not isinstance(_ff, dict):
                continue
            _field_id = str(_ff.get("id") or "").strip()
            _field_label = str(_ff.get("label") or _field_id).strip()
            if _field_label:
                _svc_form_field_names.append(_field_label)
            if _field_id:
                _svc_form_fields_payload.append({
                    "id": _field_id,
                    "label": _field_label,
                    "type": str(_ff.get("type") or "text").strip(),
                    "required": bool(_ff.get("required", True)),
                    "validation_prompt": str(_ff.get("validation_prompt") or "").strip(),
                })
        _svc_form_field_names = [n for n in _svc_form_field_names if n]

        # For form-mode services, suppress confirmation phrase — form submission is the confirmation
        if _svc_ticketing_mode == "form":
            confirmation_phrase = ""

        history_bundle = self._build_history_bundle(
            context=context,
            memory_snapshot=memory_snapshot,
            last_window_messages=10,
            last_window_chars=max(4000, int(getattr(settings, "llm_orchestration_history_chars", 8000) or 8000)),
            full_window_messages=max(
                60,
                int(getattr(settings, "llm_orchestration_full_history_messages", 120) or 120),
            ),
            full_window_chars=max(
                7000,
                int(getattr(settings, "llm_orchestration_full_history_chars", 12000) or 12000),
            ),
        )
        pending_pub_pre = self._coerce_public_pending(context.pending_data)
        # First turn = no slot data collected yet for this service
        is_first_service_turn = not any(
            str(v or "").strip()
            for k, v in pending_pub_pre.items()
            if k not in {"service_id"}
        )
        # Service agent primarily uses its own extracted_knowledge. On pending
        # multi-property turns where the LLM may need to detect a property
        # switch mid-flow, we can also provide broader hotel KB support as a
        # fallback when the service KB lacks property sections.
        full_kb_text = ""  # kept for backward compat with _build_service_system_prompt
        if (
            allow_multi_property_switch_decision
            and selected_property_hint
            and known_property_scope_count > 1
            and "=== PROPERTY:" not in str(extracted_knowledge or "")
            and service_property_rows
        ):
            try:
                _multi_property_scope = await config_service.get_kb_text_for_properties(
                    property_ids=[
                        str(row.get("id") or "").strip()
                        for row in service_property_rows
                        if str(row.get("id") or "").strip()
                    ],
                    include_manifest=False,
                    include_common=True,
                    past_property_context=(
                        dict(pending_raw.get("_past_property_context") or {})
                        if isinstance(pending_raw.get("_past_property_context"), dict)
                        else None
                    ),
                    max_sources=120,
                    allow_llm_generation=False,
                )
                full_kb_text = str((_multi_property_scope or {}).get("scoped_kb_text") or "").strip()
            except Exception:
                full_kb_text = ""

        # Prefer LLM-generated prompt if available; append KB knowledge so the
        # agent always has access to its data even when using the generated prompt.
        _generated_prompt = str(service.get("generated_system_prompt") or "").strip()
        # FAQ-as-primary-truth: runtime-inject admin FAQ as PRIMARY KNOWLEDGE
        # ahead of the steady-state KB. Empty string when no FAQs configured.
        # Scope to the focused property so other properties' FAQs never leak in;
        # common (unscoped) FAQs always pass.
        _faq_active_scope = pending_raw.get("_active_property_ids")
        _faq_block = build_faq_block(_faq_active_scope)
        faq_candidates = self._build_faq_candidates(active_property_slugs=_faq_active_scope)
        if _generated_prompt:
            kb_sections: list[str] = []
            if str(extracted_knowledge or "").strip():
                kb_sections.append(str(extracted_knowledge or "").strip())
            # THE FOOD AGENT'S KB IS THE MENU. full_kb_text is the hotel's
            # property knowledge - suite bed configurations, room areas, Wi-Fi
            # instructions, outlet hours - ~8k of it, none of which helps take a
            # food order, and all of which competes with the menu and the live
            # order state for the model's attention.
            # service_id is already normalised to "in_room_dining" here;
            # service.get("id") is the numeric DB id and never matches.
            _food_only = self._is_sarovar_ird(context, service_id)
            if str(full_kb_text or "").strip() and not _food_only:
                kb_sections.append("=== PROPERTY-SCOPED HOTEL KNOWLEDGE ===\n" + str(full_kb_text or "").strip())
            kb_section = "\n\n".join(section for section in kb_sections if section).strip()
            if kb_section:
                system_prompt = _generated_prompt + "\n\n=== KNOWLEDGE BASE ===\n" + kb_section
            else:
                system_prompt = _generated_prompt
            if _faq_block:
                system_prompt = _faq_block + "\n\n" + system_prompt
            # Missing-info -> staff handoff is judgment-based (the LLM decides what
            # counts as a genuine booking-relevant fact) and scoped to pre_booking.
            _missing_info_handoff_clause = self._build_room_missing_info_handoff_clause(
                selected_phase_id
            )
            system_prompt = (
                f"{system_prompt}\n\n"
                "=== HARD KB-ONLY RULE (HIGHEST PRIORITY) ===\n"
                "- The `=== KNOWLEDGE BASE ===` section above is your ONLY source of truth. If something is in the KB you can use it; if not, you don't have it.\n"
                "- Never invent room types, prices, hours, amenities, packages, menu items, treatments, or features — not even generic hotel-industry defaults like 'Standard Room', 'Family Room', 'Deluxe Suite'.\n"
                "- If the KB is silent on a specific detail the guest asked for, reply with one short bullet: '- I don't have that detail right now.' Never fill the gap with plausible-sounding fabrication.\n"
                "- Your `suggested_actions` chips must each be answerable from the SAME KB you used in `response_text`. Never include a chip referring to an item you didn't see in KB.\n\n"
                "=== OUTPUT FORMAT (HARD) ===\n"
                "- Keep the answer clean, selective, and premium to read.\n"
                "- Simple asks may be answered in 1-3 short lines.\n"
                "- When multiple facts help, use 2-5 compact points or mini sections. Do NOT force '-' bullets every time.\n"
                "- A brief lead-in or heading is allowed when it improves readability.\n"
                "- Use `**...**` selectively for room names, offers, labels, and the main answer terms.\n"
                "- Use at most 1-2 relevant emojis when they help scanning naturally.\n"
                "- Avoid long paragraphs, dry field dumps, and repetitive filler.\n"
                "- No filler openers ('Currently,', 'At our hotel,', 'Please note that,', 'I'd be happy to,').\n"
                "- No internal phase names ('pre_booking', 'during_stay', 'pre_checkin', 'post_checkout') in the visible reply.\n"
                "- Trivial replies (single-line ack, denial, simple confirmation) may stay as one clean sentence or two short lines.\n"
                "- EXAMPLES:\n"
                "  Multi-option: '- **Café Vindhyas** — *Himachali and global cuisine* with scenic views.'\n"
                "  Single-fact: '**Check-in:** 2:00 PM.'\n"
                "  Recommendation: '- **Premium Forest Villa** — best for families; *fits 2 adults + 2 children*.'\n\n"
                "=== OFF-DOMAIN HANDOFF ===\n"
                "- This service has a specific scope — only what your role/description above covers.\n"
                "- If the guest's message is asking for something OUTSIDE this scope (e.g. you are a room-booking agent and the guest asks about food, drinks, spa, weather, hall booking, towels), set `context_switched: true` and `response_text: \"\"`. The orchestrator will route the next turn correctly.\n"
                "- Informational follow-ups about THIS service (e.g. 'what's the size of the suite' while booking a room) stay with you — answer those.\n"
                "- Never run a different service yourself. Never invent items just because the guest asked for something the active service doesn't handle.\n"
                # ── OUT OF YOUR SCOPE IS NOT THE SAME AS UNAVAILABLE ──────────
                # "Never run a different service" was read as "refuse it": asked
                # for a Chicken Burger mid-way through another service, the bot
                # told the guest it was not available. The hotel serves it - it
                # simply is not this agent's to take. Handing over and refusing
                # look identical to the agent and opposite to the guest.
                "- OUTSIDE YOUR SCOPE IS NOT 'WE DON'T HAVE IT'. Never tell a guest the hotel "
                "cannot do something merely because YOU cannot. If another team handles it, "
                "hand over with `context_switched: true` and say nothing about availability. "
                "Only decline outright when the hotel genuinely does not offer it at all — and "
                "if you are not certain of that, hand over rather than refuse.\n\n"
                "=== PHASE-AWARE DENIAL ===\n"
                "- If the guest asks you to execute something this service does NOT handle (different domain) AND that other service is not active in the current phase, set `context_switched: true` so the orchestrator's book-route can deny it cleanly. Do not pretend you can do it later.\n"
                f"{_missing_info_handoff_clause}"
                "=== COMPLETENESS RULE ===\n"
                "When the guest asks about rooms, options, packages, amenities, services, or any list of offerings:\n"
                "- List ALL items from the knowledge base — never summarize, skip, or show only a partial list.\n"
                "- For each item, highlight only the most useful guest-facing differentiators unless the guest asked for exact specs.\n"
                "- Do NOT say 'and more' or 'among others' — be exhaustive.\n\n"
                "=== KNOWLEDGE PRECISION RULE ===\n"
                "- Never generalize one fact into a different or stronger claim.\n"
                "- A room amenity, service mention, or nearby-landmark mention does NOT prove a different facility, menu, package, policy, or availability unless the KB explicitly says so.\n"
                "- Comparative asks such as cheapest, most affordable, best value, best, largest, smallest, closest, or similar need an explicit KB basis. If price/size/distance/rating data is missing, say that basis is not available instead of naming a winner.\n"
                "- When the guest explicitly asks for room size, area, or dimensions, include the exact size values from the KB if present.\n\n"
                "=== PROPERTY CONTINUITY RULE ===\n"
                "- The payload may include property_scope with active_property_ids, detected_properties, and available_locations.\n"
                "- If the guest explicitly switches to a different property or location mid-flow, update selected_property_ids and selected_property_name and answer from that property's KB in the same turn.\n"
                "- If the guest mentions a location with multiple properties, selected_property_ids must include ALL properties from that location.\n"
                "- When the property changes, stop carrying forward old property-specific selections unless the guest repeats them.\n\n"
                "ROOM PRESENTATION STYLE: For room options, lead with what makes each room uniquely appealing to guests "
                "(experience, view, inclusions, standout features). Do not lead with square-foot/area specs, "
                "and mention size only if the guest explicitly asks for size/area.\n\n"
                "=== RUNTIME RULES (OVERRIDE — these take precedence over any earlier instructions) ===\n"
                f"{self._build_service_runtime_json_contract(service_id=str(service.get('id') or '').strip(), food_only_service=self._is_sarovar_ird(context, service_id), confirmation_pending_action=confirmation_pending_action, current_phase_id=selected_phase_id, ticketing_mode=_svc_ticketing_mode, trigger_field_id=_svc_trigger_field_id, trigger_field_label=_svc_trigger_field_label, form_field_names=_svc_form_field_names, service_type=str(service.get('type') or '').strip(), service_cuisine=service_cuisine, service_description=description, include_faq_reporting=bool(faq_candidates))}"
            )
        else:
            system_prompt = self._build_service_system_prompt(
                service_id=service_id,
                service_name=service_name,
                role=role,
                behavior=behavior,
                description=description,
                service_knowledge=extracted_knowledge,
                knowledge_facts=service_facts[:30],
                required_slots=required_slots,
                slots_are_custom=slots_are_custom,
                validation_rules=validation_rules,
                confirmation_format=confirmation_format,
                ticketing_enabled=ticketing_enabled,
                ticketing_policy=ticketing_policy,
                ticketing_conditions=ticketing_conditions,
                service_hours=service_hours,
                delivery_zones=service_delivery_zones,
                cuisine=service_cuisine,
                confirmation_phrase=confirmation_phrase,
                confirmation_pending_action=confirmation_pending_action,
                requires_confirmation_step=requires_confirmation_step,
                full_kb_text=full_kb_text,
                current_phase_id=selected_phase_id,
            )
            if _faq_block:
                system_prompt = _faq_block + "\n\n" + system_prompt
        # Only ask for FAQ reasoning when there are actually FAQs to reason about.
        # Measured over 77 real service-agent turns: faq_resolution came back
        # EMPTY 100% of the time, because these hotels have no FAQ rows — yet the
        # model still paid to emit the empty array plus a resolved_facts list that
        # just restates response_text. Neither field is read by anything outside
        # _normalize_faq_resolution_metadata. When FAQs DO exist the block is
        # appended exactly as before.
        faq_resolution_rules = "" if not faq_candidates else (
            "\n=== FAQ RESOLUTION (SMART MERGE/OVERRIDE) ===\n"
            "The payload includes faq_candidates (admin-curated Q/A entries).\n"
            "Treat faq_candidates as high-priority current facts.\n"
            "If FAQ conflicts with steady-state KB, FAQ is current truth (relation=override).\n"
            "If FAQ adds detail to KB, include both (relation=merge).\n"
            "If FAQ is unrelated, ignore it (relation=ignore).\n"
            "For amenities/facilities asks, if FAQ marks an amenity unavailable or under maintenance,\n"
            "do not present it as open/available in the same response.\n"
            "For amenities lists, clearly separate available vs temporarily unavailable items.\n"
            "If useful, phrase context as: 'Usually X, but currently Y.'\n"
            "Do not skip relevant FAQ inclusions in the final answer.\n"
            "Record your reasoning in metadata.faq_resolution (faq_id, relation, applied, confidence, reason, fact)\n"
            "and metadata.resolved_facts (source=faq:<id>|kb, fact=short line).\n"
            "Example: if FAQ says Lux Suite includes complimentary dinner, include that in the Lux Suite answer.\n"
        )
        system_prompt = system_prompt + faq_resolution_rules
        system_prompt = system_prompt + self._build_service_response_text_style_contract()
        _sarovar_hotel_code = str(getattr(context, "hotel_code", "") or "").strip().lower()
        if _sarovar_hotel_code == "sarovar_portico_sriperumbudur":
            system_prompt += (
                "\n=== SAROVAR STYLE OVERRIDE (HIGHEST PRIORITY) ===\n"
                "- This override applies only to Sarovar Portico Sriperumbudur.\n"
                "- Follow any hotel/service response-style or menu-display instructions above exactly.\n"
                "- For multi-point guest replies, use one short warm lead-in, then compact `•` bullets or short emoji-led mini-headings.\n"
                "- Put one meaningful `**bold**` highlight in every visible content point.\n"
                "- Use 1-3 relevant emojis total when they improve readability; keep them tasteful, never spammy.\n"
                "- Do not flatten a styled answer into a dense paragraph, comma dump, or plain hyphen list.\n"
                "- For menus or dining options, always include at least one relevant food/drink emoji in the lead-in or section heading.\n"
                "- For menus or dining options, never dump items in one block. Use a short lead-in, then veg/non-veg or category mini-headings when helpful, then one `•` bullet per dish/item.\n"
                "- When both veg and non-veg are present, split them under mini-headings such as `🥬 **Vegetarian**` and `🍗 **Non-Vegetarian**`.\n"
                "- For dining/menu bullets, bold the dish name and, when natural, the price or key answer term.\n"
                "- After a menu/options list, end with one clean next-step line.\n"
            )

        # Bug B fix — a FRESH service request (nothing being collected/confirmed
        # right now → pending_action empty) must not inherit request-scoped slot
        # values left over from a PRIOR request the guest abandoned without
        # confirming. Drop them the same way a completed ticket does, keeping
        # identity/scope/internal + last-service anchors. Without this, e.g. an
        # abandoned "toothbrush" request leaks into a later "room not cleaned"
        # confirmation. Mid-collection turns keep their data (pending_action set).
        if not str(context.pending_action or "").strip():
            context.pending_data = conversation_memory_service.retain_safe_internal_entries(
                context.pending_data,
                preserve_last_service=True,
            )

        pending_pub = self._coerce_public_pending(context.pending_data)
        # Build a human-readable view of already-collected slot data for the LLM
        collected_labels: dict[str, Any] = {}
        for s in required_slots:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            val = pending_pub.get(sid)
            if val is not None:
                collected_labels[s.get("label") or sid] = val

        # The guest's clock, not the server's - a menu that opens and closes by
        # time of day has to be judged in the timezone the guest is standing in.
        try:
            from zoneinfo import ZoneInfo as _ZI
            _svc_tz_name = str(getattr(settings, "hotel_timezone", "") or "UTC").strip() or "UTC"
            _svc_tz = _ZI(_svc_tz_name)
        except Exception:
            _svc_tz_name, _svc_tz = "UTC", UTC
        _now_svc = datetime.now(_svc_tz)
        # Build known_context: merge memory facts + public pending_data as pre-filled guest slots
        _mem_facts = memory_snapshot.get("facts", {})
        _known_ctx: dict[str, Any] = {}
        _internal_booking_ref: dict[str, Any] = {}
        # Keys that are internal booking/stay metadata — useful for date-based logic
        # but should NEVER be displayed to the guest in confirmation messages.
        _booking_metadata_keys = {
            "guest_id", "booking_id", "reservation_id", "confirmation_code",
            "booking_confirmation_code", "booking_check_in_date", "booking_check_out_date",
            "booking_guest_name", "booking_property_name", "booking_room_type",
            "stay_check_in_date", "stay_check_out_date", "stay_date_range",
            "check_in_date", "check_out_date", "checkin_date", "checkout_date",
            "check_in", "check_out",
        }
        # Internal routing / scope metadata that _coerce_public_pending mirrors onto
        # the public pending map for orchestration (chips, routing). These must NEVER
        # surface in the guest-facing confirmation summary — fixes the leak of
        # 'Last Service Id / Last Service Name / Service' into "Shall I go ahead?".
        _internal_routing_keys = {
            "active_property_ids", "active_property_names", "past_property_context",
            "selected_property_scope_id", "selected_property_scope_ids",
            "selected_property_scope_name", "stay_property_id", "stay_property_name",
            "last_service_id", "last_service_name", "service_id", "service",
            "selected_phase_id", "property_name_map",
        }
        if isinstance(_mem_facts, dict):
            for _k, _v in _mem_facts.items():
                if _v is not None and str(_v).strip():
                    _key_lower = str(_k).strip().lower()
                    if _key_lower in _internal_routing_keys:
                        continue
                    if _key_lower in _booking_metadata_keys:
                        _internal_booking_ref[str(_k)] = _v
                    else:
                        _known_ctx[str(_k)] = _v
        for _k, _v in pending_pub.items():
            if (
                _v is not None and str(_v).strip()
                and not str(_k).startswith("_")
                and str(_k).strip().lower() not in _internal_routing_keys
            ):
                _known_ctx[str(_k)] = _v

        # Merge booking context fields into internal booking reference (not known_context)
        for _bk_attr in (
            "booking_guest_name", "booking_property_name", "booking_room_type",
            "booking_check_in_date", "booking_check_out_date", "booking_confirmation_code",
        ):
            _bk_val = getattr(context, _bk_attr, None)
            if _bk_val:
                _internal_booking_ref[_bk_attr] = str(_bk_val)
        if getattr(context, "room_number", None):
            _known_ctx["room_number"] = context.room_number
        if getattr(context, "guest_name", None):
            _known_ctx["guest_name"] = context.guest_name

        # Fetch available departments (best-effort) so LLM can assign the right one to the ticket.
        _available_departments: list[dict[str, Any]] = []
        if ticketing_enabled:
            try:
                from integrations.lumira_ticketing_repository import lumira_ticketing_repository as _ltr
                from models.database import AsyncSessionLocal as _ASL
                _integration_ctx = {}
                _pd = context.pending_data if isinstance(context.pending_data, dict) else {}
                _int_raw = _pd.get("_integration")
                if isinstance(_int_raw, dict):
                    _integration_ctx = _int_raw
                _entity_id = (
                    _integration_ctx.get("entity_id")
                    or _integration_ctx.get("organisation_id")
                    or _integration_ctx.get("org_id")
                )
                if _entity_id:
                    async with _ASL() as _db:
                        _available_departments = await _ltr.fetch_departments_of_entity(_db, _entity_id)
            except Exception:
                pass  # department list is best-effort; empty list = LLM leaves department_id blank

        # WHAT THE KITCHEN ALREADY HAS. Held under internal underscore keys, which
        # _coerce_public_pending strips, so none of it reached the agent: it saw a
        # cart, could not tell the food had gone, and asked the guest to confirm an
        # order already being cooked.
        # ── IS THIS A NEW ORDER? DECIDED BEFORE THE AGENT SEES ANYTHING ──
        # This used to be settled after the agent had already replied, so on the
        # turn the guest asked for a new order the agent was still looking at
        # "one open order, you are adding to it" - and said so - while the code
        # closed that order a moment later. The agent also never saw an unplaced
        # new order at the point it had to decide whether to raise a ticket, so
        # the second order never reached the kitchen.
        try:
            _pd_split = context.pending_data if isinstance(context.pending_data, dict) else None
            if (
                _pd_split
                and _pd_split.get("_ird_order_ticket_ref")
                and str(user_message or "").strip()
            ):
                # EVERYTHING THEY HAVE SAID SINCE THE ORDER WENT IN - which is
                # what this judge's prompt has always described, and never
                # received. It was handed the single current message instead, so
                # "can i order continental breakfast" - a plain enquiry about a
                # dish, refused moments later as out-of-hours - read as a
                # fresh-order announcement on the strength of the word "order".
                # That closed the open order ten turns before the guest actually
                # asked for a new one, and by then the mechanism was spent.
                _since: list[str] = []
                try:
                    _placed_stamp = str(_pd_split.get("_ird_order_placed_at") or "")
                    for _m in (context.get_recent_messages(20) or []):
                        _role = str(getattr(getattr(_m, "role", None), "value",
                                            getattr(_m, "role", ""))).lower()
                        if _role != "user":
                            continue
                        _txt = str(getattr(_m, "content", "") or "").strip()
                        if _txt:
                            _since.append(_txt[:200])
                    _since = _since[-6:]
                except Exception:
                    _since = []
                if str(user_message or "").strip() not in _since:
                    _since.append(str(user_message).strip()[:200])
                _sep = await self._ird_guest_wants_a_separate_order(
                    user_message=" | ".join(_since) if _since else str(user_message),
                    open_order_items=list(_pd_split.get("_ird_open_order_items") or []),
                )
                if _sep:
                    _leaving_ref = str(_pd_split.get("_ird_order_ticket_ref") or "").strip()
                    _closed_refs = [
                        str(r) for r in (_pd_split.get("_ird_closed_order_refs") or []) if str(r)
                    ]
                    if _leaving_ref and _leaving_ref not in _closed_refs:
                        _closed_refs.append(_leaving_ref)
                    _pd_split["_ird_closed_order_refs"] = _closed_refs[-10:]
                    _pd_split["_ird_closed_items"] = sorted(
                        {
                            str(l.get("item") or "").strip().lower()
                            for l in (_pd_split.get("_ird_cart") or [])
                            if isinstance(l, dict) and str(l.get("item") or "").strip()
                        }
                        | {
                            str(i.get("name") or "").strip().lower()
                            for r in (_pd_split.get("_ird_placed_orders") or [])
                            if isinstance(r, dict)
                            for i in (r.get("items") or []) if isinstance(i, dict)
                            and str(i.get("name") or "").strip()
                        }
                    )
                    _pd_split["_ird_cart"] = []
                    _pd_split.pop("_ird_vetted_items", None)
                    _pd_split.pop("_ird_order_ticket_ref", None)
                    _pd_split.pop("_ird_order_placed_at", None)
                    _pd_split.pop("_ird_open_order_items", None)
                    _pd_split["_ird_fresh_order"] = True
                    # The post-process must not close a second order on the way
                    # back out; this turn's split is already done.
                    _pd_split["_ird_split_done_this_turn"] = True
                    logger.info(
                        "[SAROVAR_IRD] new order decided BEFORE the reply - order %s "
                        "closed, clean cart, agent will see an unplaced order; "
                        "closed_items WRITTEN=%r",
                        _leaving_ref or "(unplaced)",
                        _pd_split.get("_ird_closed_items"),
                    )
        except Exception as _split_exc:
            logger.warning("[SAROVAR_IRD] early split skipped (non-fatal): %r", _split_exc)

        _food_order_status: dict[str, Any] = {}
        try:
            _pd_food = context.pending_data if isinstance(context.pending_data, dict) else {}
            _placed_ref = str(_pd_food.get("_ird_order_ticket_ref") or "").strip()
            _placed_at = str(_pd_food.get("_ird_order_placed_at") or "").strip()
            _placed_rows = [
                _r for _r in (_pd_food.get("_ird_placed_orders") or [])
                if isinstance(_r, dict)
            ]
            if _placed_ref or _placed_rows:
                # ── EVERY ORDER, NOT JUST "AN ORDER EXISTS" ──────────────────
                # This used to hand over a single ticket ref, a timestamp and a
                # flat list of dishes with no way to tell which order any dish
                # belonged to — and because the ledger it read was never actually
                # written, the dish list was always empty. So the model knew food
                # had been sent and nothing about what or when.
                #
                # A conversation can now hold several orders, and the model has to
                # answer "what did I order?", "cancel my second order" and "is my
                # food coming?" against them. Each order is therefore listed on its
                # own, numbered the way the guest sees it, and marked with whether
                # it can still be changed — which is the hotel's decision, computed
                # here, not something the model should infer from a timestamp.
                _orders: list[dict[str, Any]] = []
                for _row in _placed_rows:
                    _ref_row = str(_row.get("ticket_ref") or "").strip()
                    _still_editable = not self._ird_edit_window_expired(
                        category="in-room dining",
                        created_at=str(_row.get("placed_at") or _placed_at or ""),
                    )
                    _orders.append({
                        "order_no": int(_row.get("n") or 0),
                        "ticket_ref": _ref_row,
                        "placed_at": str(_row.get("placed_at") or ""),
                        "items": [
                            f"{int(_it.get('qty') or 1)} x "
                            f"{str(_it.get('name') or _it.get('item') or '').strip()}"
                            for _it in (_row.get("items") or [])
                            if isinstance(_it, dict)
                            and str(_it.get("name") or _it.get("item") or "").strip()
                        ][:20],
                        "total": _row.get("total"),
                        "is_the_order_being_added_to": bool(
                            _ref_row and _ref_row == _placed_ref
                        ),
                        "can_still_be_changed": bool(_still_editable),
                    })
                _food_order_status = {
                    "already_sent_to_the_kitchen": True,
                    "orders": _orders,
                    "open_order_count": len(_orders),
                    # Kept so anything still reading the old shape keeps working.
                    "ticket_ref": _placed_ref,
                    "placed_at": _placed_at,
                    # WHAT IS NOT YET PLACED. Without this the agent could see the
                    # orders already with the kitchen but nothing of the one being
                    # built, so a new order in progress looked like an amendment to
                    # the last placed one - and got announced as "added to your
                    # first order" moments after the code had split it off.
                    "not_yet_placed": {
                        # ONLY WHAT THE KITCHEN DOES NOT ALREADY HAVE. The cart
                        # survives placement on purpose (see _ird_unplaced_cart_lines),
                        # so listing it raw described food already on its way as food
                        # nobody had sent - and the model asked the guest to place
                        # their own placed order.
                        "items": [
                            f"{int(_l.get('qty') or 1)} x {str(_l.get('item') or '').strip()}"
                            for _l in self._ird_unplaced_cart_lines(_pd_food)
                        ][:20],
                        # ALREADY DECIDED - do not re-litigate it in the reply.
                        # True: the guest asked for a fresh order, so this becomes
                        # its own ticket. False: they are adding to the order named
                        # in belongs_to_order_no, which is still open.
                        "is_a_new_separate_order": bool(
                            _pd_food.get("_ird_fresh_order")
                        ),
                        "belongs_to_order_no": (
                            None if _pd_food.get("_ird_fresh_order")
                            else next(
                                (
                                    _o.get("order_no") for _o in _orders
                                    if _o.get("is_the_order_being_added_to")
                                ),
                                None,
                            )
                        ),
                    },
                    "note": (
                        "THE ORDERS IN `orders` BELOW are already with the kitchen and "
                        "being prepared - and only those. Do not ask the guest to confirm "
                        "or place THOSE again; say they are on their way. If they change "
                        "one of them, that is an amendment to that existing order. "
                        "Anything in `not_yet_placed` is the OPPOSITE: it is NOT with the "
                        "kitchen, nobody is cooking it, and it still has to be confirmed "
                        "and placed like any other order. Which of the two it is has "
                        "ALREADY been decided for you: if `is_a_new_separate_order` is "
                        "true it becomes its own order with its own ticket and its own "
                        "total; if it is false it is being added to order "
                        "`belongs_to_order_no` above and must go onto THAT order, never "
                        "announced as a second one. Do not reach your own conclusion "
                        "about which - the guest's words were already read and judged. "
                        "When the guest says to place it, PLACE IT; do not answer by "
                        "listing it back and asking again. "
                        "When there is more than one order, refer to them by their "
                        "order_no ('your second order'), never merge them in what you "
                        "say, and give each its own total - a combined total charges "
                        "the guest for food they have already been served. "
                        "Work out WHICH order the guest means from `orders` above - "
                        "each carries its order_no, its own items, its own total and "
                        "whether it can still be changed. `not_yet_placed` is the order "
                        "they are building right now and is NOT with the kitchen yet; it "
                        "is a separate thing from every order in that list, and it is the "
                        "one that still needs placing. Never present the two added "
                        "together. "
                        "ALL OF THAT APPLIES ONLY TO THE ORDERS LISTED HERE. Food that "
                        "is not in this list has NOT been placed and nobody is cooking "
                        "it: it still has to be confirmed and placed in the normal way, "
                        "raising it with the kitchen like any first order. Never tell a "
                        "guest that food is on its way, being prepared, or has been added "
                        "as another order unless you have actually placed it on this turn "
                        "or it already appears above."
                    ),
                }
        except Exception:
            _food_order_status = {}  # best-effort: silence here just restores the old behaviour

        # ── WHAT STATE IS EACH ORDER IN? ─────────────────────────────────────
        # One cart and many orders, each carrying its own state and the one thing
        # the bot may say about it: empty / ongoing / placed / processing /
        # delivered. Everything confusing this agent has done about orders came
        # from "is this placed or not?" being INFERRED from a cart that survives
        # placement, a timestamp and a ticket ref that could disagree. Answered
        # once here instead. `ongoing` is the only state that may ever be offered
        # for placing. See services/ird_order_lifecycle.py.
        _lifecycle: dict[str, Any] = {}
        _order_book: dict[str, Any] = {}
        if self._is_sarovar_ird(context, service_id):
            _window = int(getattr(settings, "ird_edit_window_minutes", 5) or 0)
            # ── THE ORDER BOOK, WHEN IT IS SWITCHED ON ───────────────────────
            # One source at a time, deliberately. Showing the model both the book
            # and the pending_data lifecycle would hand it two accounts of the
            # same order to reconcile - which is the exact bug the book exists to
            # remove. With the flag off this branch does nothing at all and the
            # old lifecycle is built exactly as before. See irdcartbook.md.
            if bool(getattr(settings, "ird_order_book_enabled", False)):
                try:
                    from services import ird_cart_book as _book
                    from services import ird_cart_store as _store

                    _row = await _store.load(
                        hotel_code=str(getattr(context, "hotel_code", "") or ""),
                        session_id=str(getattr(context, "session_id", "") or ""),
                        guest={
                            "room_number": str(
                                (context.pending_data or {}).get("room_number") or ""),
                            "conversation_id": str(
                                getattr(context, "conversation_id", "") or ""),
                        },
                    )
                    if _row:
                        _slots = _book.refresh(
                            _book.slots_from_row(_row),
                            now=_now_svc,
                            window_minutes=_window,
                            ticket_status_by_ref=await self._ird_ticket_statuses(_row),
                        )
                        _order_book = _book.for_the_model(
                            _slots, now=_now_svc, window_minutes=_window)
                        # ── EVERY TURN, NOT JUST WRITE TURNS ─────────────────
                        # The ~100 places that still read _ird_cart and its
                        # siblings only saw the book on turns where the model
                        # emitted items. A placement turn emits none - the guest
                        # just said "place it" - so the confirm gate read an empty
                        # cart, decided the guest had never seen a summary and
                        # refused to place, while the bot told them it WAS placed.
                        # Projecting on every turn keeps those fields a true view
                        # of the book instead of a stale copy.
                        _book.project_to_pending_data(_slots, context.pending_data)
                        logger.info("[IRD_BOOK] %d live order(s) in the book, "
                                    "%d line(s) awaiting placement",
                                    len(_order_book.get("orders") or []),
                                    len(context.pending_data.get("_ird_cart") or []))
                except Exception as _bk_exc:
                    # Fails open to the old path: a book we cannot read must not
                    # cost the guest their order.
                    logger.warning("[IRD_BOOK] unreadable, falling back to "
                                   "pending_data (non-fatal): %r", _bk_exc)
                    _order_book = {}
            if not _order_book:
                try:
                    from services.ird_order_lifecycle import build_lifecycle

                    _lifecycle = await build_lifecycle(
                        context.pending_data, window_minutes=_window)
                except Exception as _lc_exc:
                    logger.warning("[IRD_STATE] lifecycle skipped (non-fatal): %r", _lc_exc)

        # ── ONE ACCOUNT OF THE ORDER, NOT FIVE ───────────────────────────────
        # The book was added beside food_order_status, current_confirmed_order,
        # order_lifecycle and food_profile - so the model was handed the same
        # order described five ways and asked to reconcile them, which is the
        # exact problem the book exists to remove. It stitched them together and
        # told a guest "Filter Coffee is already on its way" when the book held
        # only burgers. When the book is on it is the ONLY description of the
        # current order in the payload.
        if _order_book:
            _food_order_status = {}
        _turn_response_language = self._response_language_preference(
            user_message=str(
                getattr(context, "_turn_guest_language_message", "") or user_message or ""
            ),
            history_rows=history_bundle.get("history_last_10", []),
            last_assistant_message=str(history_bundle.get("last_assistant_message") or ""),
            router_language=getattr(context, "_turn_response_language", None),
        )
        _previous_response_language = self._last_assistant_response_language(context)
        _language_switch = bool(
            _previous_response_language
            and _turn_response_language
            and str(_previous_response_language.get("language") or "").strip().casefold()
            != str(_turn_response_language.get("language") or "").strip().casefold()
        )
        (
            _answer_history,
            _answer_last_assistant,
            _language_switch_history_suppressed,
        ) = self._history_for_explicit_language_switch(
            history_rows=history_bundle.get("history_last_10", []),
            last_assistant_message=str(history_bundle.get("last_assistant_message") or ""),
            current_user_message=str(user_message or ""),
            language_switch=_language_switch,
        )
        payload = {
            "user_message": str(user_message or ""),
            "food_order_status": _food_order_status,
            "current_date": _now_svc.strftime("%Y-%m-%d"),
            "current_day": _now_svc.strftime("%A"),
            "current_time": _now_svc.strftime("%H:%M"),
            "current_timezone": _svc_tz_name,
            # WHICH DISHES CAN BE ORDERED RIGHT NOW, resolved per dish in code from
            # config/ird_serving_windows.json. The model does no timing work: it
            # reads one short list of NAMES.
            #
            # This replaces a list of three SECTION headings that the model had to
            # map 152 dishes onto by reading the 41k-char menu itself - which lost
            # Idli's breakfast window (it sits in a sub-section) and reported the
            # whole of INDIAN SELECTION shut because of a kebab-only note.
            #
            # The block carries its own "this is TIMING ONLY, it is never the menu"
            # rule, because a short list of names is easy to mistake for the whole
            # menu - which is how a Chicken Burger got refused at 10:34 on the
            # theory that the kitchen was shut.
            **self._build_menu_availability_payload(
                hotel_code=str(getattr(context, "hotel_code", "") or ""),
                menu_text=extracted_knowledge,
                now_local=_now_svc,
            ),
            **({"order_lifecycle": _lifecycle} if _lifecycle else {}),
            **({"order_book": _order_book} if _order_book else {}),
            "is_first_turn": is_first_service_turn,
            "current_phase_id": selected_phase_id,
            "service_phase_id": self._normalize_identifier(service.get("phase_id")),
            "pending_action": str(context.pending_action or ""),
            "pending_data_collected": collected_labels,
            "pending_data_raw": self._sanitize_json({
                _k: _v for _k, _v in pending_pub.items()
                if str(_k).strip().lower() not in _internal_routing_keys
            }),
            "known_context": self._sanitize_json(_known_ctx),
            "_internal_booking_reference": self._sanitize_json(_internal_booking_ref),
            "history": _answer_history,
            "history_last_10": _answer_history,
            "response_language": self._sanitize_json(_turn_response_language),
            # Compatibility alias for prompt rows deployed before the turn-level
            # response_language contract existed.
            "response_language_preference": self._sanitize_json(_turn_response_language),
            "previous_response_language": self._sanitize_json(_previous_response_language),
            "language_switch": _language_switch,
            "language_switch_history_suppressed": _language_switch_history_suppressed,
            "full_history_context": history_bundle.get("full_history_context", {}),
            "last_user_message": str(history_bundle.get("last_user_message") or ""),
            "last_assistant_message": _answer_last_assistant,
            "memory_summary": self._summary_within_budget(memory_snapshot.get("summary"), 1200),
            "memory_facts": self._sanitize_json(_mem_facts),
            "faq_candidates": self._sanitize_json(faq_candidates),
            "confirmation_phrase": confirmation_phrase,
            "available_departments": _available_departments,
            "service_ticketing_mode": _svc_ticketing_mode,
            "service_form_trigger_field": self._sanitize_json({
                "id": _svc_trigger_field_id,
                "label": _svc_trigger_field_label,
                "description": str(_svc_trigger_field.get("description") or "").strip(),
            }),
            "service_form_fields": self._sanitize_json(_svc_form_fields_payload),
            "service_form_instructions": str(_svc_form_config.get("pre_form_instructions") or "").strip(),
            "property_scope": self._sanitize_json(service_property_scope_payload),
        }
        # SAROVAR IRD: feed the server-validated running order back as the
        # AUTHORITATIVE cart so the agent recaps/summarises from it (never re-derives
        # the order from chat, and never re-adds a dropped/off-menu item).
        if self._is_sarovar_ird(context, service_id):
            _pd_cart = context.pending_data if isinstance(context.pending_data, dict) else {}
            # DELIBERATELY THE WHOLE CART, placed lines included. The prompt calls
            # this "the IN-PROGRESS cart not yet placed", which stops being true the
            # moment an order is placed - but narrowing it to the genuinely unplaced
            # lines made the bot DUMBER, and was measured doing so: with the field
            # gone, "also add a filter coffee to that" had nothing to append to, so
            # the model offered a brand-new order and the coffee never reached the
            # kitchen ticket. This list is also the whitelist that keeps a declined
            # off-menu item from reappearing ("NEVER include an item that is not in
            # current_confirmed_order"), and an empty whitelist guards nothing.
            # Whether any of it still needs placing is answered separately and
            # unambiguously by food_order_status.not_yet_placed.
            _ird_cart = _pd_cart.get("_ird_cart")
            # Under the book this is the same lines a third time, and it is the
            # field the model was already most likely to misread as an unplaced
            # order. The book carries every line with its real status.
            if _ird_cart and not _order_book:
                payload["current_confirmed_order"] = self._sanitize_json(_ird_cart)
                # Keeping placed lines in the list above costs one thing: on its own
                # it reads as an order still being built, and the model asked a guest
                # to place food that was already cooking. So when nothing in it is
                # actually awaiting placement, say so here rather than by deleting
                # the list.
                if not self._ird_unplaced_cart_lines(_pd_cart):
                    payload["current_confirmed_order_status"] = (
                        "EVERY line in current_confirmed_order is ALREADY WITH THE "
                        "KITCHEN and being prepared. NOTHING in it is awaiting "
                        "placement, so NEVER ask the guest to place, confirm or send "
                        "it - say it is on its way. It is listed only so you can name "
                        "the dishes correctly and so anything the guest adds next "
                        "joins this same order."
                    )
            # Guest food profile from durable memory: past placed orders (for correct
            # recall + personalised recommendations) and any declared allergies (for
            # warnings). This is the single authoritative source for "what did I order".
            _mf = (memory_snapshot or {}).get("facts") or {}
            _allergies = _mf.get("allergies") or _mf.get("food_allergies") or []
            _profile = {
                "past_orders": _mf.get("order_history") or [],
                "latest_order": _mf.get("latest_order") or {},
                "allergies": _allergies,
            }
            # ── DO NOT DESCRIBE THIS STAY'S ORDERS TWICE ─────────────────────
            # Verified on a live turn: the book said the burgers were `placed`
            # with "about 5 minutes left to change it", while food_profile filed
            # the SAME order - 30 seconds old, still open - under `past_orders`
            # and `latest_order`. The model was being told its live order was
            # history. Anything the book already holds is dropped here; genuinely
            # older orders stay, because past_orders is what drives "last time you
            # enjoyed..." recommendations.
            if _order_book:
                _book_refs = {
                    str(o.get("ticket_ref") or "").strip()
                    for o in (_order_book.get("orders") or [])
                    if str(o.get("ticket_ref") or "").strip()
                }
                _book_dishes = {
                    str(i.get("item", "")).strip().lower()
                    for o in (_order_book.get("orders") or [])
                    for i in (o.get("items") or [])
                }

                def _is_this_stay(_o: Any) -> bool:
                    if not isinstance(_o, dict):
                        return False
                    if str(_o.get("ticket_ref") or "").strip() in _book_refs:
                        return True
                    # An order the book holds but which was written before the
                    # ticket came back has no ref to match on - fall back to its
                    # dishes, so it is not described a second time as history.
                    _names = {str(i.get("name") or i.get("item") or "").strip().lower()
                              for i in (_o.get("items") or []) if isinstance(i, dict)}
                    return bool(_names) and _names.issubset(_book_dishes)

                _profile["past_orders"] = [
                    o for o in (_profile["past_orders"] or []) if not _is_this_stay(o)
                ]
                if _is_this_stay(_profile["latest_order"]):
                    _profile["latest_order"] = {}
            if _profile["past_orders"] or _profile["latest_order"] or _profile["allergies"]:
                payload["food_profile"] = self._sanitize_json(_profile)
        # Inject booking context as a SEPARATE system message AFTER the static
        # service system prompt. This keeps the cache prefix [system_prompt]
        # stable across turns within the same property session — OpenAI's
        # automatic prompt cache hits the prefix regardless of whether the
        # booking block is present, and prefill on 7-9k input tokens drops
        # ~50% on cache hits.
        _booking_block = self._build_booking_context_prompt_block(context)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]
        if _booking_block:
            messages.append({"role": "system", "content": _booking_block})
        messages.append(
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        )
        # ZONE-ONLY answer-shape rule, second from last for the same recency
        # reason as the language rule. The router sends some questions here and
        # some to the book answerer, so a rule that must hold for EVERY reply has
        # to sit in both lanes — the book answerer gets the identical directive
        # in llm_orchestration_grounding_mixin. Admin-managed hotels switch it
        # on from the Pre-booking tab, worded by their DB row.
        _no_offer_rule = await self._reply_style_directive(
            context, "no_trailing_offer", legacy_hotel="zone",
            legacy_text=self._no_trailing_offer_directive,
        )
        if _no_offer_rule:
            messages.append({"role": "system", "content": _no_offer_rule})
        _emoji_rule = await self._admin_emoji_directive(context)
        if _emoji_rule:
            messages.append({"role": "system", "content": _emoji_rule})
        # Ordering rules for the food agent, placed late for the same recency
        # reason: buried in the service prompt they lost to the surrounding text.
        if self._is_sarovar_ird(context, service_id):
            messages.append(
                {"role": "system", "content": self._ird_order_flow_directive()}
            )
            # ── THE GUEST'S ACTUAL ORDER, SAID LAST ───────────────────────────
            # It was already in the payload, but the payload is one 35k user
            # message in the middle of a 100k system prompt, and the book is 2%
            # of it. Buried there it lost to prose the model had read tens of
            # thousands of characters of - it was told "can_be_changed: true,
            # about 1 minute left" and still answered "already placed, this will
            # be a new order". The same recency problem the language rule and the
            # ordering directive were both moved late to solve.
            if _order_book and _order_book.get("orders"):
                messages.append({
                    "role": "system",
                    "content": (
                        "=== THE GUEST'S ORDERS RIGHT NOW (THE ONLY RECORD) ==="
                        + "\n" + json.dumps(_order_book, ensure_ascii=False,
                                            default=str)
                        + "\nEverything above about orders is history or general "
                        "guidance. THIS is the state. If any earlier instruction "
                        "disagrees with an order's `status` or `can_be_changed` "
                        "here, THIS WINS."
                    ),
                })
        # The service lane had NO scope rule at all — it lived in the book
        # answerer only. During a stay this lane answers most things, so a joke or
        # a sum asked mid-order was simply answered. Sent only on the turns the
        # router flagged, so an ordinary turn pays nothing.
        if bool(getattr(context, "_router_off_topic", False)):
            messages.append(
                {"role": "system", "content": self._router_says_off_topic_directive()}
            )
        # ── THE SCOPE + SAFETY GUARDRAIL, WHICH WAS NEVER ACTUALLY SENT ──────
        # `_out_of_scope_guardrail_directive()` has existed for a long time and
        # was referenced only by a comment - defined, never injected. That is why
        # jokes, maths and "capital of france" sailed through, and why a request
        # for a knife could raise "Deliver a knife to the guest in room 1400" one
        # turn and be refused the next: nothing in the system had an opinion, so
        # the base model improvised a different one each time. Placed second-last,
        # immediately before the language rule, which must stay the very last.
        messages.append(
            {"role": "system",
             "content": self._out_of_scope_guardrail_directive(
                 pre_arrival=str(selected_phase_id or "").strip().lower()
                 .replace(" ", "_").replace("-", "_")
                 in {"pre_booking", "pre_checkin"})}
        )
        # Language rule MUST stay the VERY LAST message for reliable language mirroring.
        messages.append(
            {"role": "system", "content": self._reply_language_directive()}
        )
        # ── SEE EXACTLY WHAT THE MODEL IS SENT ───────────────────────────────
        # Set IRD_DUMP_PROMPT=1 to write the complete message list and payload of
        # every dining turn to overnight/_chat/last_prompt.json. The DB log tables
        # do not capture this path at all, so when the bot contradicts the data it
        # was given there was no way to see what it was actually given - which is
        # the only question worth asking at that point.
        if os.environ.get("IRD_DUMP_PROMPT") and self._is_sarovar_ird(context, service_id):
            try:
                from pathlib import Path as _P
                _dump = _P(__file__).resolve().parent.parent / "overnight" / "_chat"
                _dump.mkdir(parents=True, exist_ok=True)
                (_dump / "last_prompt.json").write_text(
                    json.dumps({"user_message": str(user_message or ""),
                                "messages": messages, "payload": payload},
                               indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
            except Exception as _d_exc:
                logger.warning("[IRD_DUMP] could not write the prompt dump: %r", _d_exc)
        _prompt_source = "generated" if str(service.get("generated_system_prompt") or "").strip() else "template"
        _log_llm_call(
            label=f"SERVICE AGENT [{service_id}] (prompt:{_prompt_source})",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt=system_prompt,
            payload=payload,
        )
        service_agent_max_tokens = max(
            220,
            int(getattr(settings, "llm_service_agent_max_tokens", 900) or 900),
        )
        raw = await self._chat_with_json(
            messages=messages,
            model=model,
            temperature=0.0,
            max_tokens=service_agent_max_tokens,
            trace_context={
                "responder_type": "service",
                "agent": "service_agent",
                "session_id": str(getattr(context, "session_id", "")),
                "service_id": service_id,
                "service_name": service_name,
                "selected_phase_id": selected_phase_id,
                "selected_phase_name": selected_phase_name,
            },
        )
        _log_llm_call(
            label=f"SERVICE AGENT [{service_id}] RESPONSE",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt="",
            payload={},
            response=raw,
        )
        decision = self._safe_parse_decision(raw, user_message=user_message, service_id=service_id)
        decision.target_service_id = service_id or decision.target_service_id
        model_source = str(decision.metadata.get("source") or "").strip()
        if model_source and model_source != "service_agent":
            decision.metadata["service_agent_model_source"] = model_source
        decision.metadata["source"] = "service_agent"
        decision.metadata["service_agent_id"] = service_id
        decision.metadata["is_first_service_turn"] = is_first_service_turn
        decision = self._normalize_faq_resolution_metadata(
            decision,
            faq_candidates=faq_candidates,
        )

        # Settle the shape of a declared food order BEFORE anything reads it — the
        # confirmation gate below and the order-state pass at the end of this method
        # both do, and a JSON-string order was invisible to both.
        if self._is_sarovar_ird(context, service_id):
            self._ird_normalize_order_items(decision)

        if _svc_ticketing_mode == "form":
            # Pre-booking-tab hotels keep web addresses and emails whole.
            from services import prebooking_settings_service as _pbs_form_text

            decision.response_text = strip_form_confirmation_instructions(
                decision.response_text,
                keep_web_addresses=_pbs_form_text.is_admin_managed(
                    str(getattr(context, "hotel_code", "") or "").strip().lower()
                ),
            )
            decision.pending_data_updates, _trigger_alias_key, _ = canonicalize_trigger_pending_data(
                decision.pending_data_updates,
                _svc_trigger_field_id,
                _svc_trigger_field_label,
            )
            _, _trigger_value = resolve_trigger_field_value(
                decision.pending_data_updates,
                _svc_trigger_field_id,
                _svc_trigger_field_label,
            )
            decision.missing_fields = normalize_trigger_missing_fields(
                decision.missing_fields,
                _svc_trigger_field_id,
                _svc_trigger_field_label,
                trigger_value_present=bool(str(_trigger_value or "").strip()),
            )

            # ── FORM-MODE SAFETY GUARDS (no hardcoded triggering) ──
            # The LLM is the sole authority on whether to render the form. It
            # reads the admin-defined trigger description from the prompt and
            # decides based on conversation context. We do NOT force the form
            # on based on heuristics like "trigger value present" — that path
            # used to fire on info questions where the LLM merely mentioned a
            # room name in pending_data_updates.
            #
            # The two guards below only strip explicit anti-patterns (confirm_*
            # and create_ticket) that the form-mode contract forbids — they
            # never CAUSE the form to render. If the LLM said respond_only,
            # we keep respond_only.

            _form_trigger_present = bool(str(_trigger_value or "").strip())
            _pa_lower = str(decision.pending_action or "").strip().lower()

            # Anti-pattern 1: confirm_* in form mode. Form submission is the
            # confirmation — no chat-style "yes confirm" step. If the LLM set
            # confirm_*, downgrade it: keep collect_form_details only if the
            # LLM also signaled form intent via a trigger value, otherwise
            # clear pending_action and let the LLM's action stand.
            if _pa_lower.startswith("confirm_"):
                decision.pending_action = (
                    "collect_form_details" if _form_trigger_present else None
                )
                decision.metadata["form_mode_stripped_confirm_pending_action"] = _pa_lower

            # Anti-pattern 2: create_ticket action. Form submission creates
            # the ticket — the LLM must not. Downgrade to collect_info only
            # if the LLM intended the form (trigger present), else respond_only.
            if str(decision.action or "").strip().lower() == "create_ticket":
                decision.action = "collect_info" if _form_trigger_present else "respond_only"
                if hasattr(decision, "ticket") and hasattr(decision.ticket, "ready_to_create"):
                    decision.ticket.ready_to_create = False
                decision.metadata["form_mode_stripped_create_ticket"] = True

            if (
                self._is_room_booking_service(
                    service_id=service_id,
                    service_name=service_name,
                    description=description,
                    trigger_field_id=_svc_trigger_field_id,
                )
                and self._normalize_identifier(_svc_trigger_field_id) == "room_type"
            ):
                decision = self._enforce_explicit_room_type_selection(
                    decision=decision,
                    user_message=user_message,
                    history_bundle=history_bundle,
                    extracted_knowledge=str(extracted_knowledge or ""),
                    trigger_field_id=_svc_trigger_field_id,
                    trigger_field_label=_svc_trigger_field_label,
                )
            # Backstop only fires in pre_booking — the prompt is the primary,
            # judgment-based driver; this catches the clearest conversion blockers
            # (price/size/availability) if the LLM forgets to offer the callback.
            if self._normalize_phase_identifier(selected_phase_id) == "pre_booking":
                decision = self._normalize_room_booking_missing_detail_handoff(
                    decision=decision,
                    user_message=user_message,
                    service_id=service_id,
                    service_name=service_name,
                    description=description,
                    trigger_field_id=_svc_trigger_field_id,
                )
            decision = self._normalize_support_form_open_decision(
                decision=decision,
                service_id=service_id,
                service_name=service_name,
                description=description,
                known_context=_known_ctx,
                user_message=user_message,
            )

        # Context-switch marker for immediate same-turn main-orchestrator re-route.
        if bool((decision.metadata or {}).get("context_switched")):
            decision.metadata["context_switched"] = True

        # Persist any new guest facts the service LLM discovered this turn.
        new_facts = raw.get("new_facts_to_remember") if isinstance(raw, dict) else None
        if isinstance(new_facts, dict) and new_facts:
            from services.conversation_memory_service import conversation_memory_service as _cms
            import datetime as _dt
            memory = _cms.ensure_memory(context)
            facts = memory.setdefault("facts", {})
            fact_history = memory.setdefault("fact_history", [])
            for key, value in new_facts.items():
                key_clean = str(key or "").strip().lower().replace(" ", "_")
                if not key_clean or value is None:
                    continue
                _cms._set_fact(
                    facts=facts,
                    fact_history=fact_history,
                    key=key_clean,
                    value=value,
                    source_message=str(user_message or "")[:300],
                    change_type="set",
                )
            memory["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
            decision.metadata["new_facts_saved"] = list(new_facts.keys())

        user_compact = re.sub(r"\s+", " ", str(user_message or "").strip().lower())
        confirmation_compact = re.sub(r"\s+", " ", confirmation_phrase.lower())
        awaiting_confirmation = str(context.pending_action or "").strip().lower().startswith("confirm_")
        strict_confirmation_enforced = bool(getattr(settings, "chat_require_strict_confirmation_phrase", False))

        # ── THE MODEL ASKED AND ANSWERED IN THE SAME BREATH ──────────────────
        # A decision that carries BOTH ready_to_create=true ("place it now") and a
        # pending_action ("I am waiting for the guest") contradicts itself, and the
        # two halves were being read by different code: the order-state pass
        # believed the ticket was going out and told the guest "Done — on its way",
        # while the ticket gate read the pending_action and raised nothing. The
        # guest was thanked for an order that never reached the kitchen.
        #
        # Which half is true depends on where we already are in the conversation,
        # so that is what decides it — not the wording of the reply:
        #   • we ALREADY asked the guest to confirm and they answered this turn →
        #     the model judged the answer good enough to place the order, so the
        #     leftover pending_action is a hedge; drop it and let the order go.
        #   • we had NOT asked yet → the model is still asking, so it is not ready
        #     however it filled the ticket fields in.
        _tkt = getattr(decision, "ticket", None)
        if _tkt is not None and bool(getattr(_tkt, "ready_to_create", False)) and str(
            decision.pending_action or ""
        ).strip():
            # "We had NOT asked yet" is decided from pending_action, and in-room
            # dining never leaves one: its checkout summary is driven by the cart,
            # and the model ends that turn with pending_action empty. So the turn
            # where the guest actually says "yes place it" ALWAYS looks like we had
            # not asked — and this branch threw the order away on roughly one run
            # in four, telling the guest "Shall I place this order?" all over again
            # while the kitchen got nothing. A validated cart is the server's own
            # record that the order was built, priced and put to the guest, so it
            # answers the same question `awaiting_confirmation` is asking.
            if awaiting_confirmation or self._ird_guest_already_saw_the_order(
                context, service_id
            ):
                logger.info(
                    "[CONFIRM] guest answered our confirmation ask — dropping the "
                    "model's leftover pending_action %r so the ticket can be raised",
                    str(decision.pending_action or ""),
                )
                decision.pending_action = ""
                decision.metadata["confirmation_hedge_cleared"] = True
            else:
                logger.info(
                    "[CONFIRM] model set ready_to_create while still asking "
                    "(pending_action=%r) — treating it as NOT ready",
                    str(decision.pending_action or ""),
                )
                _tkt.ready_to_create = False
                decision.metadata["premature_ready_downgraded"] = True

        # Deterministic confirmation override — if the user said the confirmation phrase
        # while we were in a confirm_* pending state (or last bot message asked for it),
        # force ticket creation regardless of what the LLM returned.
        last_bot_asked_confirmation = False
        if requires_confirmation_step:
            for _msg in reversed(context.messages):
                _role = str(getattr(_msg, "role", "") or "").lower()
                if "assistant" in _role:
                    _content = str(getattr(_msg, "content", "") or "").lower()
                    # Match if last bot message asked for confirmation (contains "confirm" broadly)
                    if "confirm" in _content or confirmation_compact in _content:
                        last_bot_asked_confirmation = True
                    break

        if requires_confirmation_step and (awaiting_confirmation or last_bot_asked_confirmation) and user_compact == confirmation_compact:
            pending_pub = self._coerce_public_pending(context.pending_data)
            # Build auto_issue from ALL collected pending data — no hardcoded slot names
            skip_keys = {"service_id", "service_name"}
            # Same rule as the guest-facing recap below: a structured slot (an order
            # item list, say) is not a detail line. Left in, the ticket's issue read
            # as raw Python for staff too. The owning service rebuilds the issue from
            # its own validated data anyway.
            detail_parts = [
                f"{k.replace('_', ' ')}: {v}"
                for k, v in pending_pub.items()
                if k not in skip_keys
                and not isinstance(v, (list, dict, tuple, set))
                and str(v or "").strip()
            ]
            auto_issue = service_name
            if detail_parts:
                auto_issue = f"{service_name} — {', '.join(detail_parts)}"
            decision.action = "create_ticket"
            decision.pending_action = None
            decision.missing_fields = []
            decision.ticket.required = True
            decision.ticket.ready_to_create = True
            if not str(decision.ticket.issue or "").strip():
                decision.ticket.issue = auto_issue
            if not str(decision.ticket.category or "").strip():
                decision.ticket.category = "request"
            if not str(decision.ticket.sub_category or "").strip():
                decision.ticket.sub_category = self._normalize_identifier(service_id)
            # Keep LLM response_text only if it looks like a genuine booking confirmation.
            # Normalize Unicode apostrophes (LLM often returns \u2019 instead of ASCII ')
            resp_lower = str(decision.response_text or "").strip().lower()
            resp_lower = resp_lower.replace("\u2019", "'").replace("\u2018", "'").replace("\u2014", " ")
            bad_signals = (
                "table booking", "table reservation",
                "can't confirm", "cant confirm", "cannot confirm",
                "unable to confirm", "i'm unable", "im unable",
                "i can help with", "feel free to ask",
                "hotel enquiries", "sightseeing",
                "not available", "can't help", "cant help",
            )
            # A genuine confirmation should mention "confirm" or "booking" or "reservation"
            good_signals = ("confirm", "booking confirmed", "reservation confirmed", "booking has been")
            looks_genuine = any(sig in resp_lower for sig in good_signals)
            if not resp_lower or not looks_genuine or any(sig in resp_lower for sig in bad_signals):
                # Clean, generic fallback confirmation. The previous code
                # referenced room_type/checkin/checkout/guests, which are never
                # defined in this scope — a latent NameError that crashed the
                # confirmation-override path for any text-mode service (e.g.
                # in-room dining) whose reply didn't read as a "genuine" booking
                # confirmation. We deliberately do NOT dump the collected slots
                # here: pending_data carries internal keys (guest_id, property
                # scope ids, last_service_id, date ranges…) that must never leak
                # into a guest-facing message. The guest already saw the item /
                # detail summary on the confirmation turn, and the full detail is
                # captured on the ticket.
                decision.response_text = (
                    f"Your {service_name} request has been confirmed! "
                    "Our team will follow up with you shortly."
                )
            decision.metadata["confirmation_override"] = True
        active_flow_signal = bool(str(context.pending_action or "").strip() or str(base_decision.pending_action or "").strip())
        if bool(base_decision.missing_fields):
            active_flow_signal = True

        # Gate: intercept premature ticket creation — force confirmation step first.
        # Fires when the LLM returned create_ticket but the guest never saw a summary
        # and confirmed explicitly (i.e. we haven't been in a confirm_* pending state).
        if (
            requires_confirmation_step
            and not awaiting_confirmation
            and user_compact != confirmation_compact
            and not bool(decision.requires_human_handoff)
            and str(decision.action or "").strip().lower() == "create_ticket"
            and bool(getattr(decision.ticket, "ready_to_create", False))
            # ...unless the guest is looking at a validated in-room-dining order.
            # Then they HAVE seen the summary, this gate's premise is false, and
            # firing it ate their "yes" and left the kitchen with no order.
            and not self._ird_guest_already_saw_the_order(context, service_id)
        ):
            decision.action = "collect_info"
            decision.pending_action = confirmation_pending_action
            decision.ticket.ready_to_create = False
            # Build summary from context pending_data + any new updates the LLM provided
            merged = dict(self._coerce_public_pending(context.pending_data))
            if isinstance(decision.pending_data_updates, dict):
                for k, v in decision.pending_data_updates.items():
                    if str(v or "").strip():
                        merged[k] = v
            # Recap ONLY what the guest actually gave us. NEVER echo internal
            # routing/stay/scope context that gets auto-attached from the
            # checked-in booking (guest ids, property scope, stay dates, etc.) —
            # those are internal-only and must never appear in a guest reply.
            skip_keys = {
                "service_id", "service_name",
                # Active/last-service routing metadata (mirrored onto pending by
                # _coerce_public_pending) — internal only, must never be recapped.
                "service", "last_service_id", "last_service_name",
                "selected_phase_id", "selected_phase", "phase_id",
                "past_property_context", "property_name_map", "last_user_topic",
                "flow", "bot_mode", "ticket_source", "source",
                "message_id", "agent_id", "staff_id", "group_id",
                "user_id", "wa_number", "org_id",
                "guest_id", "guest_name", "guest_phone", "guest_email",
                "active_property_ids", "active_property_names",
                "active_property_id", "active_property_name", "active_property",
                "selected_property_scope_id", "selected_property_scope_ids",
                "selected_property_scope_name",
                "check_in", "check_out",
                "stay_checkin_date", "stay_checkout_date",
                "stay_property_id", "stay_property_name",
                "property_id", "property_ids", "property_name", "property_names",
                "hotel_id", "hotel_code", "hotel_name",
                "entity_id", "organisation_id", "organization_id", "fms_entity_id",
                "session_id", "conversation_id", "conversation_uid",
                "phase", "location", "city",
            }
            # ONLY PLAIN VALUES REACH THE GUEST. A slot holding a list or a dict is
            # internal structure, and printing it dumped raw Python at the guest:
            # a food order recapped as
            #   - Order Items: [{'name': 'Tiramisu', 'qty': 1, 'price': 500, ...}]
            # Structured slots belong to whichever service owns them — it renders
            # them properly (in-room dining builds a real order summary) — so this
            # generic recap skips them rather than guessing at a format.
            detail_parts = [
                f"- {k.replace('_', ' ').title()}: {v}"
                for k, v in merged.items()
                if k not in skip_keys
                and not str(k).startswith("_")
                and not isinstance(v, (list, dict, tuple, set))
                and str(v or "").strip()
            ]
            existing = str(decision.response_text or "").strip()
            confirmation_ask = "Shall I go ahead?"
            if detail_parts:
                summary_block = "\n".join(detail_parts)
                if existing and "confirm" not in existing.lower():
                    decision.response_text = f"{existing}\n\n{summary_block}\n\n{confirmation_ask}"
                elif existing:
                    decision.response_text = f"{existing}\n\n{confirmation_ask}"
                else:
                    decision.response_text = f"{summary_block}\n\n{confirmation_ask}"
            else:
                if existing and "confirm" not in existing.lower():
                    decision.response_text = f"{existing}\n\n{confirmation_ask}"
                else:
                    decision.response_text = existing or confirmation_ask
            decision.metadata["confirmation_gate_forced"] = True

        if (
            requires_confirmation_step
            and active_flow_signal
            and not awaiting_confirmation
            and user_compact != confirmation_compact
            and not bool(decision.requires_human_handoff)
            and not bool(decision.missing_fields)
            and not bool(getattr(decision.ticket, "ready_to_create", False))
            and not str(decision.pending_action or "").strip()
            and not bool(decision.metadata.get("confirmation_override"))
        ):
            _action_lower = str(decision.action or "").strip().lower()
            # collect_info with no missing fields → LLM finished collecting → confirm
            # respond_only → only confirm if the LLM actually collected new field
            #   data this turn (otherwise it's just answering a side question)
            _should_force_confirm = False
            if _action_lower == "collect_info":
                _should_force_confirm = True
            elif _action_lower == "respond_only":
                _new_updates = decision.pending_data_updates if isinstance(decision.pending_data_updates, dict) else {}
                _meaningful = {
                    k: v for k, v in _new_updates.items()
                    if str(v or "").strip() and k not in {"service_id", "service_name"}
                }
                _should_force_confirm = bool(_meaningful)
            if _should_force_confirm:
                decision.pending_action = confirmation_pending_action
                response_lower = str(decision.response_text or "").strip().lower()
                if confirmation_phrase.lower() not in response_lower:
                    confirmation_line = f"Please reply '{confirmation_phrase}' to confirm."
                    if str(decision.response_text or "").strip():
                        decision.response_text = f"{str(decision.response_text).strip()} {confirmation_line}".strip()
                    else:
                        decision.response_text = confirmation_line
                decision.metadata.setdefault("confirmation_flow_forced", True)
                decision.metadata.setdefault("confirmation_pending_action", confirmation_pending_action)

        # SAROVAR IRD "cart": validate the model's declared order against the menu,
        # persist the validated running order, and rebuild the ticket from it so a
        # declined/off-menu item can never reach the order or the kitchen ticket.
        # Uses the runtime-resolved, property-scoped `extracted_knowledge` (the exact
        # menu the agent sees), not the trimmed service dict.
        #
        # RUNS LAST, AFTER THE CONFIRMATION GATES ABOVE. It used to run straight off
        # the model's raw decision, which meant it read ready_to_create BEFORE the
        # gates had their say — it announced "Done — your order is on its way" and
        # then a gate below it quietly turned the same ticket off. Reading the
        # decision only once the gates have finished means the order text and the
        # ticket can no longer disagree about whether the order was placed.
        if self._is_sarovar_ird(context, service_id):
            await self._sarovar_ird_apply_order_state(
                context=context, decision=decision, menu_text=extracted_knowledge,
            )
            # Safety net: correct any non-veg item the model placed under a Vegetarian
            # heading on the menu render (no-op on unexpected structure).
            if decision.response_text:
                decision.response_text = self._sarovar_ird_fix_veg_grouping(
                    decision.response_text, extracted_knowledge
                )

        # Always stamp service_id into pending_data_updates so sticky routing works.
        # This ensures context.pending_data["service_id"] is always set while a
        # service is active, regardless of what the LLM put in pending_data_updates.
        if str(decision.pending_action or "").strip():
            if not isinstance(decision.pending_data_updates, dict):
                decision.pending_data_updates = {}
            decision.pending_data_updates.setdefault("service_id", service_id)

        try:
            from services.flow_logger import log_service_runtime

            missing_fields = []
            for field in list(getattr(decision, "missing_fields", []) or []):
                text = str(field or "").strip()
                if text:
                    missing_fields.append(text)
            log_service_runtime(
                session_id=str(getattr(context, "session_id", "") or ""),
                service_id=service_id,
                service_name=service_name,
                phase_id=str(selected_phase_id or ""),
                prompt_source=_prompt_source,
                extracted_knowledge_chars=len(extracted_knowledge),
                generated_prompt_chars=len(_generated_prompt),
                full_kb_fallback_chars=len(full_kb_text),
                pending_action_before=str(context.pending_action or ""),
                response_action=str(getattr(decision, "action", "") or ""),
                missing_fields=missing_fields,
                ticket_ready_to_create=bool(getattr(decision.ticket, "ready_to_create", False)),
                context_switched=bool((decision.metadata or {}).get("context_switched")),
            )
        except Exception:
            pass

        decision = self._apply_answer_priority_fields(decision)
        return decision

    def _remember_bare_yes_verdict(self, session_id: Any, user_message: str, raw: Any) -> None:
        """Keep this turn's offer-acceptance verdict so the router can read it."""
        try:
            cache = getattr(self, "_bare_yes_verdicts", None)
            if not isinstance(cache, dict):
                cache = {}
                self._bare_yes_verdicts = cache
            cache[(str(session_id or ""), str(user_message or "").strip())] = (
                raw if isinstance(raw, dict) else {}
            )
            if len(cache) > 200:
                for key in list(cache)[:100]:
                    cache.pop(key, None)
        except Exception:
            pass

    def _bare_yes_accepted_nothing(self, *, session_id: Any, user_message: str) -> bool:
        """Did the judge say this reply is pure agreement with NOTHING behind it?

        True only when it ran THIS turn and answered both halves. An unrun or
        failed judge returns False, so nothing is ever blocked on a missing answer.
        """
        cache = getattr(self, "_bare_yes_verdicts", None)
        if not isinstance(cache, dict):
            return False
        verdict = cache.get((str(session_id or ""), str(user_message or "").strip()))
        if not isinstance(verdict, dict) or not bool(verdict.get("is_bare_agreement")):
            return False
        return str(verdict.get("offer_kind") or "").strip().lower() in {"", "none"}

    def _bare_yes_is_agreement(self, *, session_id: Any, user_message: str) -> bool:
        """Did the judge, THIS turn, read the reply as pure agreement ("yes", "sure")?"""
        cache = getattr(self, "_bare_yes_verdicts", None)
        if not isinstance(cache, dict):
            return False
        verdict = cache.get((str(session_id or ""), str(user_message or "").strip()))
        return isinstance(verdict, dict) and bool(verdict.get("is_bare_agreement"))

    @staticmethod
    def _summary_within_budget(summary: Any, limit: int) -> str:
        """The memory summary trimmed to `limit` chars WITHOUT losing the newest hotel.

        HOTELS DISCUSSED is written OLDEST FIRST, so a plain `[:limit]` cut throws
        away the property the guest is on right now and keeps the ones they left.
        Real failure: a cake asked for on a Jaipur turn was confirmed "...at Zone by
        The Park, Chennai ORR". The service agent's copy of the summary stopped at
        1200 characters, mid-way through the second of four hotels — the word
        "Jaipur" (character 1877) was not in it at all, so Chennai was the only
        hotel it could see, and the ticket and the form prefill both followed it.

        Header lines are always kept; hotel entries are dropped from the OLDEST end
        until the rest fits, so the active property is the one that always survives.
        """
        text = str(summary or "")
        if limit <= 0:
            return ""  # same as the [:limit] this replaced - never the whole text
        if len(text) <= limit:
            return text
        lines = text.splitlines()
        entry_idx = [i for i, line in enumerate(lines) if line.lstrip().startswith("- ")]
        if not entry_idx:
            # Not the list format — keep the END, which is the most recent part.
            return text[-limit:]
        head = lines[: entry_idx[0]]
        tail = lines[entry_idx[-1] + 1 :]
        fixed = "\n".join(head + tail)
        budget = limit - len(fixed) - 1
        kept: list[str] = []
        for line in reversed(lines[entry_idx[0] : entry_idx[-1] + 1]):
            cost = len(line) + 1
            if cost <= budget:
                kept.insert(0, line)
                budget -= cost
                continue
            # The newest entry alone overflows: keep its opening (the hotel name)
            # rather than dropping it, and stop.
            if not kept and budget > 40:
                kept.insert(0, line[:budget])
            break
        result = "\n".join(head + kept + tail)
        # Header/footer lines alone could in principle outrun the budget. The
        # caller sized this for its prompt, so the cap is honoured either way.
        return result if len(result) <= limit else result[:limit]

    async def _bare_yes_accepts_an_offer(
        self,
        *,
        previous_bot_message: str,
        user_message: str,
        session_id: str = "",
        handoff_kind: bool = False,
        hotel_code: str = "",
    ) -> "dict | None":
        """Did the bot offer to SHOW or SUGGEST something, and has the guest just
        accepted it?

        `handoff_kind` (admin-managed hotels only - T24 onward; Zone, Mango and
        Sarovar keep the prompt they were tuned on) adds offer_kind=handoff, so a
        real staff offer is not read as "nothing was offered".

        Returns a short description of what was offered, or None. Only fires on a
        bare agreement — anything with content of its own is left alone, because
        the guest has said what they want and needs no help being interpreted.

        The distinction that matters: offering to SHOW options ("shall I suggest
        some beverages?") is not the same as asking permission to ACT ("shall I
        place this order?"). A yes to the first must produce a list; a yes to the
        second must produce the action. Conflating them is what left a guest with
        a placed order and none of the drinks they had just said yes to.
        """
        prev = str(previous_bot_message or "").strip()
        msg = str(user_message or "").strip()
        if not prev or not msg or len(msg) > 40:
            return None
        # Retire any verdict left from an earlier turn that said the same short
        # thing. A verdict only speaks for the turn it was reached on: if the
        # call below fails, the slate stays empty and nothing is blocked.
        self._remember_bare_yes_verdict(session_id, msg, {})
        _handoff_kind_name = "handoff|" if handoff_kind else ""
        _handoff_kind_rule = (
            "offer_kind=handoff when the assistant ASKED whether the guest wants someone "
            "from the hotel to contact them, call them back, reach out, share something "
            "with them later, or take their request to the team - 'Would you like our "
            "team to reach out to share that with you?', 'Shall I have someone call "
            "you?', 'Do you want me to connect you with our team?'. It must be a "
            "question the guest can say yes to. A statement that staff can help "
            "('once you're checked in, the team can help with that'), a refusal, or "
            "telling the guest to contact the hotel themselves is NOT handoff - that is "
            "none. (Read as none, a guest who said yes to 'Would you like our team to "
            "reach out?' was told 'Sure - what would you like help with?' and no form "
            "opened.)\n"
        ) if handoff_kind else ""
        # A TEAM-ONLY HOTEL OFFERS ITS TEAM, AND "YES" MUST ACCEPT THAT.
        # Mango's bot asks "Would you like me to connect you with them?" and the
        # kinds below had no name for that offer, so a guest's "yes" came back as
        # "nothing was offered". Two guards trust that verdict: the router dropped
        # the support route, and the form code stripped the form and asked "Sure -
        # what would you like help with?" (Mango, 9 Oct, three times in one chat).
        # Mango only: every other hotel gets this prompt byte for byte as before.
        _connect_kind = False
        try:
            from services.booking_assistance_state import team_booking_enabled as _ba_team_only

            _connect_kind = bool(_ba_team_only(hotel_code))
        except Exception:
            _connect_kind = False
        system_prompt = (
            "You are given the assistant's last message in a hotel chat, and the guest's "
            "reply. Decide TWO things.\n"
            "Return STRICT JSON: {\"is_bare_agreement\":bool,\"offer_kind\":\"show|"
            + "add_item|invite_more|"
            + ("connect|" if _connect_kind else "")
            + _handoff_kind_name
            + "none\",\"offered\":\"<what was offered, a few words, "
            "or empty>\"}\n"
            "offer_kind=show when the assistant offered to SHOW / SUGGEST / LIST / TELL "
            "THEM ABOUT specific things.\n"
            "offer_kind=add_item when the assistant named ONE OR MORE PARTICULAR DISHES "
            "and asked whether to ADD them to the guest's order - 'would you like to add "
            "the Filter Coffee?', 'shall I add 2 naan to that?', 'would you like the "
            "Arrabbiata Pasta with it?'. The guest is being asked about FOOD GOING INTO "
            "AN ORDER, not about being shown information. Put the dish name(s) in "
            "`offered`. This is NOT show: nothing is left to display, they have already "
            "been told what it is and what it costs, and a yes here means they want it. "
            "Answering such a yes with a list instead of adding the dish loses the order "
            "- a guest said yes to a coffee and it never reached the kitchen.\n"
            "OFFERING TO START A NEW ORDER FOR A NAMED DISH IS add_item. When the "
            "assistant says 'I can help you start a new order for the 2 x Arrabbiata "
            "Pasta separately' or 'shall I put the Tiramisu on a separate order?', a "
            "dish is named and it is going into an order - that is add_item and "
            "`offered` is the dish. It is NOT show: there is nothing left to display, "
            "and a yes means they want it made. Read as show, the guest said yes and "
            "got a tidy list instead of an order - 2 Arrabbiata Pasta were confirmed "
            "to a guest and never reached the kitchen.\n"
            "offer_kind=invite_more when it asked, without naming anything, whether the "
            "guest wanted to add or order something further ('would you like to add "
            "anything else?', 'anything else for you?'). A yes there means they DO want "
            "something more but have not yet said what.\n"
            "A NAMED DISH BEATS A TRAILING 'OR ANYTHING ELSE'. Assistants often offer "
            "one dish and then leave the door open in the same breath: 'I can add the "
            "Vegetable Burger (₹650), or any other available items - what would you "
            "like to do?'. That is add_item, NOT invite_more, and `offered` is the "
            "Vegetable Burger. A dish was named, so a bare yes means THAT dish; the "
            "open-ended tail does not erase it. Reading such an offer as invite_more "
            "threw the named dish away and asked the guest to choose all over again, "
            "after they had already said yes to it. Only choose invite_more when NO "
            "dish is named anywhere in the assistant's message.\n"
            + (
                "offer_kind=connect when the assistant ITSELF offered to put the guest in "
                "touch with the hotel team - asked ('Would you like me to connect you with "
                "our team?', 'Would you like me to connect you with them?', 'Shall I have "
                "our team contact you?') or stated ('If you prefer, I can connect you with "
                "our team', 'I can have our team get back to you'). Leave `offered` empty. "
                "It is NOT connect when the message only says the team can help later or "
                "after check-in, tells the guest to call or contact the hotel themselves, "
                "or points them to a button or form - those are none.\n"
                if _connect_kind else ""
            )
            + _handoff_kind_rule
            + "offer_kind=none for everything else, including asking permission to act.\n"
            "is_bare_agreement: true only when the guest's reply is pure agreement with "
            "no content of its own ('yes', 'yes please', 'sure', 'ok', 'go ahead', "
            "'please do'). If they said anything specific, it is false.\n"
            "NAMING THE ACTION IS NOT BARE AGREEMENT. 'yes place it', 'place the "
            "order', 'send it to the kitchen', 'order it' say exactly what the guest "
            "wants done - they are instructions, not a yes to whatever happened to be "
            "mentioned last. Return is_bare_agreement=false for those, whatever the "
            "assistant had just said. Reading 'yes place it' as accepting a paneer "
            "tikka that had been suggested in passing meant the guest was asked what "
            "else they wanted instead of having their order placed, and the food never "
            "reached the kitchen.\n"
            "offered: fill this ONLY when the assistant's message offered to SHOW, "
            "SUGGEST, LIST or TELL THEM ABOUT something — options, alternatives, a menu "
            "section, recommendations. Describe what was offered, e.g. 'the available "
            "beverages and mocktails'.\n"
            "Leave `offered` EMPTY when the assistant was instead asking permission to "
            "DO something — place an order, raise a request, go ahead, book, confirm, "
            "cancel — or asking the guest to fill a form or choose between named things. "
            "Those are actions and confirmations, not offers to show information.\n"
            "If the assistant did both, prefer the offer to show, because the guest can "
            "still be asked to confirm afterwards, whereas an offer skipped is simply "
            "never answered. THE ONE EXCEPTION is add_item: when a specific dish was "
            "named and the question was whether to add it, that is add_item even if the "
            "message also described the dish or gave its price. Describing a dish while "
            "asking to add it is not an offer to show it."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    # The offer a guest says yes to is the END of a long reply: the
                    # first 900 characters cut "Would you like to see photos...?" off an
                    # overview, so the judge saw no offer. Keep the start and the end.
                    {"role": "user", "content": json.dumps(
                        {"assistant_said": prev if len(prev) <= 900 else f"{prev[:300]} ... {prev[-600:]}",
                         "guest_replied": msg},
                        ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=80,
                trace_context={"responder_type": "main", "agent": "offer_acceptance"},
            )
            # Keep the whole verdict, not just the half that has an annotation to
            # make. "Pure agreement, and NOTHING was offered" is a finding in its
            # own right - it is what tells the router that a yes accepted nothing.
            self._remember_bare_yes_verdict(session_id, msg, raw)
            if not isinstance(raw, dict) or not bool(raw.get("is_bare_agreement")):
                return None
            kind = str(raw.get("offer_kind") or "").strip().lower()
            offered = str(raw.get("offered") or "").strip()
            if kind == "add_item" and offered:
                return {"kind": "add_item", "offered": offered}
            if kind == "show" and offered:
                return {"kind": "show", "offered": offered}
            if kind == "invite_more":
                return {"kind": "invite_more", "offered": offered}
            return None
        except Exception as exc:
            logger.warning("_bare_yes_accepts_an_offer failed (non-fatal): %s", exc)
            return None

    async def _dishes_named_without_a_quantity(
        self,
        *,
        user_message: str,
    ) -> "list[str] | None":
        """Which dishes has the guest just named WITHOUT saying how many?

        A food line is a number of portions, and the guest is charged per
        portion. Asked for "one tandoori murgh and garlic naan" the model quietly
        wrote one naan and went straight to the confirmation - so a guest ordering
        for the table is served a single piece of bread and only finds out when the
        tray arrives. The service prompt was told to ask, twice, and both times lost
        to the older rule that says go straight to checkout. So the reading is done
        here instead, by a model asked nothing else, and the service agent is handed
        the answer and left to phrase the question in its own words.

        Returns the dish names carrying no quantity, [] when every dish has one, or
        None when the question does not apply or the call fails - in which case
        nothing is annotated and the turn proceeds exactly as it always has.
        """
        msg = str(user_message or "").strip()
        if not msg or len(msg) > 400 or msg.startswith("[CONTEXT:"):
            return None
        system_prompt = (
            "The guest of a hotel is talking to room service. Read their message "
            "and list the DISHES or DRINKS they are asking for that do NOT carry a "
            "quantity.\n"
            "Return STRICT JSON: {\"is_ordering\":bool,\"without_quantity\":[\"dish\"]}\n"
            "is_ordering: true only when the guest is actually asking for food or "
            "drink to be brought to them. Questions ('what desserts do you have', "
            "'is it spicy'), small talk, complaints and requests meant for another "
            "department are false.\n"
            "without_quantity: name each dish the guest asked for with NO number and "
            "no word standing in for one ('a', 'an', 'one', 'a couple of', 'two'). "
            "In 'one tandoori murgh and garlic naan' the murgh HAS a quantity and the "
            "naan does NOT, so return ['garlic naan'].\n"
            "Use the guest's own words for the dish name.\n"
            "When one quantity plainly covers everything ('two of each'), or the "
            "guest names no dish at all, return an empty list.\n"
            "If you are unsure whether a dish carries a quantity, leave it OUT: "
            "asking again about something already answered is a small irritation, "
            "but interrogating a guest over nothing is worse than assuming one."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(
                        {"guest_said": msg[:400]}, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=120,
                trace_context={"responder_type": "main", "agent": "portion_check"},
            )
            if not isinstance(raw, dict) or not bool(raw.get("is_ordering")):
                return None
            return [
                str(n).strip() for n in (raw.get("without_quantity") or [])
                if str(n or "").strip()
            ]
        except Exception as exc:
            logger.warning(
                "_dishes_named_without_a_quantity failed (non-fatal): %s", exc)
            return None

    async def _cart_lines_that_survive_an_edit(
        self,
        *,
        cart: "list[dict]",
        revised_request: str,
    ) -> "list[dict] | None":
        """Which cart lines are still in the order after the ticket was rewritten?

        The rewritten ticket line is what the kitchen will cook. This maps it back
        onto the cart so the guest is shown the same order, with the prices and
        allergens the cart already holds.

        Returns the surviving lines, or None to leave the cart alone - on any
        doubt the cart is left as it was, because a wrong rewrite here would
        delete food the guest is still expecting.
        """
        lines = [l for l in (cart or []) if isinstance(l, dict) and str(l.get("item") or "").strip()]
        revised = str(revised_request or "").strip()
        if not lines or not revised:
            return None
        system_prompt = (
            "A room-service order was changed. You are given the order as it was, "
            "and the rewritten request line describing what it should be NOW.\n"
            "Say which of the original items are still in the order, and how many "
            "of each.\n"
            "Return STRICT JSON: {\"items\":[{\"item\":\"exact name from the "
            "order as it was\",\"qty\":number}]}\n"
            "Use the item names EXACTLY as they appear in the original order, so "
            "they can be matched back to their prices.\n"
            "Leave out any item the rewritten line no longer mentions - that is the "
            "one the guest removed. Keep every item it still mentions, with the "
            "quantity it gives.\n"
            "If the rewritten line is empty or you cannot tell what it means, return "
            "the original items unchanged rather than guessing."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({
                        "order_as_it_was": [
                            {"item": str(l.get("item")), "qty": int(l.get("qty") or 1)}
                            for l in lines
                        ],
                        "rewritten_request": revised[:400],
                    }, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=200,
                trace_context={"responder_type": "main", "agent": "cart_after_edit"},
            )
            if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
                return None
            by_name = {str(l.get("item")).strip().lower(): l for l in lines}
            out = []
            for entry in raw["items"]:
                if not isinstance(entry, dict):
                    continue
                src = by_name.get(str(entry.get("item") or "").strip().lower())
                if not src:
                    continue
                try:
                    qty = int(entry.get("qty") or 1)
                except Exception:
                    qty = int(src.get("qty") or 1)
                if qty <= 0:
                    continue
                merged = dict(src)
                merged["qty"] = qty
                out.append(merged)
            # An empty result would silently wipe an order the guest still has;
            # a real "cancel everything" arrives as a cancel, handled separately.
            return out or None
        except Exception as exc:
            logger.warning("_cart_lines_that_survive_an_edit failed (non-fatal): %s", exc)
            return None

    async def _guest_is_talking_about_their_food_order(
        self,
        *,
        user_message: str,
    ) -> bool:
        """Is the guest asking about, adding to, or changing their room-service order?

        Asked only when an order is already open and the turn has routed away from
        the dining lane, so the question is never "is this about food" in the
        abstract - there IS an order, and this decides whether the guest is
        speaking about it.

        False on any doubt: pulling an unrelated turn into the dining service would
        answer a housekeeping request with a menu, which is worse than the ungrounded
        recall this exists to prevent.
        """
        msg = str(user_message or "").strip()
        if not msg or len(msg) > 300:
            return False
        system_prompt = (
            "A hotel guest has an in-room dining order open - some food they have "
            "asked for and not yet received. Read their next message and decide "
            "whether it is about THAT ORDER.\n"
            "Return STRICT JSON: {\"about_the_order\":bool}\n"
            "true when they ask what they ordered, what it costs, where it is or how "
            "long it will take, when they add or remove a dish, change a quantity, "
            "cancel it, confirm it, or answer a question the assistant asked them "
            "about it.\n"
            "true for ANY question ABOUT the order or orders themselves, not only "
            "'what did I order': how many orders they have, whether two orders are "
            "separate or have been combined, which order something is on, whether an "
            "order is still open or already placed, or a request to confirm/check any "
            "of that. These are questions only the order can answer. A guest asked "
            "'can you confirm both orders are kept separate?' three times and was told "
            "'I don't have that detail confirmed' by a part of the system that cannot "
            "see their orders — the orders were sitting right there, correct.\n"
            "true also for a short reply that only makes sense as a continuation of "
            "the order ('yes lets continue', 'go ahead', 'that one').\n"
            "false for anything else - a broken air conditioner, towels, the spa, a "
            "table in the restaurant, the wifi password, checkout time, small talk. "
            "A guest can want something else entirely while an order is open.\n"
            "When it could be either, answer false."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(
                        {"guest_said": msg[:300]}, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=40,
                trace_context={"responder_type": "main", "agent": "order_reentry"},
            )
            return bool(isinstance(raw, dict) and raw.get("about_the_order"))
        except Exception as exc:
            logger.warning(
                "_guest_is_talking_about_their_food_order failed (non-fatal): %s", exc)
            return False

    async def _guest_is_asking_to_change_or_cancel(
        self,
        *,
        user_message: str,
        previous_bot_message: str,
    ) -> "bool | None":
        """Is the guest asking to CHANGE or CANCEL something they already
        requested — or simply answering the question the bot just asked them?

        Judged from the pair, because the message alone cannot say: a bare "yes"
        means "cancel it" after "shall I cancel your order?", and means "go
        ahead" after "shall I book the cab?". Returns None when unclear, and
        None does NOT block — the caller only uses an explicit False.
        """
        msg = str(user_message or "").strip()
        if not msg:
            return None
        system_prompt = (
            "A hotel guest is mid-conversation. Decide ONE thing: is their latest "
            "message asking to CHANGE or CANCEL something they previously requested?\n"
            "Return STRICT JSON: {\"change_or_cancel\":bool,\"why\":\"<a few words>\"}\n"
            "TRUE when they ask to cancel, drop, undo, move, reschedule or amend an "
            "existing request — including a bare 'yes' when the assistant's previous "
            "message asked whether to cancel or change something.\n"
            "FALSE when they are doing anything else: answering a different question "
            "the assistant asked ('shall I go ahead?', 'shall I place this order?', "
            "'please fill in the details'), agreeing to a new request, asking for "
            "something new, or asking for information. A plain 'yes' that accepts an "
            "offer or confirms a NEW request is FALSE — accepting is not cancelling.\n"
            "Judge the pair together; the message alone cannot tell you."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(
                        {"assistant_just_said": str(previous_bot_message or "")[:700],
                         "guest_replied": msg}, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=60,
                trace_context={"responder_type": "main", "agent": "change_or_cancel_intent"},
            )
            if not isinstance(raw, dict) or "change_or_cancel" not in raw:
                return None
            return bool(raw.get("change_or_cancel"))
        except Exception as exc:
            logger.warning("_guest_is_asking_to_change_or_cancel failed (non-fatal): %s", exc)
            return None

    @staticmethod
    def _looks_like_form_ticket(request_line: str) -> bool:
        """Was this ticket's request line rendered from form fields?

        Structural, not a list of known form names: the renderer joins the
        fields the guest filled in as "Label: value; Label: value", so two or
        more of those pairs is the signature. A request the model worded itself
        is ordinary prose and has at most the one colon after its title.
        """
        text = str(request_line or "").strip()
        if not text:
            return False
        pairs = [seg for seg in text.split(";") if ":" in seg]
        return len(pairs) >= 2

    @staticmethod
    def _ticket_label(line: dict) -> str:
        """The SHORT, guest-facing name for one open request.

        `_ticket_subject` below is written for the match judge and deliberately
        piles on the kind and the staff summary; said out loud to a guest it
        reads "the guest's Order 1: In-room dining order: 2 x Lachha Paratha
        (in-room_dining)". Anything the guest is asked to choose between, or
        told was cancelled, uses this instead.
        """
        if not isinstance(line, dict):
            return ""
        _r = str(line.get("request") or "").strip()
        try:
            _n = int(line.get("order_no") or 0)
        except (TypeError, ValueError):
            _n = 0
        if _n > 0:
            return ("Order %d — %s" % (_n, _r)) if _r else ("Order %d" % _n)
        return _r

    @staticmethod
    def _ticket_subject(line: dict) -> str:
        """Everything we know about what one ticket is FOR, as one sentence.

        The judge below is asked "is the guest talking about this request?" and
        was handed the issue line alone. For a spa booking that line was the
        single word "Massage", so the honest answer to "cancel the spa booking"
        was "I cannot tell" — and an unsure judge drops the ref, which is why a
        real booking stayed open. The service that raised the ticket and its
        staff-facing summary say what the issue line leaves out, so they are
        included here rather than thrown away.
        """
        if not isinstance(line, dict):
            return ""
        request = str(line.get("request") or "").strip()
        kind = str(line.get("kind") or "").strip()
        summary = str(line.get("summary") or "").strip()
        order_no = int(line.get("order_no") or 0)
        parts: list[str] = []
        if order_no > 0:
            # The guest was shown this as "Order 2" — saying so is what lets the
            # judge agree that "cancel my second order" is about this ticket.
            parts.append(f"the guest's Order {order_no}:")
        if request:
            parts.append(request)
        # Only when it adds something — repeating the issue line back teaches the
        # judge nothing and just spends tokens.
        if kind and kind.lower() not in request.lower():
            parts.append(f"({kind})")
        if summary and summary.lower() not in request.lower():
            parts.append(f"- {summary}")
        return " ".join(parts).strip()

    async def _cancel_target_is_the_same_thing(
        self,
        *,
        user_message: str,
        ticket_request: str,
    ) -> "bool | None":
        """Is the thing the guest is cancelling the thing this ticket is about?

        Deliberately narrow: one message, one ticket, one yes/no. The big
        classifier has to pick a referent out of a list while juggling a dozen
        other fields, and under that load it will hand back the only open ticket
        it can see even when the subjects are unrelated — it matched "cancel the
        spa booking" to an "AC not cooling" repair at high confidence. Asked on
        its own, with nothing else in view, the same size of model is steady.

        Returns True (same thing), False (different thing), or None when it
        cannot tell. None means "do not block", so a hiccup here can never stop a
        legitimate cancellation.
        """
        msg = str(user_message or "").strip()
        req = str(ticket_request or "").strip()
        if not msg or not req:
            return None
        system_prompt = (
            "A hotel guest wants to cancel something. You are shown ONE existing request "
            "of theirs. Decide only this: is the guest's message about THAT request, or "
            "about something else?\n"
            "Return STRICT JSON: {\"same_thing\":bool,\"why\":\"<a few words>\"}\n"
            "FIRST: if the guest's message does not name any particular service or item — "
            "'cancel it', 'cancel that', 'we don't need it anymore', 'call it off' — answer "
            "TRUE and stop. They are pointing at something by context, which is normal "
            "language, and there is nothing in their words that could contradict this "
            "request. Only carry on to the test below when they DID name something.\n"
            "Judge what the request IS, not whether the words match. The SAME thing "
            "described differently is still the same thing: a 'food order' and an 'in-room "
            "dining order' are one and the same; 'my table booking' is a table reservation; "
            "'the pickup' is an airport transfer. Guests never use our internal wording, so "
            "different words are expected and are NOT evidence of a different request.\n"
            "same_thing=true when they mean this request — they name it in any words, or "
            "they refer to it by context and it is the obvious fit. Also true when they name "
            "nothing at all ('cancel it', 'we don't need it anymore'): context is all they "
            "gave you.\n"
            "same_thing=false ONLY when the message is about a genuinely different service — "
            "a spa appointment is not a maintenance repair, a taxi is not housekeeping, a "
            "table reservation in the restaurant is not food delivered to the room. Being "
            "the only request in front of you is not evidence that they meant it.\n"
            "When you are unsure, answer true: this check exists to catch requests that are "
            "plainly about something else, not to second-guess ordinary phrasing."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"guest_message": msg, "the_request": req}, ensure_ascii=False
                        ),
                    },
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=60,
                trace_context={"responder_type": "main", "agent": "cancel_target_check"},
            )
            if not isinstance(raw, dict) or "same_thing" not in raw:
                return None
            return bool(raw.get("same_thing"))
        except Exception as exc:
            logger.warning("_cancel_target_is_the_same_thing failed (non-fatal): %s", exc)
            return None

    _VENUE_NAME_CACHE: dict[str, set[str]] = {}

    async def _known_venue_names(self, hotel_code: str) -> set[str]:
        """Every venue this hotel actually has, as the guest would name one.

        Read from the reservation form's own dropdown - the same list the guest
        picks from - so "is this a real outlet?" is answered from data instead of
        being guessed at by a model that was never given the list.
        """
        key = str(hotel_code or "").strip().lower()
        if key in self._VENUE_NAME_CACHE:
            return self._VENUE_NAME_CACHE[key]
        names: set[str] = set()
        try:
            import json as _json
            from sqlalchemy import text as _text
            from models.database import AsyncSessionLocal as _ASL

            async with _ASL() as _db:
                _res = await _db.execute(_text(
                    "SELECT s.form_config FROM new_bot_services s "
                    "JOIN new_bot_hotels h ON h.id = s.hotel_id "
                    "WHERE h.code = :hc AND s.form_config IS NOT NULL"
                ), {"hc": key})
                rows = _res.fetchall()
            for (raw,) in rows:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "replace")
                cfg = _json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(cfg, dict):
                    continue
                for fld in (cfg.get("fields") or []):
                    if not isinstance(fld, dict):
                        continue
                    # Only fields that actually list PLACES. The same hotel has an
                    # "Event Type" dropdown offering Meeting / Conference / Banquet,
                    # and treating those as venues would let "move it to conference"
                    # look like a real outlet change.
                    _label = str(fld.get("label") or fld.get("name") or "").lower()
                    if not any(w in _label for w in ("outlet", "venue", "restaurant")):
                        continue
                    for opt in (fld.get("options") or []):
                        val = opt.get("value") if isinstance(opt, dict) else opt
                        if str(val or "").strip():
                            names.add(str(val).strip().lower())
        except Exception as exc:
            logger.warning("[VENUES] could not read the outlet list (%s) - "
                           "leaving name checks to the old behaviour", exc)
            return set()
        self._VENUE_NAME_CACHE[key] = names
        logger.info("[VENUES] %s has %d named venue(s): %s", key, len(names), sorted(names))
        return names

    @staticmethod
    def _forget_cancelled_booking(context: ConversationContext, ref: str, label: str = "", kind: str = "") -> None:
        """A cancelled room-booking request leaves the booking ledger (hotels set
        up in the Pre-booking tab, T24 onward). Left in `_rb_bookings`, the next
        "book a room" at the same hotel counted as already booked: the guest got
        the link again and no new request reached the team."""
        try:
            from services import prebooking_settings_service as _pbs_led

            if not _pbs_led.is_admin_managed(str(getattr(context, "hotel_code", "") or "").strip().lower()):
                return
            pd = context.pending_data if isinstance(context.pending_data, dict) else None
            if pd is None or not pd.get("_rb_bookings"):
                return
            ref_s = str(ref or "").strip()
            is_booking = "roombooking" in re.sub(r"[^a-z]", "", str(kind or "").lower())
            label_l = str(label or "").lower()
            kept = []
            for row in pd.get("_rb_bookings") or []:
                if not isinstance(row, dict):
                    continue
                prop = str(row.get("property") or "").strip().lower()
                if (ref_s and str(row.get("ticket_ref") or "").strip() == ref_s) or (
                    is_booking and prop and prop in label_l
                ):
                    continue
                kept.append(row)
            pd["_rb_bookings"] = kept
        except Exception as exc:
            logger.debug("booking ledger clean-up after cancel skipped: %r", exc)

    async def _answer_to_cancel_menu(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        session_id: str,
    ) -> "OrchestrationDecision | None":
        """The guest's reply to our own "which one should I cancel?" (hotels set up
        in the Pre-booking tab, T24 onward).

        Asked "Which one would you like me to cancel?", a T24 guest copied the
        first option and added "this one", and was asked again: the check that a
        cancel names its target reads the message alone, not the question it
        answers. "first one booking room" then went to the room-booking agent as
        a new booking, and the booking stayed open (14 Oct). The list we showed is
        kept for one turn, and the reply is read against it by a judge worded in
        the DB row `ticketing.cancel_menu_pick_rules`. None hands the turn on."""
        pd = context.pending_data if isinstance(context.pending_data, dict) else None
        menu = pd.pop("_cancel_menu", None) if pd is not None else None
        if not isinstance(menu, list) or not menu:
            return None
        try:
            from services import prebooking_settings_service as _pbs_menu
            from services.ticketing_service import ticketing_service

            if not _pbs_menu.is_admin_managed(str(getattr(context, "hotel_code", "") or "").strip().lower()):
                return None
            options = [m for m in menu if isinstance(m, dict) and str(m.get("ref") or "").strip()][:6]
            rules = await self._db_prompt_text("ticketing.cancel_menu_pick_rules") if options else ""
            if not rules:
                return None
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": rules},
                    {"role": "user", "content": json.dumps({
                        "options": [{"n": i + 1, "request": str(o.get("label") or "")} for i, o in enumerate(options)],
                        "guest_replied": str(user_message or "").strip()[:400],
                    }, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=20,
                trace_context={"responder_type": "main", "agent": "cancel_menu_pick"},
            )
            try:
                picked = int(raw.get("picked") or 0) if isinstance(raw, dict) else 0
            except (TypeError, ValueError):
                picked = 0
            if not 1 <= picked <= len(options):
                logger.info(
                    "[TICKET_EDIT] reply to the cancel list picks none of %d (%r) - normal flow",
                    len(options), str(user_message or "")[:60],
                )
                return None
            chosen = options[picked - 1]
            ref = str(chosen.get("ref") or "").strip()
            label = str(chosen.get("label") or "").strip().rstrip(" .")
            open_refs = {
                str(t.get("ticket_ref") or "").strip()
                for t in (await ticketing_service.list_session_tickets(session_id) or [])
            }
            if ref not in open_refs or ref in ticketing_service.session_cancelled_refs(session_id):
                logger.info("[TICKET_EDIT] picked %s is no longer open - normal flow", ref)
                return None
            res = await ticketing_service.append_ticket_comment(
                session_id=session_id, ticket_ref=ref,
                note="CANCELLED: Guest chose this request from the list to cancel.",
                set_status="cancel",
            )
            if not getattr(res, "success", False):
                return None
            ticketing_service.mark_ticket_cancelled(session_id, ref)
            self._forget_cancelled_booking(context, ref, label, str(chosen.get("kind") or ""))
            logger.info("[TICKET_EDIT] guest picked option %d of the cancel list -> cancelled %s", picked, ref)
            return OrchestrationDecision(
                intent="ticket_edit",
                action="respond_only",
                response_text=(
                    "Done — I've cancelled "
                    + (f"your request for {label}." if label else "that request for you.")
                    + " Is there anything else I can help with?"
                ),
                confidence=0.9,
                metadata={
                    "ticket_edit_handled": True,
                    "ticket_edit_ref": ref,
                    "ticket_edit_action": "cancel",
                },
            )
        except Exception as exc:
            logger.warning("[TICKET_EDIT] cancel list reply skipped (non-fatal): %r", exc)
            return None

    @staticmethod
    def _cancel_menu_rows(lines: list[dict]) -> list[dict]:
        """What `_answer_to_cancel_menu` reads the next reply against."""
        return [
            {"ref": str(t.get("ref") or ""), "label": LLMOrchestrationService._ticket_label(t), "kind": str(t.get("kind") or "")}
            for t in lines
            if isinstance(t, dict) and str(t.get("ref") or "").strip()
        ]

    async def _maybe_handle_ticket_edit(
        self,
        *,
        user_message: str,
        context: ConversationContext,
    ) -> "OrchestrationDecision | None":
        """When the guest asks to CANCEL or MODIFY something they already
        requested THIS session, append a comment to the EXACT existing ticket
        instead of raising a new one (and instead of looping on the service
        that can't perform the change).

        Fully context-driven: a small LLM matches the guest's words to ONE of
        the session's open tickets (or none). Per product rule — an edit
        comments on the matched ticket; when nothing matches we still just
        acknowledge and create NO new ticket. Returns a respond_only decision
        when it handled the edit, else None so the normal flow continues.
        Fail-open: any error → None.
        """
        try:
            session_id = str(getattr(context, "session_id", "") or "").strip()
            if not session_id:
                return None
            from services.ticketing_service import ticketing_service

            _menu_decision = await self._answer_to_cancel_menu(
                user_message=user_message, context=context, session_id=session_id,
            )
            if _menu_decision is not None:
                return _menu_decision

            # Cheap cancel/drop-intent pre-check (no DB, no LLM). This lets us also
            # intercept a guest CANCELLING a request that never became a committed
            # ticket (e.g. a form-based service they never submitted). Without it
            # that cancel falls through and a service can misclassify it as a NEW
            # create_ticket → spurious "cancellation" ticket + generic booking reply.
            _msg_l = str(user_message or "").strip().lower()
            _looks_cancel = any(kw in _msg_l for kw in (
                "cancel", "call it off", "don't need", "do not need", "dont need",
                "no longer need", "no longer want", "forget the", "forget it",
                "drop the", "drop it", "scrap that", "scrap the", "never mind",
                "don't want the", "do not want the", "remove the",
            ))


            # A form/slot flow is normally authoritative while mid-fill — but a clear
            # CANCEL must still be honored: abort the form and acknowledge, never let
            # the cancel silently become a NEW ticket. So bail on a pending form ONLY
            # when this is NOT a cancel. When it IS a cancel we return cancel_pending
            # below, which clears the parked form and raises nothing.
            _pending = bool(str(getattr(context, "pending_action", "") or "").strip())
            if _pending and not _looks_cancel:
                return None

            # LATENCY GUARD: `list_session_tickets` hits prod MySQL over VPN, and
            # under pool contention that blocked ~3-5s on EVERY turn. Only pay for it
            # when this session actually has a committed ticket. When it does not, we
            # still run the (small) classifier IF the message looks like a cancel, so
            # a cancel of an uncommitted request is intercepted rather than ticketed.
            _known = ticketing_service.session_has_known_tickets(session_id)
            if not _known and not _looks_cancel:
                return None

            ticket_lines: list[dict[str, str]] = []
            # Only in-room dining can ever be locked - the window is a kitchen rule.
            # Housekeeping, spa, transport and the rest stay editable, so this set
            # stays empty for them and every check below simply does not fire.
            _locked_refs: set[str] = set()
            # `_known` is an IN-MEMORY set that a restart wipes, while the tickets
            # themselves live in the DB keyed by session_id. Loading only when
            # `_known` meant that after a deploy, a guest cancelling was told
            # "I've noted that for our team" while the ticket stayed OPEN — the
            # classifier had an empty list to match against, so nothing was
            # cancelled and nobody was told. Silent, and worse than an error.
            #
            # A cancel now always pays for the lookup. That is the rare path (it
            # needs cancel-shaped wording), and getting a cancellation wrong is
            # worth far more than the query costs.
            # Mango (team-only hotel) gets the two guards marked "Mango" below;
            # every other hotel runs this function exactly as before.
            try:
                from services.booking_assistance_state import team_booking_enabled as _ba_team_only_edit

                _team_only_edit = bool(_ba_team_only_edit(getattr(context, "hotel_code", "")))
            except Exception:
                _team_only_edit = False
            if _known or _looks_cancel:
                tickets = await ticketing_service.list_session_tickets(session_id)
                # THIS CHAT'S REQUESTS ONLY (Mango). Tickets belong to the visitor,
                # so after the guest cleared the chat, "i want to modify booking
                # details" was matched to an event enquiry from the previous chat, a
                # note was written on it and the guest - who had booked nothing in
                # this chat - was told "I've noted your request" (Mango, 9 Oct). A
                # ticket counts when it was raised in this chat, or when this chat's
                # own booking form or forms wrote to it (the booking form updates the
                # lead ticket an earlier chat raised).
                if _team_only_edit:
                    _pd_scope = context.pending_data if isinstance(context.pending_data, dict) else {}
                    _uid_now = str(_pd_scope.get("_active_conversation_uid") or "").strip()
                    if _uid_now:
                        _own_refs = {
                            str(_b.get("ticket_ref") or "").strip()
                            for _b in (_pd_scope.get("_rb_bookings") or []) if isinstance(_b, dict)
                        } | {str(_x).strip() for _x in (_pd_scope.get("_form_built_ticket_refs") or [])}
                        _own_refs.discard("")
                        _all_n = len(tickets)
                        tickets = [
                            t for t in tickets
                            if str(t.get("conversation_uid") or "").strip() == _uid_now
                            or str(t.get("ticket_ref") or "").strip() in _own_refs
                        ]
                        if len(tickets) != _all_n:
                            logger.info(
                                "[TICKET_EDIT] %d of %d session tickets are from earlier chats - "
                                "not offered for edits (session=%s)",
                                _all_n - len(tickets), _all_n, session_id,
                            )
                # A ticket the guest already CANCELLED this session is terminal — drop
                # it so the classifier can't keep re-matching later unrelated messages
                # ("what's the weather", "thanks") to the same cancel and re-fire.
                _cancelled_refs = ticketing_service.session_cancelled_refs(session_id)
                # WHICH ORDER NUMBER THE GUEST KNOWS EACH FOOD TICKET BY.
                # A conversation can hold several food orders, and the guest refers
                # to them the way they were shown: "cancel my second order". The
                # ticket rows carry no such number - it lives in the order ledger -
                # so without this the classifier has to guess between two tickets
                # that look almost identical, and a wrong guess cancels the wrong
                # meal.
                _order_no_by_ref: dict[str, int] = {}
                try:
                    _pd_led = getattr(context, "pending_data", None)
                    for _r in ((_pd_led or {}).get("_ird_placed_orders") or []):
                        if not isinstance(_r, dict):
                            continue
                        _rr = str(_r.get("ticket_ref") or "").strip()
                        if _rr and int(_r.get("n") or 0) > 0:
                            _order_no_by_ref[_rr] = int(_r["n"])
                except Exception:
                    _order_no_by_ref = {}
                ticket_lines = [
                    {
                        "ref": str(t.get("ticket_ref") or "").strip(),
                        "order_no": _order_no_by_ref.get(
                            str(t.get("ticket_ref") or "").strip(), 0
                        ),
                        "request": str(t.get("issue") or "").strip(),
                        # WHAT THIS TICKET IS — not the label the API demanded.
                        # `category` is an API-compatibility value: the external
                        # ticket API rejects anything outside a fixed allowlist, so
                        # _coerce_ticket_category maps onto it, and "Food & Beverage"
                        # lands on "In-Room Dining". A TABLE RESERVATION and a SPA
                        # BOOKING were therefore both stored as category
                        # "In-Room Dining". Asked which ticket the guest meant, the
                        # model was looking at three tickets that all claimed to be
                        # food orders — so it picked a food order for "change it back
                        # to spr" and the guest was told their meal was already being
                        # prepared. `sub_category` says what the request actually is
                        # ("table reservation", "spa & wellness booking",
                        # "in-room_dining") and is correct on every row, so that is
                        # what gets shown. The API label is not passed on at all.
                        "category": (
                            str(t.get("sub_category") or "").strip()
                            or str(t.get("department") or "").strip()
                        ),
                        # WHAT THE REQUEST IS, ALONGSIDE WHO WILL ACTION IT.
                        # A spa booking reached this classifier as request="Massage",
                        # category="Front Office" — nothing in either word says "spa",
                        # so "cancel the spa booking" matched no ticket, the guest was
                        # told it was noted, and the booking stayed open. The row
                        # already carried sub_category="spa & wellness booking" and a
                        # description saying so in full; they were simply never passed
                        # on. Naming what a ticket is about is what lets the checks
                        # below say a confident yes — and, just as importantly, a
                        # confident no.
                        "kind": str(t.get("sub_category") or "").strip(),
                        "summary": str(t.get("description") or "").strip(),
                        "room": str(t.get("room_number") or "").strip(),
                        "created_at": str(t.get("created_at") or ""),
                    }
                    for t in tickets
                    if str(t.get("ticket_ref") or "").strip()
                    and str(t.get("ticket_ref") or "").strip() not in _cancelled_refs
                ]
                # ── WHICH TICKETS ARE ACTUALLY FOOD ORDERS ──────────────────
                # Lockability used to be read off the category TEXT, and that text
                # lies: a Table Reservation was filed with category "In-Room
                # Dining", which matches "dining", so the kitchen lock claimed it.
                # The guest asked to move their TABLE to 9pm and was told "that
                # order is already being prepared, so it can no longer be changed"
                # - about a booking no kitchen was cooking. The booking never moved.
                #
                # The order ledger knows exactly which refs are food orders,
                # because it recorded each one as it was placed. Where it has any
                # knowledge at all it is the authority; only when it is empty (the
                # order was placed in an earlier conversation, so this
                # conversation's ledger never saw it) do we fall back to reading
                # the category. The ledger keeps the last 10 orders, so a
                # conversation with more than ten food orders could fall back for
                # the oldest - they are long past the window anyway, and the
                # staff-pickup check below still covers them.
                _ledger_knows_food = bool(_order_no_by_ref)

                def _is_food_ticket(line: dict[str, Any]) -> bool:
                    if _ledger_knows_food:
                        return str(line.get("ref") or "").strip() in _order_no_by_ref
                    _c = str(line.get("category") or "").strip().lower()
                    return bool(_c) and any(
                        m in _c for m in ("dining", "food", "beverage", "restaurant")
                    )

                # A food order stops being changeable once the kitchen starts on
                # it. Anything raised longer ago than the window is shown to the
                # classifier as locked, so it declines the change instead of
                # telling the guest it is done and then not doing it.
                _locked_refs = {
                    line["ref"]
                    for line in ticket_lines
                    if _is_food_ticket(line)
                    and self._ird_edit_window_expired(
                        # `_is_food_ticket` has already settled that this IS a food
                        # order — by ref, not by wording — so the helper's own
                        # category gate must not veto it on a mislabelled row. It
                        # is asked only the question it alone can answer: the age.
                        category="in-room dining",
                        created_at=line.get("created_at", ""),
                    )
                }
                # The clock is only a guess at when the kitchen started. Ask the
                # staff dashboard as well: an order somebody has already taken is
                # locked however recent it is, so the guest is told it is being
                # prepared rather than promised a change nobody can make.
                for line in ticket_lines:
                    if line["ref"] in _locked_refs:
                        continue
                    # Same authority as above: asking the dashboard whether staff
                    # picked up a TABLE booking, and locking it as "being prepared"
                    # if they had, is the identical bug one step later.
                    if not _is_food_ticket(line):
                        continue
                    if await ticketing_service._staff_have_picked_up(line["ref"]) is True:
                        _locked_refs.add(line["ref"])
                for line in ticket_lines:
                    line["changeable"] = (
                        "no - LOCKED, ALREADY BEING PREPARED, CANNOT BE CANCELLED OR CHANGED"
                        if line["ref"] in _locked_refs else "yes"
                    )
            # No committed ticket AND not a cancel-ish message → nothing to edit;
            # let the normal flow answer (classifier was already skipped above).
            if not ticket_lines and not _looks_cancel:
                return None

            valid_refs = {t["ref"] for t in ticket_lines}
            last_assistant = self._last_message_by_role(context, "assistant")
            system_prompt = (
                "You decide whether an in-house hotel guest is asking to CANCEL or MODIFY "
                "something they ALREADY requested this session. Each prior committed request "
                "is an open ticket in `tickets`. NOTE: `tickets` MAY be empty — the guest can "
                "still be cancelling a request they made earlier this chat that was never "
                "formally logged (e.g. a form they never submitted).\n"
                "Return STRICT JSON only: {\"is_edit\":bool,\"is_locked_order_request\":bool,"
                "\"locked_ticket_ref\":\"<ref of the locked ticket they mean, or empty>\","
                "\"is_new_request\":bool,"
                "\"names_specific_place_or_item\":bool,"
                "\"named_place_or_item\":\"<the restaurant / bar / dish / drink / treatment / room type the guest named, copied from THEIR words, or empty>\","
                "\"named_refers_to_something_already_on_the_ticket\":bool,"
                "\"ticket_ref\":\"<ref or empty>\","
                "\"action\":\"cancel|modify|other\",\"match_confidence\":\"high|low\","
                "\"note\":\"<short internal staff note>\","
                "\"revised_request\":\"<full rewritten request line, or empty>\","
                "\"reply\":\"<warm one-line guest reply>\"}\n"
                + (
                    "Also include \"names_the_change\":bool in that JSON. true when THIS message itself says "
                    "WHAT should change - a new date, time, number of guests, room or another concrete detail "
                    "('move it to 16 Oct', 'make it 3 guests', 'change it to the Grove View room'); false when "
                    "the guest only says they want to change / modify / update something without saying what "
                    "('i want to modify my booking details', 'can I change my booking?', 'I need to update my "
                    "request').\n"
                    if _team_only_edit else ""
                )
                + "`action` MUST be exactly one of the three lowercase words cancel / modify / "
                "other — never a variant like 'cancelled', 'cancellation' or 'cancel_request'. "
                "If the guest is withdrawing a request, the value is the bare word cancel. This "
                "matters: the word you put here is what marks the request cancelled for staff, "
                "so a variant silently leaves it showing as still active.\n"
                "Rules:\n"
                "0) If `tickets` is EMPTY: set is_edit=true ONLY for a clear CANCEL/withdraw of "
                "a request the guest themselves made earlier in THIS chat (e.g. 'cancel the "
                "airport pickup', 'cancel my decoration', 'we don't need the extra bed anymore') "
                "— use action=cancel with ticket_ref empty. Do NOT set is_edit for a QUESTION about "
                "cancellation/refund policy, or for a brand-new request. (When `tickets` is empty a "
                "MODIFY cannot be applied, so return is_edit=false for a pure detail change.)\n"
                "0a) DECIDE is_new_request FIRST, BEFORE ANYTHING ELSE. Set is_new_request=true when "
                "the guest is asking to RAISE / FILE / OPEN / LOG / REPORT something NEW — a new "
                "complaint, a new booking, a new order, a new service request. 'raise a complaint', "
                "'file a complaint about X', 'report an issue with Y', 'book a room', 'order a dosa' "
                "are asks to CREATE work, not to change work that already exists. WHEN "
                "is_new_request IS TRUE, is_edit MUST BE false and ticket_ref MUST be empty — even "
                "when an open ticket is about the very same topic and shares its wording. A guest "
                "who already complained once is allowed to complain again about something else; the "
                "second complaint is a second request, never an update of the first.\n"
                "0b) WHICH HOTEL / PROPERTY / CITY A REQUEST BELONGS TO IS NOT AN EDITABLE DETAIL. "
                "The word 'location' in rule 1 means a pickup or drop point INSIDE one request "
                "(Terminal 2 -> Terminal 1), never which hotel the request is about. If the guest's "
                "message names a DIFFERENT hotel, property or city from the ticket you were about to "
                "match, that is a NEW request for that place: is_new_request=true, is_edit=false. "
                "(Real failure this prevents: a guest with an open complaint about the Goa hotel said "
                "'raise complaint on Patna hotel'. It was recorded as changing the Goa complaint's "
                "location — so the Patna complaint never reached staff, and the Goa complaint's own "
                "text was overwritten and lost.)\n"
                "0c) YOU CANNOT CHECK WHETHER A NAME IS REAL, SO NEVER DECIDE ONE IS. You are shown "
                "the guest's words and their open tickets — nothing else. You do NOT have the "
                "hotel's list of restaurants, bars, dishes, drinks, spa treatments or room types, so "
                "you have no way to tell a real one from one that does not exist here. Set "
                "names_specific_place_or_item=true whenever the guest's message NAMES such a thing "
                "as a NEW value that would REPLACE what the ticket already says — a different "
                "restaurant or bar to move the table to, a dish or drink to swap in, another "
                "treatment, another room type. Set it FALSE when the change is pure logistics on "
                "the SAME thing (time, date, how many people, room number). SEPARATELY from that "
                "flag, ALWAYS copy into `named_place_or_item` any restaurant, bar, dish, "
                "drink, treatment or room type the guest names in THEIR OWN WORDS, even "
                "when the change is only a time or a date, and even when you believe they "
                "meant the one already on the ticket. Copy their spelling, do not correct "
                "it to whatever the ticket says: a guest naming a place we do not have "
                "must be told so, and silently swapping their word for the ticket's is "
                "how \"change kadak hotel timings\" became a confirmed booking at a "
                "0d) named_refers_to_something_already_on_the_ticket: you CAN answer this one, because you are shown the ticket. Set it TRUE when the thing they named is already part of that ticket - a dish on the order, the outlet already booked, the treatment already requested - EVEN IF THEY WORD IT DIFFERENTLY. 'the 4th paratha', 'my second naan', 'the massage' all refer to something on a ticket that says '4 x Lachha Paratha' or 'Deep Tissue Massage'; an ordinal or a shortened name is still the same thing. Set it FALSE only when they are naming something NEW that is not on that ticket at all. When it is TRUE the guest is asking about their existing request, and nobody should be told we do not have such a place.\n"
                "different restaurant. Leave it EMPTY when they name nothing. The test is one "
                "question: IS THIS NAME ALREADY IN THE TICKET'S `request` TEXT? If yes, set FALSE "
                "— the guest is pointing at part of their existing request, which is already known "
                "to be real. If the name appears nowhere on the ticket and would become part of "
                "it, set TRUE, because only the service holding the real list can tell whether it "
                "exists. THIS FIELD IS ONLY ABOUT AN UNVERIFIABLE NAME: it says nothing about what "
                "the guest wants DONE, so it must never influence your `action`, which you decide "
                "independently by the rules below.\n"
                "1) is_edit=true ONLY when the guest EXPLICITLY wants to (a) CANCEL one of the "
                "listed prior requests, or (b) change a logistical DETAIL of one — its time, date, "
                "pickup/drop location, guest count, or room. There must be a clear cancel/change "
                "intent (words/meaning like cancel, don't want it, drop it, change, move, "
                "reschedule, update, make it instead). Examples: 'cancel that order', 'cancel my "
                "pickup', 'change my spa time to 6pm', 'move the pickup to Terminal 1'.\n"
                "1b) WHOLE vs PART. action=cancel withdraws the ENTIRE request — staff mark the "
                "whole ticket cancelled. Use it only when NOTHING of the request is left standing. "
                "If part of it survives the change, that is action=modify, and revised_request is "
                "the request as it now stands. Ask what is LEFT, and let that decide.\n"
                "2) is_edit=false for everything else, and when unsure. In particular a QUESTION "
                "asking for information (e.g. 'which restaurants are open now?', 'what time is "
                "breakfast?', 'do you have a spa?', 'what's on the menu?') is NEVER an edit even if "
                "it is about the same topic as a prior request. Also is_edit=false for a brand-new "
                "request and for ADDING a new item / dish / drink or PLACING a new order — EVEN IF "
                "the guest says 'add it to my order' (new items must be priced and placed through "
                "the ordering flow, never just noted onto an old ticket).\n"
                "2b) JUDGE ONLY THE CURRENT GUEST MESSAGE'S OWN INTENT — never carry cancel/edit "
                "intent forward from an earlier turn. The `last_assistant_message` having been about "
                "a cancellation or change does NOT make the current message an edit. A closing "
                "pleasantry or acknowledgement that carries no new request ('thanks', 'thank you', "
                "'ok', 'great', 'that's all', 'nothing else', 'bye', 'cheers'), and any question about "
                "a DIFFERENT topic (weather, timings, wifi, directions, facilities), are NEVER edits "
                "— set is_edit=false. Set is_edit=true ONLY when THIS message itself newly and "
                "explicitly asks to cancel or change one of the listed tickets.\n"
                "2c) A BARE AFFIRMATIVE OR ACKNOWLEDGEMENT IS NEVER AN EDIT. If the whole message "
                "is just agreement or politeness — 'yes', 'yes please', 'sure', 'ok', 'please do', "
                "'go ahead', 'do it', 'thanks', 'great' — it contains NO cancel and NO change "
                "instruction of its own, so is_edit=false ALWAYS, no matter what the tickets or "
                "`last_assistant_message` are about. The guest is agreeing to something the bot "
                "just offered; that is a new request for the normal flow to handle, not an edit "
                "of an old one. (Real failure this prevents: a guest said 'Yes, please' to an "
                "offer about late check-out and it was recorded as a change to an unrelated "
                "ticket raised 15 minutes earlier, so the new request was never actioned.)\n"
                "3) ticket_ref MUST be the exact `ref` of the ONE ticket the guest means. Work "
                "out WHICH REQUEST THEY MEAN, do not match on shared words — a guest who says "
                "'cancel it', 'cancel that', 'we don't need it anymore' is referring to a "
                "request by context, and when only ONE open ticket could plausibly be meant, "
                "that is the one. If it is genuinely ambiguous between several, or none "
                "matches, leave ticket_ref empty.\n"
                "3a) STOP AND CHECK `changeable` BEFORE YOU NAME ANY TICKET. A ticket whose "
                "changeable starts with 'no' is CLOSED. The kitchen is already cooking that food. "
                "For such a ticket you MUST answer is_edit=true, action=other, ticket_ref EMPTY. "
                "THERE IS NO EXCEPTION. It does not matter how clearly, how politely or how firmly "
                "the guest asks. 'Cancel my order', 'cancel it now', 'I do not want it any more', "
                "naming the exact dishes - none of these unlock it. A clear cancel request is not a "
                "reason to allow it; being too late is the entire point, and the guest cannot know "
                "the kitchen has started because they cannot see it.\n"
                "WHAT PUTTING THAT REF IN YOUR ANSWER ACTUALLY DOES: it cancels food that is already "
                "being cooked. The kitchen bins a finished dish, the guest is still charged for it, "
                "and staff read a cancellation nobody can honour. You are not being helpful by "
                "passing the request through - you are destroying a real meal.\n"
                "Your `reply` must warmly tell them this order is already being prepared so it can "
                "no longer be changed or cancelled, and offer to place a NEW order for anything else "
                "they would like. Never say you have noted a change you cannot make.\n"
                "YOU are the one who decides this, on every turn, by reading the `changeable` "
                "field of the ticket the guest means. When that ticket is locked, set "
                "is_locked_order_request=true in your answer, AND put that ticket's ref in "
                "`locked_ticket_ref` so we know which one you mean. Set the flag false in every "
                "other case - a normal cancel, a change to a ticket still open, a question, "
                "anything unrelated - and leave `locked_ticket_ref` empty. The flag is only ever "
                "about the ticket the guest is ACTUALLY asking about: if they are asking about a "
                "spa booking, a repair, housekeeping or anything that is not that locked order, "
                "it is false, no matter what else is open. That flag is how you tell us to give "
                "the guest the already-being-prepared message, so judge it from the conversation "
                "in front of you and nothing else.\n"
                "3c) A TICKET ONLY MATCHES IF IT IS ABOUT THE SAME THING. Before you put a ref in "
                "ticket_ref, read that ticket's `request`, `kind` and `summary` together and check "
                "they are about what the guest is actually cancelling or changing. `request` is the "
                "one-line request, `kind` is the service that raised it (e.g. 'spa & wellness "
                "booking', 'in-room dining'), `summary` is the staff-facing description. `category` "
                "is only the DEPARTMENT that will action it — 'Front Office' handles many unrelated "
                "things, so it tells you little about what the request is. Guests name the SERVICE, "
                "so `kind` is usually what their words line up with: a guest saying 'cancel the spa "
                "booking' means the ticket whose `kind` is the spa booking, even when its `request` "
                "line happens to name only the treatment. `order_no`, when it is above 0, is the "
                "number the guest was actually shown for a food order - 'my second order', 'order 2' "
                "and 'the first one' mean the ticket with that order_no, and two food orders can "
                "otherwise look nearly identical. BEING THE ONLY OPEN TICKET IS NOT A REASON TO "
                "MATCH IT. `requests_without_tickets` lists things the guest started this session "
                "that never became tickets — if what they name belongs to one of those, or to "
                "nothing you can see, leave ticket_ref EMPTY and say it is noted; there is no "
                "ticket to change and taking an unrelated one destroys work the guest still "
                "wants. Only when the guest names nothing at all ('cancel it', 'we don't need it "
                "anymore') may you fall back to the referent the conversation makes obvious.\n"
                "3b) match_confidence: 'high' when you are sure WHICH ticket they mean — they "
                "named it, or the context leaves only one sensible referent. 'low' when you are "
                "guessing, including when several open tickets could fit. Be honest: a cancel "
                "flips that exact ticket to cancelled for staff, so a wrong guess cancels the "
                "wrong request. When unsure, say low — we then only acknowledge and touch "
                "nothing.\n"
                "4) note: concise staff note describing the change, e.g. 'Guest requested "
                "cancellation of this order.' / 'Guest wants pickup moved to 6 PM.'\n"
                "4b) revised_request: ONLY for action=modify. Rewrite the matched ticket's "
                "`request` IN FULL with the change applied, as one self-contained line staff "
                "can action without reading anything else — e.g. ticket 'Airport pickup at 4 PM "
                "from Terminal 2' + 'move it to 6' → 'Airport pickup at 6 PM from Terminal 2'. "
                "Keep every detail the guest did NOT change. NEVER write a delta like 'change "
                "to 6 PM'. Leave it EMPTY for cancel and other. This line REPLACES what staff "
                "see as the request, so an incomplete rewrite loses details they need.\n"
                "5) reply: warm, brief, NO jargon (never say 'ticket'/'escalate'); tell them the "
                "team will take care of it. If is_edit is true but ticket_ref is empty, still "
                "reply that you've noted it for the team.\n"
                "6) NEVER invent a ref that is not present in `tickets`.\n"
                "\n"
                "=== LAST CHECK BEFORE YOU ANSWER (THIS OUTRANKS EVERYTHING ABOVE) ===\n"
                "Look at the ticket you were about to put in ticket_ref. Find that ticket in "
                "`tickets` and read its `changeable` field. If it starts with 'no', DELETE the ref "
                "from your answer right now: ticket_ref empty, action=other, and your reply tells the "
                "guest it is already being prepared. Do this even if every other rule above pointed "
                "you at that ticket, and even if the guest could not have been clearer. A locked "
                "ticket is NEVER returned in ticket_ref. Ever."
            )
            # WHAT THE GUEST STARTED BUT NEVER COMMITTED.
            # Until now the classifier was shown ONLY committed tickets, so a
            # request that never became one — a form opened and never submitted,
            # a flow parked mid-way — was invisible to it. Asked to cancel such a
            # thing it had nothing to match, and rule 3's "when only ONE open
            # ticket could plausibly be meant" handed it the only ticket in
            # sight. A guest who cancelled a spa booking they never submitted had
            # their AC repair cancelled instead, at high confidence.
            # Naming those requests gives the referent an honest home, so the
            # model can say "that one has no ticket" instead of taking someone
            # else's.
            _uncommitted: list[str] = []
            _seen_names: set[str] = set()
            for _row in (getattr(context, "suspended_services", None) or []):
                if not isinstance(_row, dict):
                    continue
                _nm = str(_row.get("service_name") or _row.get("service_id") or "").strip()
                if _nm and _nm.lower() not in _seen_names:
                    _seen_names.add(_nm.lower())
                    _uncommitted.append(_nm)
            _pd_raw = getattr(context, "pending_data", None)
            _pd_now = _pd_raw if isinstance(_pd_raw, dict) else {}
            _active_nm = str(_pd_now.get("_last_service_name") or "").strip()
            if _active_nm and _active_nm.lower() not in _seen_names:
                _uncommitted.append(_active_nm)

            payload = {
                "user_message": str(user_message or "").strip(),
                "last_assistant_message": str(last_assistant or "").strip(),
                "tickets": ticket_lines,
                "requests_without_tickets": _uncommitted,
            }
            _edit_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            # The hotel's own rules for this check, from its DB prompt row
            # `ticketing.edit_router_hotel_rules`, read last so they outrank the
            # in-house rules above. Hotels set up in the Pre-booking tab only (T24
            # onward); Zone, Mango and Sarovar keep the classifier they were tested
            # with.
            from services import prebooking_settings_service as _pbs_edit

            if _pbs_edit.is_admin_managed(
                str(getattr(context, "hotel_code", "") or "").strip().lower()
            ):
                _hotel_edit_rules = await self._db_prompt_text("ticketing.edit_router_hotel_rules")
                if _hotel_edit_rules:
                    _edit_messages.append({"role": "system", "content": _hotel_edit_rules})
            raw = await self._chat_with_json(
                messages=_edit_messages,
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=240,
                trace_context={"responder_type": "main", "agent": "ticket_edit_router"},
            )
            if not isinstance(raw, dict):
                return None
            # NEW-REQUEST OVERRIDE — this interceptor runs BEFORE the main router,
            # so anything it answers never reaches routing at all. That is correct
            # for a cancel/change of existing work, and wrong for a request that
            # asks to CREATE work: the message skipped the router's unsupported-city
            # decline AND the support service that opens the form, so the guest was
            # told their complaint had been "updated" while no ticket was raised and
            # the matched ticket's own text got rewritten. The model reports whether
            # this message asks for something new; we enforce that here by handing
            # the turn back to the normal flow.
            if bool(raw.get("is_new_request")):
                logger.info(
                    "[TICKET_EDIT] handing back to normal flow (is_new_request) session=%s msg=%r",
                    session_id, _msg_l[:60],
                )
                return None
            # UNVERIFIABLE NAME OVERRIDE — same reasoning as above, one step wider.
            # This classifier is shown the guest's words and their open tickets and
            # nothing else: no outlet list, no menu, no treatment list. So when the
            # change the guest wants is "make it THAT place / THAT dish instead", it
            # is being asked to approve a name it has no way to check, and it
            # approves whatever it is given. A guest with a table at SPR Kitchen
            # asked for "table at Kadak" — a restaurant that does not exist, which
            # the bot itself had declined twice earlier the same chat — and was told
            # it was noted, while the real booking was quietly rewritten underneath.
            #
            # The model reports when a message turns on a name like that; we hand
            # those turns back so the service that OWNS the list (and already
            # declines unknown outlets correctly) is the one that answers. A CANCEL
            # is exempt: it withdraws the request rather than substituting anything,
            # and dropping it here would risk it coming back as a new ticket.
            # The flag alone is not enough to hand back, because the risk is not
            # "a name was mentioned" — it is "a name we cannot check would be
            # WRITTEN onto the ticket". So ask the only question that matters:
            # does the rewritten request introduce anything that was not already
            # in the request we started from? Both strings come from the model,
            # so this compares its own work against itself rather than against
            # any list of ours — it holds for outlets, dishes, treatments and
            # room types alike, in any wording.
            #
            # Removing or reordering what is already there introduces nothing, so
            # it stays here and the ticket gets rewritten as before. Substituting
            # something new (a different outlet) does introduce something, and
            # that goes back to the service holding the real list.
            _names_flag = bool(raw.get("names_specific_place_or_item"))
            _proposed = str(raw.get("revised_request") or "").strip()
            _ref_now = str(raw.get("ticket_ref") or "").strip()
            _original = next(
                (t.get("request", "") for t in ticket_lines if t.get("ref") == _ref_now), ""
            )
            # A team-only hotel's ROOM-BOOKING ticket: the names a guest gives it are
            # room types, which the venue list below does not hold. Tapping the
            # "Premium City View" chip after "book a room" was matched as an edit of
            # that ticket, failed the venue lookup, and the guest was told "We do not
            # have a venue named 'Premium City View'" (Mango, 7 Oct, deployed too).
            _is_team_room_ticket = False
            try:
                from services.booking_assistance_state import team_booking_enabled as _ba_team_only

                _kind_now = next(
                    (t.get("kind", "") for t in ticket_lines if t.get("ref") == _ref_now), ""
                )
                _is_team_room_ticket = (
                    "roombooking" in re.sub(r"[^a-z]", "", str(_kind_now).lower())
                    and _ba_team_only(getattr(context, "hotel_code", ""))
                )
            except Exception:
                _is_team_room_ticket = False
            # ── WHAT THE ORDER HOLDS NOW, NOT WHAT IT HELD WHEN RAISED ───────
            # `request` is the text captured when the ticket was created, so a
            # dish added afterwards is invisible here. A guest added a Filter
            # Coffee to ticket 11174, asked to remove it a minute later, and was
            # told we have no venue by that name - the coffee was on the ticket
            # and on the order, just not in the sentence this compares against.
            if bool(getattr(settings, "ird_order_book_enabled", False)) and _ref_now:
                try:
                    from services import ird_cart_book as _bkm
                    from services import ird_cart_store as _bsm

                    _rowm = await _bsm.load(
                        hotel_code=str(getattr(context, "hotel_code", "") or ""),
                        session_id=str(getattr(context, "session_id", "") or ""),
                        create=False)
                    if _rowm:
                        for _slm in _bkm.slots_from_row(_rowm):
                            if str(_slm.get("ticket_ref") or "").strip() == _ref_now:
                                _original = (_original + " " + " ".join(
                                    str(i.get("item") or "")
                                    for i in (_slm.get("items") or []))).strip()
                                break
                except Exception as _om:
                    logger.warning("[TICKET_EDIT] could not read the order book for "
                                   "%s (non-fatal): %r", _ref_now, _om)
            _tokens = lambda s: {
                w for w in re.findall(r"[a-z0-9]+", str(s).lower()) if len(w) > 2
            }
            _introduces_new = True
            if _proposed and _original:
                _introduces_new = not _tokens(_proposed).issubset(_tokens(_original))
            # The guest's OWN word for a place or item, checked against the ticket
            # it is supposed to belong to. The model's revised_request cannot be
            # used for this: it had already replaced "kadak" with "SPR Kitchen",
            # so the substitution was invisible by the time it got here.
            _named = str(raw.get("named_place_or_item") or "").strip()
            _named_is_unknown = False
            _named_is_real_venue = False
            if _named and _original:
                _named_words = _tokens(_named)
                # Ignore words that carry no identity ("hotel", "restaurant") so
                # "the restaurant" is not mistaken for naming a new place.
                _generic = {
                    "hotel", "restaurant", "outlet", "bar", "lounge", "kitchen",
                    "table", "booking", "reservation", "room", "order", "the",
                }
                _named_words -= _generic
                # ── THE MODEL SAYS WHETHER THEY MEAN THIS ORDER; WE DON'T GUESS ─
                # Deciding this by comparing words was the bug. "are you sure the
                # kitchen knows about the 4th paratha?" gave
                # named_place_or_item="4th paratha"; the ticket said "3 x Lachha
                # Paratha", so a subset test failed on the word "4th" alone, the
                # phrase fell through to a venue lookup and the guest was told
                # "We do not have a place called '4th Paratha'" — about a dish in
                # their own order. Adding "4th" to the ignore-list would only
                # move the bug to "my second naan".
                #
                # The classifier already sees the message AND the ticket, so it
                # is asked directly and its answer is used. An ordinal, a
                # nickname or a shortened dish name is a language question, and
                # that is what the model is for.
                if bool(raw.get("named_refers_to_something_already_on_the_ticket")):
                    logger.info(
                        "[TICKET_EDIT] %r is something already on ticket %r — the "
                        "guest means their existing request, not a new place",
                        _named[:40], _ref_now,
                    )
                elif _named_words and not _named_words.issubset(_tokens(_original)):
                    # A room name on a room-booking ticket goes to the room agent,
                    # which holds the room list - never through the venue lookup.
                    # A cancel still runs below, so it can still cancel the ticket.
                    if _is_team_room_ticket and not str(
                        raw.get("action") or ""
                    ).strip().lower().startswith("cancel"):
                        logger.info(
                            "[TICKET_EDIT] %r on room-booking ticket %r - handing back to "
                            "the room agent (session=%s)", _named[:40], _ref_now, session_id,
                        )
                        return None
                    # Not the place on their ticket. That alone says nothing about
                    # whether it exists - so ask the list. A venue we really have
                    # is a legitimate MOVE and is edited like any other change;
                    # only a name we do not have is refused.
                    _venues = await self._known_venue_names(
                        str(getattr(context, "hotel_code", "") or "")
                    )
                    _is_real_venue = any(
                        _named_words.issubset(_tokens(v)) for v in _venues
                    )
                    if not _is_real_venue and _venues:
                        # The reply names the alternatives, and it was naming every
                        # space in the KB - Summit, Senate, Board Room - as if a guest
                        # could book dinner in a meeting room. Only the outlets that
                        # actually take table reservations are offered.
                        try:
                            context.pending_data["_real_dining_outlets"] = sorted(_venues)
                        except Exception:
                            pass
                    if _is_real_venue:
                        _named_is_real_venue = True
                        logger.info(
                            "[TICKET_EDIT] %r is one of our venues — treating this as a "
                            "move of the existing booking, not a new one", _named[:40],
                        )
                    else:
                        _named_is_unknown = True
            if _named_is_unknown and not str(
                raw.get("action") or ""
            ).strip().lower().startswith("cancel"):
                logger.info(
                    "[TICKET_EDIT] guest named %r, which is nowhere on ticket %r — "
                    "handing back so the service holding the real list can answer "
                    "(session=%s msg=%r)",
                    _named[:40], _ref_now, session_id, _msg_l[:60],
                )
                # Carry the name forward. Handing back without it produced "I
                # can't change hotel timings from here" - a refusal about our
                # own limits that leaves the guest still believing the place
                # exists. They need to hear that it does not.
                try:
                    if isinstance(getattr(context, "pending_data", None), dict):
                        context.pending_data["_unverified_place_named"] = _named[:60]
                except Exception:
                    pass
                return None
            if (
                _names_flag
                and _introduces_new
                # A venue checked against our own outlet list is not unverifiable.
                # Without this the older guard still handed the turn away, the
                # reservation service opened a form, and the guest ended up with a
                # second booking while the first stayed live - the very thing the
                # lookup exists to prevent.
                and not _named_is_real_venue
                and not str(raw.get("action") or "").strip().lower().startswith("cancel")
            ):
                logger.info(
                    "[TICKET_EDIT] handing back to normal flow (unverifiable name would be "
                    "written) session=%s msg=%r",
                    session_id, _msg_l[:60],
                )
                return None
            # A LOCKED ORDER IS ANSWERED HERE, BEFORE THE is_edit GATE.
            # Being told an order can no longer be changed is a real outcome in
            # its own right, and it does not depend on the turn being classified
            # an "edit". The model routinely reports the lock while calling
            # is_edit=false (it is refusing, not editing), and this gate then
            # returned None first — so a guest asking to cancel food the kitchen
            # had already started fell through to the normal flow and was never
            # told why. The lock is decided from `locked_ticket_ref` verified
            # against the set we computed, so it is safe to act on this early.
            # ── YOU CANNOT BE ASKING ABOUT A LOCKED ORDER IF YOU ARE NOT ────
            # ── ASKING TO CHANGE ANYTHING ──────────────────────────────────
            # 22% of this classifier's 13k-char prompt is a warning about locked
            # food orders, and once one exists it sees them everywhere: asked
            # "what time is checkout?", "i need extra towels" and "thanks, that's
            # all for now!", it set is_locked_order_request=true on all three, at
            # temperature 0. Every one of those had is_edit=false; the one turn
            # that genuinely WAS about the locked order had is_edit=true.
            # So the model's own two fields settle it — this contradicts nothing
            # it decided, it just refuses to let half its answer outvote the
            # other half.
            if bool(raw.get("is_locked_order_request")) and not bool(raw.get("is_edit")):
                logger.info(
                    "[TICKET_EDIT] locked flag dropped — the model also said this is "
                    "not an edit, so it cannot be about a locked order (msg=%r)",
                    _msg_l[:60],
                )
                raw["is_locked_order_request"] = False
                raw["locked_ticket_ref"] = ""
            _lref = str(raw.get("locked_ticket_ref") or "").strip()
            _locked_cleared = False
            # A guest saying "yes" to a question the bot just asked is not asking to
            # change or cancel anything. The judge below deliberately reads a bare
            # affirmative as "about this ticket" so that "cancel it" keeps working —
            # which meant a plain "yes", answering a transport form, was answered
            # "that order is already being prepared". So before the locked path may
            # claim a turn at all, require that the guest is actually asking to
            # change or cancel something.
            if bool(raw.get("is_locked_order_request")) and _lref in _locked_refs:
                if await self._guest_is_asking_to_change_or_cancel(
                    user_message=user_message,
                    previous_bot_message=str(last_assistant or ""),
                ) is False:
                    logger.info(
                        "[TICKET_EDIT] locked flag ignored - the guest is answering the "
                        "bot's own question, not asking to change anything (msg=%r)",
                        _msg_l[:60],
                    )
                    raw["is_locked_order_request"] = False
                    _locked_cleared = True
                    _lref = ""
            if bool(raw.get("is_locked_order_request")) and _lref in _locked_refs:
                # Verifying the ticket is locked is not the same as verifying the
                # guest meant it. The model named a locked FOOD order while the
                # guest was cancelling a SPA booking, and they were told their
                # order was already being prepared. Nothing was damaged - the
                # words were simply about someone else's request. Same question,
                # same judge as the cancel path below.
                _lreq = next(
                    (self._ticket_subject(t) for t in ticket_lines if t.get("ref") == _lref), ""
                )
                if await self._cancel_target_is_the_same_thing(
                    user_message=user_message, ticket_request=_lreq,
                ) is False:
                    logger.info(
                        "[TICKET_EDIT] model claimed locked ticket %s but the guest is asking "
                        "about something else - not using the already-being-prepared reply",
                        _lref,
                    )
                    raw["is_locked_order_request"] = False
                    _locked_cleared = True
                    _lref = ""
            # ── AND AN UNNAMED CANCEL DOES NOT GET THIS ANSWER EITHER ────────
            # "That order is already being prepared" is still a confident answer
            # about ONE guessed ticket. With a spa booking, housekeeping, a
            # repair and a laundry pickup all open, a bare "actually cancel it"
            # was answered about the food order purely because the food order
            # happened to be the locked one. The guest never said which. The
            # question below gets asked first; only a cancel that names its
            # target reaches the locked reply.
            if (bool(raw.get("is_locked_order_request")) and _lref in _locked_refs
                    and len(valid_refs) > 1):
                if not await self._cancel_names_what_it_means(user_message=user_message):
                    logger.info(
                        "[TICKET_EDIT] locked ref %s was the model's guess for an "
                        "UNNAMED cancel with %d requests open — asking which one "
                        "instead of answering about that order",
                        _lref, len(valid_refs),
                    )
                    raw["is_locked_order_request"] = False
                    _locked_cleared = True
                    _lref = ""
            if bool(raw.get("is_locked_order_request")) and _lref in _locked_refs:
                logger.info(
                    "[TICKET_EDIT] locked order named by the model (ref=%s) session=%s msg=%r",
                    _lref, session_id, _msg_l[:60],
                )
                return OrchestrationDecision(
                    intent="ticket_edit",
                    action="respond_only",
                    response_text=(
                        "That order is already being prepared, so it can no longer be "
                        "changed or cancelled. I'd be happy to place a new order for "
                        "anything else you'd like."
                    ),
                    confidence=0.9,
                    metadata={
                        "ticket_edit_handled": True,
                        "ticket_edit_action": "locked_refused",
                        "ticket_edit_locked_ref": str(raw.get("locked_ticket_ref") or "").strip(),
                    },
                )
            if not bool(raw.get("is_edit")):
                return None

            reply = str(raw.get("reply") or "").strip() or (
                "I've noted that for our team — they'll take care of it shortly."
            )
            ref = str(raw.get("ticket_ref") or "").strip()
            # The open ticket the classifier itself matched, before any check below
            # drops it. Read at the end: a dropped match means a real request may
            # still be open, so nothing may be claimed (Pre-booking-tab hotels).
            _router_ref = ref if ref in valid_refs else ""
            note = str(raw.get("note") or "").strip() or "Guest requested a change to this request."
            # A cancel is recognised by INTENT, not by an exact label. The model
            # routinely writes "cancelled" / "cancellation" / "cancel_request"
            # here, and an equality test read those as "not a cancel": the
            # cancellation note was appended to the ticket but its status was
            # never flipped, so the request stayed OPEN on both the KePSLA and
            # the external dashboard while the guest had been told it was done.
            _action_raw = str(raw.get("action") or "").strip().lower()
            _is_cancel = _action_raw.startswith("cancel")
            # THE MODEL'S OWN TWO FIELDS CONTRADICT EACH OTHER — TAKE THE
            # NON-DESTRUCTIVE READING.
            # `revised_request` is what the request should SAY once the change is
            # applied, so it is only ever written when something REMAINS. A cancel
            # withdraws the request entirely, leaving nothing to say. When both
            # arrive together the model has worked out a PARTIAL change and then
            # labelled it a TOTAL one, and only one of those readings can be
            # undone: a wrong cancel marks the whole ticket cancelled for staff,
            # while a wrong modify leaves work standing that the guest can still
            # cancel. So the surviving reading wins.
            #
            # This is deliberately about the fields, not about words or items — it
            # holds for any service, any wording and any language, which a rule
            # naming dishes or phrasings would not.
            #
            # (Real failure: an order of 1 Tandoori Murgh, 2 Garlic Naan and 1
            # Tiramisu, and "actually cancel the tiramisu". The model returned the
            # correct remaining two-item line in revised_request AND action=cancel.
            # The whole dinner was cancelled and the correct line discarded.)
            if _is_cancel and str(raw.get("revised_request") or "").strip():
                logger.info(
                    "[TICKET_EDIT] action=cancel arrived with a revised_request — "
                    "partial change, not a withdrawal; treating as modify (session=%s)",
                    session_id,
                )
                _is_cancel = False
                _action_raw = "modify"
            # A CHANGE THAT SAYS NOTHING TO CHANGE (Mango). "i want to modify booking
            # details" names no new date, time, number or room, yet it was matched to
            # an open ticket, a note was written on it and the guest was told "I have
            # noted your request ... the team will assist you shortly" (Mango, 9 Oct).
            # The model reports whether the message names the change; when it does
            # not, nothing is written and the guest is asked what to change.
            if _team_only_edit and not _is_cancel and raw.get("names_the_change") is False:
                _what = (
                    next((self._ticket_label(t) for t in ticket_lines if t.get("ref") == ref), "")
                    if ref in valid_refs else ""
                )
                logger.info(
                    "[TICKET_EDIT] modify names no change - asking what to change, touching "
                    "nothing (ref=%r session=%s msg=%r)", ref, session_id, _msg_l[:60],
                )
                return OrchestrationDecision(
                    intent="ticket_edit",
                    action="respond_only",
                    response_text=(
                        "Sure — what would you like to change"
                        + (f" in your request for {_what}" if _what else "")
                        + "?"
                    ),
                    confidence=0.9,
                    metadata={
                        "ticket_edit_handled": True,
                        "ticket_edit_action": "ask_what_to_change",
                        "ticket_edit_ref": ref if ref in valid_refs else "",
                    },
                )
            # SANITY GUARD: the classifier can attach an edit to the wrong ticket
            # (e.g. "cancel the spa" matched to an "AC not cooling" ticket), and a
            # cancel flips that exact ticket to CANCELLED — so a wrong match cancels
            # the WRONG request. The guard used to require a shared 4+ letter word
            # between the message and the ticket, which failed the ordinary way
            # guests actually cancel: "can u cancel it i flight got cancelled" names
            # nothing, so the correct ticket was dropped and nothing was cancelled.
            # Referring to a request by context is normal language, not a mismatch.
            # So the model now reports how sure it is which ticket is meant, and we
            # act on that instead: a low-confidence match touches no ticket and only
            # acknowledges.
            if ref and ref in valid_refs:
                if str(raw.get("match_confidence") or "").strip().lower() == "low":
                    _matched_req = next(
                        (t.get("request", "") for t in ticket_lines if t.get("ref") == ref), ""
                    )
                    logger.info(
                        "[TICKET_EDIT] dropped ref=%s (match_confidence=low: ticket=%r msg=%r)",
                        ref, _matched_req[:50], _msg_l[:50],
                    )
                    ref = ""
            # Asked BEFORE the locked-order branch below on purpose: that branch
            # answers "that order is already being prepared" about whichever
            # ticket was guessed, so a bare "cancel it" with a spa booking and
            # three other requests open still got a confident answer about the
            # food. Whether the guess is cancellable or locked, an unnamed cancel
            # is a question first.
            # ── AN UNNAMED CANCEL NEVER PICKS A TICKET BY ITSELF ─────────────
            # This only ran when a form was open, so with no form the same bare
            # "actually cancel it" went straight to whichever ticket the
            # classifier had guessed - and cancelled a guest's tea while they
            # meant their table booking. They were never told what went: the
            # confirmation is one fixed line that names nothing. With several
            # requests open, an unnamed cancel is a question to ask, not a
            # ticket to close.
            if _is_cancel and ref:
                if not await self._cancel_names_what_it_means(user_message=user_message):
                    _open_now = [t for t in ticket_lines if t.get("ref") in valid_refs]
                    if str(getattr(context, "pending_action", "") or "").strip():
                        logger.info(
                            "[TICKET_EDIT] unnamed cancel (%r) while %r is open — not "
                            "touching ticket %s; handing back to the normal flow",
                            _msg_l[:40],
                            str(getattr(context, "pending_action", ""))[:30], ref)
                        return None
                    if len(_open_now) > 1:
                        _menu = "\n".join(
                            "• %s" % self._ticket_label(t) for t in _open_now[:6]
                            if self._ticket_label(t)
                        )
                        logger.info(
                            "[TICKET_EDIT] unnamed cancel (%r) with %d open requests "
                            "— asking which one instead of guessing %s",
                            _msg_l[:40], len(_open_now), ref)
                        # Kept for the guest's answer (Pre-booking-tab hotels).
                        if _pbs_edit.is_admin_managed(
                            str(getattr(context, "hotel_code", "") or "").strip().lower()
                        ) and isinstance(context.pending_data, dict):
                            context.pending_data["_cancel_menu"] = self._cancel_menu_rows(
                                [t for t in _open_now[:6] if self._ticket_label(t)]
                            )
                        return OrchestrationDecision(
                            intent="ticket_edit",
                            action="respond_only",
                            response_text=(
                                "Which one would you like me to cancel?\n%s" % _menu
                            ),
                            confidence=0.9,
                            metadata={
                                "ticket_edit_handled": True,
                                "ticket_edit_action": "cancel_needs_target",
                            },
                        )
            # ── AN ORDER THE KITCHEN HAS STARTED IS OFF LIMITS ──────────────
            # `_locked_refs` is worked out from the ticket's own age and live
            # status, so by here we already KNOW whether this order can still be
            # touched. Until now that knowledge was only handed to the model as
            # advice, and the model wrote the guest's reply — which went wrong in
            # both directions: it once cancelled a 25-minute-old order outright,
            # and once correctly left the order alone while telling the guest
            # "Understood - I've cancelled the order." The order survived; the
            # guest stopped expecting their food and was charged for it anyway.
            #
            # So the code answers this one itself. It refuses to touch a locked
            # ticket, and it writes the sentence, because a message that
            # contradicts what actually happened is the whole bug we keep hitting.
            # The model still decides WHICH ticket the guest means; it just does
            # not get to narrate the outcome.
            _locked_reply = (
                "That order is already being prepared, so it can no longer be "
                "changed or cancelled. I'd be happy to place a new order for "
                "anything else you'd like."
            )
            # WHEN this applies is the MODEL'S call, not a rule of ours. It reads
            # each ticket's `changeable` field and reports, in its own output,
            # whether the guest is asking about one that is locked.
            #
            # We deliberately do NOT infer it from "a locked ticket exists and this
            # looks like a cancel" — that guess fires on an unrelated request and
            # tells a guest their food is being prepared when they were asking about
            # something else entirely.
            #
            # Code owns only the SENTENCE, because the model proved it will make the
            # right call and then describe it wrongly: it correctly left a locked
            # order alone while telling the guest "Understood - I've cancelled the
            # order." The order survived; the guest stopped expecting their food.
            # The flag is the model's judgement, but it is only meaningful when a
            # ticket is actually locked. `_locked_refs` can only ever hold in-room
            # dining, so if it is empty nothing here is lockable and the flag is a
            # mistake - a spa or housekeeping cancel must never be answered with
            # "your order is already being prepared". This checks a fact we computed
            # ourselves; it does not second-guess WHICH ticket the guest meant.
            # ...and "a locked ticket exists somewhere" is NOT that check. It fired
            # on "actually cancel the spa booking" purely because a food order
            # raised 15 minutes earlier had passed the kitchen window, and the
            # guest — asking about a SPA booking — was told their order was
            # already being prepared. Nothing was cancelled, so the damage was
            # only the words, but the words were nonsense.
            #
            # So the model now names WHICH ticket it means, and we check that
            # exact ticket is one we computed as locked. The model judges what the
            # guest is referring to; the code verifies that referent against a
            # fact it worked out itself, and neither is trusted alone.
            _locked_claim = str(raw.get("locked_ticket_ref") or "").strip()
            # is_locked_order_request may have been cleared above when the judge
            # found the guest was asking about a different request entirely.
            _model_says_locked = (
                bool(raw.get("is_locked_order_request")) and _locked_claim in _locked_refs
            )
            # A ref that is itself locked contradicts that judgement — acting on it
            # would cancel food already being cooked, so it is read as locked too.
            _hit_locked = bool(ref) and ref in _locked_refs
            # ── IS THE GUEST EVEN TALKING ABOUT THIS TICKET? ────────────────
            # `_model_says_locked` is verified further up by
            # _cancel_target_is_the_same_thing. `_hit_locked` was not verified by
            # anything: the classifier simply returned a ticket_ref, and if that
            # ref happened to be a locked food order the guest got "that order is
            # already being prepared" — whatever they had actually asked for.
            # Seen live on "i need extra towels", which the classifier called an
            # edit and matched to the food order: the guest was told their meal
            # could not be changed and no housekeeping ticket was ever raised.
            # Same question, same judge, so an unrelated request stops borrowing
            # a locked order's answer.
            if _hit_locked and not _model_says_locked:
                _hit_req = next(
                    (self._ticket_subject(t) for t in ticket_lines if t.get("ref") == ref), ""
                )
                if await self._cancel_target_is_the_same_thing(
                    user_message=user_message, ticket_request=_hit_req,
                ) is False:
                    logger.info(
                        "[TICKET_EDIT] classifier returned locked ticket %s but the guest is "
                        "asking about something else (%r) — handing back so the right team "
                        "answers", ref, _msg_l[:60],
                    )
                    return None
            if _model_says_locked or _hit_locked:
                logger.info(
                    "[TICKET_EDIT] locked order - model_flag=%s returned_locked_ref=%s msg=%r",
                    _model_says_locked, _hit_locked, _msg_l[:60],
                )
                return OrchestrationDecision(
                    intent="ticket_edit",
                    action="respond_only",
                    response_text=_locked_reply,
                    confidence=0.9,
                    metadata={
                        "ticket_edit_handled": True,
                        "ticket_edit_action": "locked_refused",
                        "ticket_edit_locked_ref": ref or "",
                    },
                )
            # ── A CANCEL IS DESTRUCTIVE, SO ITS TARGET GETS A SECOND OPINION ──
            # Flipping a ticket to cancelled is the one action here staff act on
            # irreversibly, and the omnibus classifier is not reliable enough to
            # be the only judge of WHICH ticket. On identical input it has both
            # correctly declined and, minutes later, matched "actually cancel the
            # spa booking" to an "AC not cooling" repair at high confidence —
            # cancelling a repair the guest still wanted, because it was the only
            # open ticket in sight.
            #
            # So ask a separate, single-question judge that sees only this message
            # and this one ticket, with no other tickets to be tempted by and no
            # route to defend. This is the same shape as
            # `_subject_is_in_stay_only`: a narrow binary judgement is far steadier
            # than one field inside a large multi-field answer, and it stays a
            # judgement — no word lists, no per-service rules. Only runs on
            # cancels, so ordinary edits pay nothing.
            # ── AN UNNAMED CANCEL CANNOT REACH PAST WHAT IS ON SCREEN ────────
            # "actually cancel that" points at whatever the guest was last
            # dealing with. With a form open in front of them, one such message
            # cancelled their FOOD ORDER instead - the matcher is told to accept
            # anything unnamed, so it rubber-stamped whichever ticket the code
            # had already picked - and the rest of that same message, a spa
            # booking, was never answered at all.
            #
            # So when the guest is mid-form and did not say WHAT to cancel, this
            # interceptor keeps its hands off tickets and hands the turn back:
            # the normal flow closes the form and answers the rest of what they
            # said. A cancel that names its target is untouched by this.
            if _is_cancel and ref and ref in valid_refs:
                _target_req = next(
                    (self._ticket_subject(t) for t in ticket_lines if t.get("ref") == ref), ""
                )
                _same = await self._cancel_target_is_the_same_thing(
                    user_message=user_message, ticket_request=_target_req,
                )
                if _same is False:
                    logger.info(
                        "[TICKET_EDIT] second opinion says this cancel is NOT about ticket %s "
                        "(%r vs %r) — touching nothing",
                        ref, _msg_l[:40], str(_target_req)[:40],
                    )
                    ref = ""
            handled_ref = ""
            if ref and ref in valid_refs:
                # On a CANCEL we never raise a new ticket — we append a clear
                # cancellation note to the EXISTING ticket AND flip its status to
                # "cancel". append_ticket_comment(set_status=...) writes
                # new_bot_tickets.status (what the dashboard shows) and best-effort
                # PATCHes the external ticketing API, so the request reads as
                # cancelled instead of leaving a stale open ticket.
                _note = (f"CANCELLED: {note}" if _is_cancel else note)
                # A MODIFY must rewrite what the ticket SAYS, not just add a note
                # under it — staff read the request line, so an edit that only
                # appended a note left the old (wrong) time/place showing as the
                # live request on both dashboards. set_issue feeds the external
                # API's `issue` field, set_description feeds KePSLA's.
                _revised = "" if _is_cancel else str(raw.get("revised_request") or "").strip()
                # ── A TICKET THE GUEST FILLED IN IS NOT A SENTENCE TO REWRITE ─
                # `revised_request` REPLACES the request line outright, which is
                # right for a request the model itself worded. A form ticket is
                # not that: its line is rendered from fields the guest typed
                # (pickup point, date, time, flight number). Asked to add two
                # guests to a room booking, the classifier matched the only other
                # open ticket - an airport pickup - at "high" confidence and
                # rewrote it to "Room booking for 3 guests in room 712.", and
                # every pickup detail was gone. A modify may still add its note,
                # which is how staff see the change; it may not overwrite what
                # the guest entered.
                if _revised:
                    _matched_line = next(
                        (str(t.get("request") or "") for t in ticket_lines
                         if t.get("ref") == ref), "")
                    # The form route records the refs it creates, so this is a
                    # fact we already hold. The shape check behind it only
                    # covers tickets raised before that record existed.
                    _form_refs = set()
                    try:
                        _form_refs = {
                            str(x) for x in
                            ((context.pending_data or {}).get(
                                "_form_built_ticket_refs") or [])
                        }
                    except Exception:
                        _form_refs = set()
                    if str(ref) in _form_refs or self._looks_like_form_ticket(_matched_line):
                        logger.info(
                            "[TICKET_EDIT] ticket %s was built from a form — keeping "
                            "the guest's own details and recording the change as a "
                            "note instead of rewriting %r",
                            ref, _matched_line[:60],
                        )
                        _revised = ""
                res = await ticketing_service.append_ticket_comment(
                    session_id=session_id, ticket_ref=ref, note=_note,
                    set_status=("cancel" if _is_cancel else ""),
                    set_issue=_revised,
                    set_description=_revised,
                )
                if getattr(res, "success", False):
                    handled_ref = ref
                    # ── A REPLY WRITTEN UNDER A PREMISE WE REJECTED ─────────
                    # The classifier writes the guest's reply in the SAME answer
                    # that guesses which ticket they mean, so a wrong guess
                    # becomes a wrong sentence. When a judge above threw the
                    # locked-order premise out, that sentence is still sitting
                    # here talking about food being prepared. There is already a
                    # guard for this, but it only runs when NOTHING matched — so
                    # a guest who successfully moved their table booking was told
                    # "your in-room dining order is already being prepared and
                    # cannot be changed", about food they had not mentioned.
                    # What actually happened is known here; say that instead.
                    if _locked_cleared and not _is_cancel:
                        _subject = next(
                            (self._ticket_subject(t) for t in ticket_lines
                             if t.get("ref") == ref), "",
                        )
                        logger.info(
                            "[TICKET_EDIT] replacing a reply written under the rejected "
                            "locked-order premise — the edit to %s succeeded", ref,
                        )
                        reply = (
                            "Done — I've updated that for you"
                            + (f": {_subject}." if _subject else ".")
                        )
                    # A cancel is terminal — remember it so this ticket is never
                    # re-offered to the interceptor on later turns.
                    if _is_cancel:
                        ticketing_service.mark_ticket_cancelled(session_id, ref)
                        _cancelled_line = next((t for t in ticket_lines if t.get("ref") == ref), {})
                        self._forget_cancelled_booking(
                            context, ref, self._ticket_label(_cancelled_line),
                            str(_cancelled_line.get("kind") or ""),
                        )
                        # ── THE CODE OWNS THE SENTENCE ON A CANCEL ───────────
                        # Same lesson as the locked path below: the model makes
                        # the right CALL and then describes it wrongly. Here it
                        # emitted action=cancel — correctly — and wrote "your
                        # order is already being prepared, so it can no longer be
                        # changed or cancelled", which this code had just done.
                        # The guest was told their food was still coming while the
                        # kitchen ticket read `cancelled`: the two must never
                        # disagree. What happened is known for certain at this
                        # point, so it is stated here rather than taken on trust
                        # from a `reply` written before the outcome existed.
                        # ── NAME WHAT WENT ──────────────────────────────────
                        # This said only "that request", so a guest whose tea was
                        # cancelled by mistake had no way to notice: the sentence
                        # reads identically whichever ticket was closed. Saying
                        # the subject out loud is what lets them catch a wrong
                        # target on the very next turn.
                        _cancelled_subject = next(
                            (self._ticket_label(t) for t in ticket_lines
                             if t.get("ref") == ref), "",
                        )
                        # No ".." after a request line that ends in a full stop
                        # (Pre-booking-tab hotels; the others keep their text).
                        if _pbs_edit.is_admin_managed(
                            str(getattr(context, "hotel_code", "") or "").strip().lower()
                        ):
                            _cancelled_subject = str(_cancelled_subject or "").rstrip(" .")
                        reply = (
                            "Done — I've cancelled "
                            + (f"your request for {_cancelled_subject}."
                               if _cancelled_subject else "that request for you.")
                            + " Is there anything else I can help with?"
                        )
                        # ── AND THE PLACED-ORDER LEDGER FOLLOWS THE TICKET ──
                        # The cart is cleared below, but the durable ledger that
                        # `food_order_status` and every recall answer are built
                        # from is a separate store. Leaving the cancelled order in
                        # it meant "what did I order", asked straight after a
                        # successful cancellation, read the food back as
                        # `already_sent_to_the_kitchen` with a total and
                        # `can_still_be_changed: true` — while the ticket itself
                        # said `cancelled`. The guest sat waiting for a meal
                        # nobody was cooking. Pruned here rather than with the
                        # cart because the cart may already be empty, and this
                        # must happen either way.
                        try:
                            _pd_led = (
                                context.pending_data
                                if isinstance(getattr(context, "pending_data", None), dict)
                                else None
                            )
                            if _pd_led is not None:
                                _ref_s = str(ref).strip()
                                _led = [
                                    r for r in (_pd_led.get("_ird_placed_orders") or [])
                                    if isinstance(r, dict)
                                ]
                                _kept = [
                                    r for r in _led
                                    if str(r.get("ticket_ref") or "").strip() != _ref_s
                                ]
                                if len(_kept) != len(_led):
                                    _pd_led["_ird_placed_orders"] = _kept
                                    logger.info(
                                        "[TICKET_EDIT] cancelled order %s dropped from the "
                                        "placed-order ledger (%d -> %d) so recall stops "
                                        "reporting it as with the kitchen",
                                        _ref_s, len(_led), len(_kept),
                                    )
                                # Only the CANCELLED order's anchors go. Another
                                # order may still be live and must be left alone.
                                if str(_pd_led.get("_ird_order_ticket_ref") or "").strip() == _ref_s:
                                    _pd_led.pop("_ird_order_ticket_ref", None)
                                    _pd_led.pop("_ird_open_order_items", None)

                            # ── AND THE DURABLE FOOD PROFILE ────────────────
                            # `food_profile.latest_order` / `order_history` are
                            # memory facts, a THIRD store beyond the cart and the
                            # ledger, and they are what recall actually reads.
                            # Left alone they kept `status: "placed"` forever, so
                            # "what did I order" answered with the cancelled meal
                            # while food_order_status was correctly empty. The
                            # order is marked cancelled rather than deleted, so
                            # history survives, and it stops being the LATEST
                            # order because there is no longer an order in flight.
                            from services.conversation_memory_service import (
                                conversation_memory_service as _cms_c,
                            )
                            _mem_c = _cms_c.ensure_memory(context)
                            _facts_c = _mem_c.setdefault("facts", {})
                            _fh_c = _mem_c.setdefault("fact_history", [])

                            def _same_order(rec: Any) -> bool:
                                return (
                                    isinstance(rec, dict)
                                    and str(rec.get("ticket_ref") or "").strip() == _ref_s
                                )

                            _hist_c = [
                                ({**r, "status": "cancelled"} if _same_order(r) else r)
                                for r in (_facts_c.get("order_history") or [])
                            ]
                            _cms_c._set_fact(
                                facts=_facts_c, fact_history=_fh_c, key="order_history",
                                value=_hist_c, source_message="[ird_cancelled]",
                                change_type="set",
                            )
                            if _same_order(_facts_c.get("latest_order")):
                                _cms_c._set_fact(
                                    facts=_facts_c, fact_history=_fh_c,
                                    key="latest_order", value={},
                                    source_message="[ird_cancelled]", change_type="set",
                                )
                                logger.info(
                                    "[TICKET_EDIT] cancelled order %s cleared from the "
                                    "durable food profile so recall stops offering it",
                                    _ref_s,
                                )
                        except Exception as _led_exc:
                            logger.warning(
                                "[TICKET_EDIT] could not prune cancelled order %s from the "
                                "placed-order ledger (non-fatal): %r", ref, _led_exc,
                            )
                    # ── THE CART FOLLOWS THE TICKET ─────────────────────────
                    # The ticket is what the kitchen will cook. Leaving the cart
                    # behind left the guest looking at a dessert they had just
                    # cancelled, and a total ₹500 higher than the order that was
                    # actually placed. Whatever the ticket now says, the guest is
                    # shown the same thing.
                    try:
                        _pd_cart = (
                            context.pending_data
                            if isinstance(getattr(context, "pending_data", None), dict)
                            else None
                        )
                        _cart_now = list((_pd_cart or {}).get("_ird_cart") or [])
                        if _pd_cart is not None and _cart_now:
                            if _is_cancel:
                                _pd_cart["_ird_cart"] = []
                                _pd_cart.pop("_ird_vetted_items", None)
                                logger.info(
                                    "[TICKET_EDIT] order cancelled — cart emptied so the "
                                    "guest is not shown food nobody is cooking"
                                )
                            elif _revised:
                                _survivors = await self._cart_lines_that_survive_an_edit(
                                    cart=_cart_now, revised_request=_revised,
                                )
                                if _survivors is not None:
                                    _pd_cart["_ird_cart"] = _survivors
                                    _pd_cart["_ird_vetted_items"] = sorted(
                                        {str(l.get("item")).strip() for l in _survivors
                                         if str(l.get("item") or "").strip()}
                                    )
                                    logger.info(
                                        "[TICKET_EDIT] cart follows the ticket: %s -> %s",
                                        [str(l.get("item")) for l in _cart_now],
                                        [str(l.get("item")) for l in _survivors],
                                    )
                                    # The cart was the ONLY store being corrected.
                                    # The ledger and the durable food profile are
                                    # what recall actually reads, so they have to
                                    # follow the same ticket or the guest is told
                                    # a dish they cancelled is still on its way.
                                    self._ird_rewrite_order_stores(
                                        context=context, ticket_ref=ref,
                                        lines=_survivors,
                                    )
                    except Exception as _sync_exc:
                        logger.warning(
                            "[TICKET_EDIT] cart sync skipped (non-fatal): %r", _sync_exc
                        )
            logger.info(
                "[TICKET_EDIT] session=%s action=%s matched_ref=%s (of %d tickets)",
                session_id, str(raw.get("action") or ""), handled_ref or "-", len(ticket_lines),
            )
            # ── THE BOOK FOLLOWS THE EDIT, OR THERE ARE TWO WRITERS ──────────
            # This path rewrites the ticket itself. The cart-sync above cannot
            # help: it keys off `_ird_cart`, which the book deliberately leaves
            # empty once an order is placed - so "remove one chicken burger"
            # produced a ticket reading 1 burger and a book still reading 2. The
            # guest, the book and the kitchen disagreeing is the exact failure
            # the book exists to end, so the slot is corrected from its own
            # contents here.
            if (handled_ref and bool(getattr(settings, "ird_order_book_enabled", False))
                    and self._is_sarovar_ird(context, "in_room_dining")):
                try:
                    from services import ird_cart_book as _bke
                    from services import ird_cart_store as _bse

                    _hce = str(getattr(context, "hotel_code", "") or "")
                    _side = str(getattr(context, "session_id", "") or "")
                    _rowe = await _bse.load(hotel_code=_hce, session_id=_side, create=False)
                    if _rowe:
                        _slotse = _bke.slots_from_row(_rowe)
                        _mine = [sl for sl in _slotse
                                 if str(sl.get("ticket_ref") or "").strip()
                                 == str(handled_ref).strip()]
                        if _mine and _mine[0].get("items"):
                            _t = _mine[0]
                            if str(raw.get("action") or "").strip().lower() == "cancel":
                                _t["status"] = _bke.CANCELLED
                                _kept = _t["items"]
                            else:
                                _kept = await self._cart_lines_that_survive_an_edit(
                                    cart=list(_t["items"]),
                                    revised_request=str(raw.get("revised_request") or "")
                                    or str(user_message or ""),
                                )
                            if _kept is not None and _kept != _t["items"]:
                                _before = [i["item"] for i in _t["items"]]
                                _t["items"] = _kept
                                await _bse.save_slots(hotel_code=_hce, session_id=_side,
                                                      slots=[_t])
                                _bke.project_to_pending_data(_slotse, context.pending_data)
                                logger.info(
                                    "[IRD_BOOK] slot %d follows the ticket edit on %s: "
                                    "%s -> %s", _t["n"], handled_ref, _before,
                                    [i["item"] for i in _kept])
                            elif str(raw.get("action") or "").strip().lower() == "cancel":
                                await _bse.save_slots(hotel_code=_hce, session_id=_side,
                                                      slots=[_t])
                                _bke.project_to_pending_data(_slotse, context.pending_data)
                                logger.info("[IRD_BOOK] slot %d cancelled with ticket %s",
                                            _t["n"], handled_ref)
                except Exception as _be:
                    logger.warning("[IRD_BOOK] could not follow the ticket edit "
                                   "(non-fatal): %r", _be)
            # When nothing matched a committed ticket:
            #  • a CANCEL still SHORT-CIRCUITS to a plain acknowledgement so the
            #    cancel NEVER becomes a brand-new ticket downstream (the guest was
            #    cancelling something uncommitted — a form they never submitted, or
            #    an item that was only noted). This is the fix for a service
            #    misclassifying a cancel as create_ticket.
            #  • a non-cancel (modify/other) FALLS THROUGH to the normal flow, so a
            #    plain info question the classifier over-eagerly labelled an edit
            #    (e.g. "which restaurants are open now?") still gets a real answer.
            # cancel_pending clears any parked form (chat_service handles it) and
            # raises nothing; respond_only is a plain reply. Use cancel_pending when
            # a form was mid-fill so the cancel actually aborts it.
            _cancel_action = "cancel_pending" if _pending else "respond_only"
            if not handled_ref:
                # The model wrote its `reply` believing a locked order was the
                # subject. When the judge established it was not — the guest was
                # cancelling a SPA booking while a food order happened to be past
                # its kitchen window — that reply is about the wrong request, and
                # sending it tells the guest their food is being prepared when they
                # asked about something else entirely. Hand the turn back so the
                # service that owns the subject answers it properly.
                if _locked_cleared:
                    logger.info(
                        "[TICKET_EDIT] dropping a reply written under a locked-order "
                        "premise the judge rejected — handing back (msg=%r)",
                        _msg_l[:60],
                    )
                    return None
                # ── AN UNPLACED CART IS THE FOOD AGENT'S, NOT THIS PATH'S ────
                # "actually drop the tiramisu" reads as a cancel, so this
                # interceptor took the turn, matched no ticket (there is none -
                # the order has not been placed), and answered "I've noted that,
                # the team will take care of it". The food agent never ran, so no
                # order_items and no order_remove were ever emitted and the
                # tiramisu stayed in the cart and on the bill. It worked on the
                # SECOND ask only because by then the wording had changed enough
                # to miss this path. Removing a dish from a cart nobody has sent
                # anywhere is an edit, not a cancellation - hand it back.
                try:
                    if bool(getattr(settings, "ird_order_book_enabled", False)):
                        from services import ird_cart_book as _ebk
                        from services import ird_cart_store as _ebs

                        _erow = await _ebs.load(
                            hotel_code=str(getattr(context, "hotel_code", "") or ""),
                            session_id=session_id, create=False)
                        if _erow and any(
                                sl.get("items") and sl.get("status") == _ebk.ONGOING
                                for sl in _ebk.slots_from_row(_erow)):
                            logger.info(
                                "[TICKET_EDIT] no ticket matched but the guest has an "
                                "unplaced cart — handing back so the food agent can "
                                "edit it (msg=%r)", _msg_l[:60])
                            return None
                except Exception as _cart_exc:
                    logger.warning("[TICKET_EDIT] cart check skipped (non-fatal): %r",
                                   _cart_exc)
                if _is_cancel:
                    # ── NOTHING WAS CANCELLED, SO DO NOT SAY IT WAS ──────────
                    # Two very different situations end up here, and until now
                    # both got the same warm "I've noted that for our team":
                    #
                    #  (a) the guest is cancelling something that never became a
                    #      ticket — a form they opened and never submitted, a
                    #      request only mentioned in passing. Nothing to cancel,
                    #      nothing lost, and the acknowledgement is the truth.
                    #
                    #  (b) the guest is cancelling a REAL open request and we
                    #      could not work out which one. The acknowledgement is
                    #      then a lie by omission: the ticket stays open, staff
                    #      still act on it, and the guest walks away believing it
                    #      is off. A guest told "I've noted your request to cancel
                    #      the spa booking" still had that booking — a therapist
                    #      held a slot for a treatment nobody was coming to.
                    #
                    # We can tell them apart from state we already have: (a) has a
                    # parked form or a named uncommitted request to point at, (b)
                    # has open tickets and neither of those. In (b) we say plainly
                    # that nothing has been cancelled yet and ask which request
                    # they mean, because a guest who is asked can answer — a guest
                    # who is reassured cannot.
                    # Pre-booking-tab hotels (T24 onward): when the classifier DID
                    # match an open ticket and a check above dropped it, a real
                    # request is meant, whatever else was discussed. "booking room
                    # cancel that ticket" matched the open booking, the second
                    # opinion read "ticket" as something else, and the guest was
                    # told "Your request to cancel ... has been noted" while the
                    # booking stayed open: the last service ("Room Booking") had
                    # counted as an uncommitted request (14 Oct).
                    _admin_dropped_pick = bool(_router_ref) and _pbs_edit.is_admin_managed(
                        str(getattr(context, "hotel_code", "") or "").strip().lower()
                    )
                    _unmatched_real_request = bool(
                        ticket_lines and not _pending and (not _uncommitted or _admin_dropped_pick)
                    )
                    if _unmatched_real_request:
                        _open_names: list[str] = []
                        _menu_lines: list[dict] = []
                        for _t in ticket_lines:
                            # The request line is what the guest will recognise.
                            # `kind` is a service slug ("in-room_amenities") and
                            # reads like leaked plumbing, so it is only the
                            # fallback, and only once it is readable.
                            _nm = str(_t.get("request") or "").strip() or str(
                                _t.get("kind") or ""
                            ).replace("_", " ").strip()
                            if len(_nm) > 60:
                                _nm = _nm[:60].rsplit(" ", 1)[0].rstrip(",;") + "..."
                            if _nm and _nm.lower() not in [n.lower() for n in _open_names]:
                                _open_names.append(_nm)
                                _menu_lines.append(_t)
                        _listed = "; ".join(_open_names[:4])
                        # Kept for the guest's answer (Pre-booking-tab hotels).
                        if _pbs_edit.is_admin_managed(
                            str(getattr(context, "hotel_code", "") or "").strip().lower()
                        ) and isinstance(context.pending_data, dict):
                            context.pending_data["_cancel_menu"] = self._cancel_menu_rows(_menu_lines[:4])
                        reply = (
                            "I haven't cancelled anything yet - I want to be sure I "
                            "cancel the right request. "
                            + (
                                f"You currently have these open with us: {_listed}. "
                                "Which one should I cancel?"
                                if _listed else
                                "Could you tell me which request you'd like cancelled?"
                            )
                        )
                        logger.info(
                            "[TICKET_EDIT] cancel matched no ticket while %d remain open — "
                            "asking which one instead of claiming it was handled (msg=%r)",
                            len(ticket_lines), _msg_l[:60],
                        )
                    return OrchestrationDecision(
                        intent="ticket_edit",
                        action=_cancel_action,
                        response_text=reply,
                        confidence=0.6,
                        cancel_pending=bool(_pending),
                        metadata={
                            "ticket_edit_handled": True,
                            "ticket_edit_action": (
                                "cancel_unmatched" if _unmatched_real_request else "cancel"
                            ),
                            "ticket_edit_no_ticket": True,
                        },
                    )
                return None
            return OrchestrationDecision(
                intent="ticket_edit",
                action=(_cancel_action if _is_cancel else "respond_only"),
                response_text=reply,
                confidence=0.7,
                cancel_pending=bool(_is_cancel and _pending),
                metadata={
                    "ticket_edit_handled": True,
                    "ticket_edit_ref": handled_ref,
                    "ticket_edit_action": str(raw.get("action") or "").strip(),
                },
            )
        except Exception as exc:
            logger.warning("_maybe_handle_ticket_edit failed (non-fatal): %s", exc)
            return None


    async def _enforce_support_property_choice(
        self,
        *,
        decision: "OrchestrationDecision",
        target_service: dict[str, Any] | None,
        context: ConversationContext,
    ) -> "OrchestrationDecision":
        """A support ticket needs ONE hotel — ask before the form opens.

        The router's ACTIVE-PROPERTY GATE makes every property-specific service
        lock a property first, and deliberately EXEMPTS support / complaint
        intake so a guest reporting a problem is never blocked on a menu. That
        exemption is what lets a brand-level complaint ("I want to complain
        about Zone") reach the form with no property attached — and a ticket
        with no property resolves to no FMS entity, so ticketing routes it
        LOCAL-ONLY with no department: it sits on the KePSLA page and reaches
        nobody, while the guest was told the team would follow up.

        The support prompt already says to ask which hotel first; this enforces
        it against the property scope the LLM itself emitted, so the ask cannot
        be skipped. It fires ONLY when nothing at all knows the hotel — any
        single property already in hand answers the question. Fail-open: any
        error leaves the decision untouched, never blocking a complaint.
        """
        try:
            _svc_blob = " ".join([
                str((target_service or {}).get("id") or ""),
                str((target_service or {}).get("name") or ""),
            ]).lower()
            _is_support_intake = any(
                tok in _svc_blob
                for tok in (
                    "support", "handoff", "contact_staff", "callback",
                    "helpdesk", "raise_request", "complaint",
                )
            )
            _commits_ticket = (
                str(decision.pending_action or "").strip().lower() == "collect_form_details"
                or str(decision.action or "").strip().lower() == "create_ticket"
                or bool(getattr(decision.ticket, "ready_to_create", False))
            )
            _pd_now = context.pending_data if isinstance(context.pending_data, dict) else {}
            _scope_ids = self._normalize_property_id_list(
                list(decision.selected_property_ids or [])
                or list(_pd_now.get("_active_property_ids") or [])
                or list(_pd_now.get("_selected_property_scope_ids") or [])
                or [_pd_now.get("_selected_property_scope_id") or ""]
            )
            # THE FIRST OF SEVERAL HOTELS IS NOT A CHOICE (hotels set up in the
            # Pre-booking tab, T24 onward). The scope name is always filled with the
            # first hotel in focus, so with both T24 hotels in focus it read "T24
            # Residency": this gate never asked, the form was pre-filled "... at T24
            # Residency" and every support ticket went to Residency's team, though the
            # guest had chosen no hotel (Suhas, 14 Oct). Zone, Mango, Sarovar unchanged.
            _admin_hotel = False
            _first_of_many = False
            try:
                from services import prebooking_settings_service as _pbs_support

                _admin_hotel = _pbs_support.is_admin_managed(
                    str(getattr(context, "hotel_code", "") or "").strip().lower()
                )
                _first_of_many = len(_scope_ids) >= 2 and _admin_hotel
            except Exception:
                _first_of_many = False
            # A QUESTION IS NOT A FORM (Pre-booking tab hotels). collect_info with no
            # form step (no pending action, nothing missing, no ticket) is the agent
            # asking first - "T24 has only two hotels in Mumbai; which one is your
            # complaint about? Once confirmed, I will open the form" - but the widget
            # opens a form for any collect_info, so the form opened under the question
            # and was filed against the hotel in focus (T24 prod 14414, 15 Oct). Answer
            # only; a hotel named next continues this request instead of its intro.
            if (
                _admin_hotel
                and _is_support_intake
                and str(decision.action or "").strip().lower() == "collect_info"
                and not str(decision.pending_action or "").strip()
                and not list(decision.missing_fields or [])
                and not bool(getattr(decision.ticket, "required", False))
                and not bool(getattr(decision.ticket, "ready_to_create", False))
            ):
                decision.action = "respond_only"
                if not list(decision.selected_property_ids or []) and isinstance(context.pending_data, dict):
                    context.pending_data["_awaiting_hotel_for"] = str((target_service or {}).get("id") or "")
                return decision
            # THE AGENT NAMED NO HOTEL (Pre-booking tab hotels). Opening a form with no
            # hotel selected means the agent did not tie the request to any of ours -
            # "mysore hotel staff is too rude" while T24 Retro was in focus - and the
            # hotel in focus is not an answer for it: the ticket went to that hotel's
            # team (T24 prod 14420). Ask, offering every hotel of the client.
            if _admin_hotel and not list(decision.selected_property_ids or []) \
                    and not str(decision.selected_property_name or "").strip():
                _every_hotel = list(self._normalize_property_name_map(_pd_now.get("_property_name_map")).keys())
                if len(_every_hotel) > 1:
                    _scope_ids = _every_hotel
                    _first_of_many = True
            # ASK ONLY WHEN NOTHING KNOWS THE HOTEL. Any single property the LLM
            # or the session already has in hand answers the question, so we never
            # re-ask something already established: the property the decision
            # itself named, the scope the guest picked, or the hotel their stay
            # is pinned to. Only when every one of those is empty does it fire.
            _known_property = (
                str(decision.selected_property_name or "").strip()
                or ("" if _first_of_many else str(_pd_now.get("_selected_property_scope_name") or "").strip())
                or str(getattr(context, "booking_property_name", "") or "").strip()
                or str(_pd_now.get("_stay_property_name") or "").strip()
            )
            _name_map = self._normalize_property_name_map(_pd_now.get("_property_name_map"))
            if not (
                _is_support_intake
                and _commits_ticket
                and not _known_property
                and len(_name_map) > 1  # multi-property tenant, data-driven
                and len(_scope_ids) != 1
            ):
                return decision

            _candidates = [
                name
                for name in (
                    self._resolve_property_name_from_pending(
                        pending_data=_pd_now, property_id=pid, fallback=""
                    )
                    for pid in _scope_ids
                )
                if name
            ]
            if _candidates and len(_candidates) <= 6:
                if len(_candidates) == 1:
                    _listed = f"**{_candidates[0]}**"
                else:
                    _listed = (
                        ", ".join(f"**{n}**" for n in _candidates[:-1])
                        + f", or **{_candidates[-1]}**"
                    )
                _ask = f"Sorry to hear that. Which hotel is this about — {_listed}?"
            else:
                _ask = (
                    "Sorry to hear that. Which of our hotels is this about? "
                    "Once you tell me, I'll get it to that team."
                )
            if _admin_hotel:
                # "Sorry to hear that" is wrong for "call the manager" or "connect me
                # to the front office". Hotels set up in the Pre-booking tab word this
                # question in the DB row `ticketing.support_property_ask`, where
                # {hotels} is the hotel list; without the row the line above stands.
                _hotel_ask = await self._db_prompt_text("ticketing.support_property_ask")
                if _hotel_ask:
                    _bold = [f"**{n}**" for n in (_candidates or list(_name_map.values()))]
                    _hotels = (", ".join(_bold[:-1]) + ", or " + _bold[-1]) if len(_bold) > 1 else "".join(_bold)
                    _ask = _hotel_ask.replace("{hotels}", _hotels)
            decision.action = "respond_only"
            decision.pending_action = None
            decision.missing_fields = []
            decision.ticket.required = False
            decision.ticket.ready_to_create = False
            decision.response_text = _ask
            decision.metadata["support_property_required"] = True
            if _first_of_many and isinstance(context.pending_data, dict):
                # The guest's next message answers this, so a tapped hotel name
                # continues the request instead of opening that hotel's intro.
                context.pending_data["_awaiting_hotel_for"] = str((target_service or {}).get("id") or "")
            logger.info(
                "[SUPPORT_PROPERTY_GATE] asked which hotel before raising ticket "
                "(session=%s service=%s scope=%d)",
                str(getattr(context, "session_id", "") or ""),
                str((target_service or {}).get("id") or ""),
                len(_scope_ids),
            )
            return decision
        except Exception as exc:  # fail-open: never block a complaint on this gate
            logger.warning("support property gate skipped (non-fatal): %s", exc)
            return decision

    async def orchestrate_turn(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        memory_snapshot: dict[str, Any],
        selected_phase_context: dict[str, Any] | None = None,
        prefetched_services: list[dict[str, Any]] | None = None,
        assistant_turn_id: str = "",
        _service_reroute_depth: int = 0,
    ) -> OrchestrationDecision | None:
        orchestration_mode_enabled = bool(getattr(settings, "chat_llm_orchestration_mode", False))
        no_template_mode_enabled = bool(getattr(settings, "chat_no_template_response_mode", False))
        if not orchestration_mode_enabled and not no_template_mode_enabled:
            return None
        if not str(settings.openai_api_key or "").strip():
            return None

        # Keep the guest's untouched text for language routing. Several orchestration
        # helpers annotate `user_message` with English [CONTEXT: ...] wrappers later;
        # those wrappers are instructions, not evidence that the guest chose English.
        if _service_reroute_depth == 0 or not str(
            getattr(context, "_turn_guest_language_message", "") or ""
        ).strip():
            context._turn_guest_language_message = str(user_message or "").strip()
            context._turn_response_language = {}
            context._turn_language_switch = False
        language_user_message = str(
            getattr(context, "_turn_guest_language_message", "") or user_message or ""
        ).strip()

        # ── KILL STALE BACKGROUND WORK ───────────────────────────────────────
        # Cancel any chip-speculation tasks left over from previous turns. The
        # live request is the ONLY priority; background work that hasn't
        # delivered a cached decision yet is dead weight that competes with
        # the live request for OpenAI's per-account rate limit and slows it
        # down. Cancelling here bounds in-flight speculation to the new
        # turn's own kickoffs (≤3 per session at any moment).
        _live_session_id = str(getattr(context, "session_id", "") or "")
        if _live_session_id:
            self._cancel_session_background_work(_live_session_id)
        # "Which hotel?" is answered on the turn right after it is asked (see
        # _maybe_build_property_intro_response); after that it no longer applies.
        _awaited_service_id = ""
        if _service_reroute_depth == 0 and isinstance(getattr(context, "pending_data", None), dict):
            _awaited_service_id = str(context.pending_data.pop("_awaiting_hotel_for", None) or "").strip()

        # ── CHIP-CLICK FAST PATH (with cascading speculation) ────────────────
        # If the guest clicked one of the chips we shipped on the previous
        # turn, the chip text comes back as `user_message` verbatim. We
        # already speculatively ran the answerer for each chip in the
        # background, so we serve the cached decision with zero LLM wait.
        # _consume_chip_speculation also clears the session's cache, so
        # stale speculations from the previous turn never leak into this
        # one regardless of whether we hit. Pending actions (mid-flow
        # form fills, etc.) skip this path so the form/service routing
        # stays authoritative.
        #
        # CASCADE: before returning the cached decision, kick off background
        # speculation for THIS decision's chips too. That way the next click
        # is also instant — and the click after that, and so on. Tree-of-
        # clicks: every chip the guest can click already has a pre-generated
        # answer waiting. The live request still returns immediately; the
        # cascade runs in the background.
        _chip_fast_path_decision = await self._maybe_serve_chip_click_fast_path(
            user_message=user_message, context=context
        )
        if _chip_fast_path_decision is not None:
            return _chip_fast_path_decision

        # ── RESUME PROMPT HANDLER ────────────────────────────────────────────
        # If last turn asked user to resume a suspended service, annotate the
        # message with context and let the LLM orchestrator decide the intent.
        if context.resume_prompt_sent:
            context.resume_prompt_sent = False
            if context.suspended_services:
                suspended = context.suspended_services[0]
                svc_name = suspended.get("service_name", "previous request")
                # Annotate so the orchestrator sees full context and decides
                user_message = (
                    f"[CONTEXT: bot just asked whether to resume '{svc_name}'. "
                    f"Guest replied: '{user_message}'. "
                    f"Decide based on the reply whether to resume or abandon.]"
                )
        # ─────────────────────────────────────────────────────────────────────

        # ── "YES" TO AN OFFER MEANS DELIVER THE OFFER ────────────────────────
        # When the bot ends by offering to SHOW or SUGGEST something ("shall I
        # suggest some beverages?", "would you like to hear about those?") and the
        # guest simply says yes, that yes belongs to the offer. It kept being
        # consumed by whatever confirmation was also open, so the guest accepted
        # an offer of drinks and got their food order placed instead — no drinks,
        # and a question they answered that was never actually answered.
        #
        # Prompt rules did not hold this (three attempts, including moving the
        # rule to the end of the prompt), so the acceptance is made explicit in
        # the message itself — the same annotation trick the resume handler above
        # uses. A small judge decides whether an offer was really made and what it
        # was; nothing here pattern-matches on words.
        from services import prebooking_settings_service as _pbs_offer

        _offer = await self._bare_yes_accepts_an_offer(
            previous_bot_message=self._last_message_by_role(context, "assistant"),
            user_message=user_message,
            session_id=str(getattr(context, "session_id", "") or ""),
            handoff_kind=_pbs_offer.is_admin_managed(
                str(getattr(context, "hotel_code", "") or "").strip().lower()
            ),
            hotel_code=str(getattr(context, "hotel_code", "") or ""),
        )
        # ── A "YES" TO OUR OWN POST-FORM CLOSER ──────────────────────────────
        # After a form the bot says "...our team will reach out" and then "In the
        # meantime, want to explore more about <hotel> - rooms, dining, things to do
        # - or is there anything else I can help you with?". A bare "Yes" to that
        # was read as accepting a staff handoff: the router opened the support form
        # (6/6 replayed) and even an info route answered "let me get someone from
        # our team" (6/6). The judge can only guess what was offered; the server
        # knows it sent this closer, so it says what was agreed to - the router then
        # answers with the hotel (6/6) and the answerer gives the overview (4/4).
        _pf_closer = str(
            (getattr(context, "pending_data", None) or {}).get("_post_form_closer") or ""
        ).strip()
        if (
            _pf_closer
            and _pf_closer == str(self._last_message_by_role(context, "assistant") or "").strip()
            and self._bare_yes_is_agreement(
                session_id=str(getattr(context, "session_id", "") or ""),
                user_message=user_message,
            )
        ):
            logger.info(
                "[POST_FORM_YES] guest said %r to the post-form closer - answering with the hotel overview",
                str(user_message)[:34],
            )
            user_message = (
                "[CONTEXT: the assistant's previous message invited the guest to explore more "
                "about the hotel - rooms, dining, things to do. The guest replied "
                f"'{user_message}', which means they want exactly that: give them an overview "
                "of the hotel's rooms, dining and things to do now. It is not a request for "
                "staff or a form.]"
            )
            _offer = None
        if _offer:
            logger.info(
                "[OFFER_ACCEPTED] guest said %r to a %r offer (%r) - making it explicit",
                str(user_message)[:34], _offer.get("kind"), str(_offer.get("offered"))[:50],
            )
            if _offer.get("kind") == "add_item":
                # A YES TO "SHALL I ADD X?" IS AN ORDER, NOT A REQUEST FOR A LIST.
                # This used to fall into the `show` branch below, whose instruction
                # ends "do NOT place, re-summarise or confirm any order on this
                # turn" - so the model dutifully wrote "Here is the item you
                # accepted: Filter Coffee ₹325", emitted no cart line and no
                # ticket, and the coffee never reached the kitchen. The guest had
                # said yes; they were answered with a receipt for nothing.
                # THE SERVER HAS ALREADY ESTABLISHED THIS ONE. A judge later in
                # the turn asks "which of these did only the ASSISTANT bring up?"
                # and it answered "Tiramisu" for a dish the guest had typed by
                # name one turn earlier and then said yes to - both visible in the
                # transcript it was given. A guess must not overturn a fact we
                # already proved, so the accepted dish is marked vetted here and
                # the judge is never asked about it.
                _accepted = str(_offer.get("offered") or "").strip()
                if _accepted:
                    try:
                        _pd_v = context.pending_data or {}
                        _pd_v["_ird_vetted_items"] = sorted(
                            {str(x) for x in (_pd_v.get("_ird_vetted_items") or [])
                             if str(x or "").strip()} | {_accepted}
                        )
                        context.pending_data = _pd_v
                        logger.info(
                            "[OFFER_ACCEPTED] %r is guest-accepted - vetted, the "
                            "provenance judge will not be asked about it", _accepted,
                        )
                    except Exception as _v_exc:
                        logger.warning(
                            "[OFFER_ACCEPTED] could not vet %r (non-fatal): %r",
                            _accepted, _v_exc,
                        )
                user_message = (
                    f"[CONTEXT: the assistant asked whether to add "
                    f"'{_offer.get('offered')}' to the guest's order, and the guest "
                    f"replied '{user_message}', which means YES, add it. Treat this "
                    f"exactly as though they had just asked for it by name: put it in "
                    f"the order NOW by emitting order_items on this turn - do not ask "
                    f"again whether to add it, they have already said yes. Then look "
                    f"at that order's status in `order_book`: if it is `ongoing`, ask "
                    f"whether to place it; if it is already `placed`, say the dish is "
                    f"added and the order is on its way, and do NOT ask to place it a "
                    f"second time. Do NOT "
                    f"answer by listing or describing it again - they have already "
                    f"seen it and said yes.]"
                )
            else:
                # ── NO PROHIBITIONS. STATE WHAT WAS AGREED AND LET IT DECIDE. ──
                # These two branches used to end "do NOT place, re-summarise or
                # confirm any order on this turn". That is a hardcoded consequence
                # attached to a guess: when the guess was wrong the order was lost
                # silently. It happened twice - a Tiramisu, then 2 Arrabbiata
                # Pasta the guest had just said yes to, listed back prettily and
                # never recorded, because "start a new order for the pasta" was
                # read as an offer to SHOW something.
                #
                # So the classification no longer dictates the action. The guest's
                # yes is restated in plain words, and what to do about it is read
                # off `order_book` like every other turn - which is the one place
                # that actually knows whether anything needs placing.
                _what = str(_offer.get("offered") or "").strip()
                user_message = (
                    f"[CONTEXT: the assistant's previous message offered: "
                    f"'{_what}'. The guest replied '{user_message}', which means "
                    f"YES to that. Work out from the assistant's own previous "
                    f"message what they agreed to and act on it. If ONE dish (or a "
                    f"specific few) was offered FOR THEIR ORDER, that is now "
                    f"theirs - put it in the order by emitting order_items on this "
                    f"turn. BUT A LIST IS NOT AN ORDER: if the assistant showed "
                    f"them options - the desserts, what is available, a menu "
                    f"section - a yes means they want to see it or choose from it, "
                    f"NOT that they are ordering every item on it. Never put a "
                    f"whole list into the order; name the options and ask which "
                    f"one they would like. Do not ask them to repeat themselves. "
                    f"Whether anything then needs placing is answered by "
                    f"`order_book`, not by this note.]"
                )

        # ── HOW MANY? ────────────────────────────────────────────────────────
        # A dish named with no number is not a complete order line. The model's
        # habit is to write 1 and move straight to the confirmation, so a guest
        # ordering bread for the table gets one piece and discovers it when the
        # tray arrives. Two attempts to fix this in the service prompt both lost
        # to the older "go straight to checkout" rule, so the reading happens in
        # a judge and only the FINDING is handed over - the service agent still
        # writes the question, in its own voice, with the menu in front of it.
        _no_qty = (
            await self._dishes_named_without_a_quantity(user_message=user_message)
            if bool(getattr(settings, "ird_portion_check_enabled", False))
            else None
        )
        if _no_qty:
            logger.info(
                "[PORTIONS] guest named %s without saying how many - asking before checkout",
                _no_qty,
            )
            _dishes = ", ".join(_no_qty)
            user_message = (
                f"[CONTEXT: the guest said '{user_message}'. They did not say HOW MANY "
                f"portions they want of: {_dishes}. Do not assume one. Ask them how many "
                f"portions of {_dishes} they would like - one short question covering all "
                f"of them, in your own words. Do NOT show an order summary, a total, or "
                f"ask them to confirm the order on this turn; you cannot total an order "
                f"whose quantities you do not know yet.]"
            )
        # ─────────────────────────────────────────────────────────────────────

        # ── CANCEL / EDIT AN EXISTING TICKET ─────────────────────────────────
        # If the guest is cancelling or changing something they ALREADY requested
        # this session, comment on that EXACT existing ticket instead of spawning
        # a new one (or looping on the service that can't perform the change).
        # Context-driven + fail-open: returns None (normal flow) unless it
        # confidently handled an edit.
        _ticket_edit_decision = await self._maybe_handle_ticket_edit(
            user_message=user_message, context=context,
        )
        if _ticket_edit_decision is not None:
            return _ticket_edit_decision

        # The edit was refused because the guest named somewhere the ticket has
        # never heard of. Say so: name the places we DO have and let them pick,
        # rather than leaving them thinking the booking simply cannot be moved.
        try:
            _pd_unv = context.pending_data if isinstance(context.pending_data, dict) else None
            _unverified = str((_pd_unv or {}).pop("_unverified_place_named", "") or "").strip()
            _dining = [str(x) for x in ((_pd_unv or {}).pop("_real_dining_outlets", None) or [])]
        except Exception:
            _unverified, _dining = "", []
        if _unverified:
            logger.info(
                "[TICKET_EDIT] telling the guest we have no %r rather than only "
                "refusing the change", _unverified,
            )
            user_message = (
                f"[CONTEXT: the guest said '{user_message}'. They referred to "
                f"'{_unverified}', which is NOT the place named on their existing "
                f"booking and may not exist at this hotel at all. Check it against "
                f"the venues this hotel actually has. If there is no such place, say "
                f"plainly that we do not have it, NAME the ones we do have, and ask "
                f"which they meant. Never deny a venue and then list it. Do NOT change "
                f"or confirm any booking on this turn, and do NOT quietly assume they "
                f"meant the place already on the ticket."
                + (
                    f" The ONLY places here that take table reservations are: "
                    f"{', '.join(_dining)}. Name these and nothing else — do not offer "
                    f"meeting rooms, banquet halls or conference spaces as somewhere to "
                    f"eat, because a guest cannot book dinner in a board room."
                    if _dining else ""
                )
                + "]"
            )

        selected_phase_id, selected_phase_name = self._resolve_selected_phase(
            context=context,
            selected_phase_context=selected_phase_context,
            capabilities_summary=capabilities_summary,
        )
        orchestration_trace_id = f"orch-{uuid.uuid4().hex[:12]}"

        services_snapshot = self._service_snapshot(capabilities_summary)
        allowed_service_rows = [
            row
            for row in services_snapshot
            if isinstance(row, dict)
            and bool(row.get("is_active", True))
            and self._normalize_identifier(row.get("phase_id")) == selected_phase_id
        ]
        allowed_service_ids = [
            self._normalize_identifier(row.get("id"))
            for row in allowed_service_rows
            if self._normalize_identifier(row.get("id"))
        ]
        allowed_service_names = [
            str(row.get("name") or "").strip()
            for row in allowed_service_rows
            if str(row.get("name") or "").strip()
        ]
        out_of_phase_services: list[dict[str, str]] = []
        for row in services_snapshot:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("is_active", True)):
                continue
            phase_id = self._normalize_identifier(row.get("phase_id"))
            if not phase_id or phase_id == selected_phase_id:
                continue
            sid = self._normalize_identifier(row.get("id"))
            if not sid:
                continue
            out_of_phase_services.append(
                {
                    "id": sid,
                    "name": str(row.get("name") or sid).strip(),
                    "phase_id": phase_id,
                    "phase_name": str(row.get("phase_name") or self._phase_label(phase_id, capabilities_summary)).strip(),
                    "description": str(row.get("description") or "").strip(),
                    "profile": str(row.get("profile") or "").strip(),
                    "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                    "ticketing_conditions": str(row.get("ticketing_conditions") or "").strip(),
                }
            )
        allowed_services_detailed = [
            {
                "id": self._normalize_identifier(row.get("id")),
                "name": str(row.get("name") or "").strip(),
                "description": str(row.get("description") or "").strip(),
                "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                "phase_id": self._normalize_identifier(row.get("phase_id")),
                "phase_name": str(row.get("phase_name") or "").strip(),
                "knowledge_facts": row.get("knowledge_facts") or [],
                "profile": str(row.get("profile") or "").strip(),
                "required_slots": row.get("required_slots") or [],
            }
            for row in allowed_service_rows
            if isinstance(row, dict) and self._normalize_identifier(row.get("id"))
        ]
        out_of_phase_services_detailed = [
            {
                "id": str(row.get("id") or "").strip(),
                "name": str(row.get("name") or "").strip(),
                "phase_id": str(row.get("phase_id") or "").strip(),
                "phase_name": str(row.get("phase_name") or "").strip(),
                "description": str(row.get("description") or "").strip(),
                "profile": str(row.get("profile") or "").strip(),
                "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                "ticketing_conditions": str(row.get("ticketing_conditions") or "").strip(),
                "availability_hint": self._phase_transition_timing_hint(
                    selected_phase_id,
                    str(row.get("phase_id") or "").strip(),
                ),
            }
            for row in out_of_phase_services
            if isinstance(row, dict)
        ]
        # Speculative KB warmup that overlaps with the router LLM.
        # The router's network round-trip (2-15s) is the single biggest sequential
        # cost in a turn. While it's thinking, we kick off a fire-and-forget DB
        # read of the structured book catalog for the cached active properties.
        # The router's own internal call to get_structured_book_catalog_db hits
        # the same 5-min process cache, so when the router starts work locally
        # it skips that DB read. Worst case (cache miss/fresh load) we just
        # trigger one extra DB query that warms the catalog cache for the
        # downstream answerer step — pure latency win, no behavioral change,
        # no quality cost. We do NOT await this — if it fails, we silently
        # fall through and the original code path handles its own load.
        try:
            if bool(getattr(settings, "kb_read_from_tables", False)):
                _hotel_code_warmup = str(getattr(context, "hotel_code", "") or "").strip()
                if _hotel_code_warmup:
                    from services.kb_table_reader import get_structured_book_catalog_db as _warm_catalog
                    asyncio.create_task(
                        _warm_catalog(
                            hotel_code=_hotel_code_warmup,
                            property_slugs=None,
                            only_active=True,
                        )
                    )
        except Exception:
            pass

        # Kick off the weather city extractor in PARALLEL with the main router.
        # The extractor is a small LLM call (~40 tokens) that returns a city
        # string or "" (not weather). Running it alongside the router means
        # weather classification happens regardless of which downstream path
        # takes over (lightweight book_answerer OR the heavy orchestrator OR
        # a service agent). Zero added wall-clock latency since both calls
        # run concurrently — by the time the router finishes, the extractor
        # has too. The router's `weather_for` field stays as the primary
        # signal; this extractor is the fallback for paths the router doesn't
        # cover (active forms, chip clicks, fallback routes, etc.).
        # ONLY when the router is going to short-circuit. Read the consumer
        # below: the extractor's answer is used in exactly one case — when
        # `pending_action` is set, so the router never runs and emits no
        # `weather_for`. On every other turn the router's own field decides and
        # the extractor's result is discarded.
        #
        # It used to fire on EVERY message. Measured over a 20-turn chat where
        # nobody mentioned weather once, that was 20 wasted LLM calls (~1.5s
        # each, in parallel so no wall-clock cost, but paid for every time).
        # `pending_action` is already set before this point, so gating on it
        # keeps behaviour identical.
        _weather_extractor_task = None
        if str(getattr(context, "pending_action", "") or "").strip():
            from services.weather_service import _extract_city_via_llm

            _weather_extractor_task = asyncio.create_task(
                _extract_city_via_llm(user_message)
            )

        main_router_hint = await self._resolve_main_route_with_llm(
            user_message=user_message,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            services_snapshot=services_snapshot,
            context=context,
            language_user_message=language_user_message,
        )
        main_router_route = self._normalize_main_router_route(main_router_hint.get("route"))
        # ── PERSIST THE ROUTER'S PROPERTY CHOICE NOW, NOT AT THE END ────────
        # The scope was only written near the end of this method, but every
        # ROUTER-DECISIVE SHORT-CIRCUIT below returns before reaching it — so a
        # turn that short-circuits kept the PREVIOUS property. Observed: the
        # guest moved from Calangute to Parra, the router correctly returned
        # ["zone_connect_park_parra_goa"], the turn short-circuited to the
        # support form, and the form prefill and the ticket both still said
        # Calangute. Writing it here means the property the router picked is the
        # property the form and the ticket use, on every path. The later sync is
        # unchanged and simply re-writes the same value on non-short-circuit
        # turns.
        try:
            _early_prop_ids = self._normalize_property_id_list(
                [
                    str(pid).strip()
                    for pid in (main_router_hint.get("selected_property_ids") or [])
                    if str(pid).strip()
                ]
            )
            if _early_prop_ids:
                if not isinstance(context.pending_data, dict):
                    context.pending_data = {}
                self._sync_property_scope_pending(
                    pending_data=context.pending_data,
                    property_ids=_early_prop_ids,
                )
        except Exception:
            logger.debug("early property scope persist skipped", exc_info=True)
        # Carried on the context rather than threaded through a dozen signatures:
        # both answering lanes need it, and it is a fact about THIS turn only, so
        # it must never reach pending_data and survive into the next one.
        try:
            context._router_off_topic = bool(main_router_hint.get("off_topic"))
            previous_response_language = self._last_assistant_response_language(context)
            context._turn_response_language = self._response_language_preference(
                user_message=language_user_message,
                history_rows=self._history_preview(context, max_messages=8, max_chars=2400),
                last_assistant_message=self._last_message_by_role(context, "assistant"),
                router_language=main_router_hint.get("response_language"),
            )
            context._turn_language_switch = bool(
                previous_response_language
                and context._turn_response_language
                and str(previous_response_language.get("language") or "").strip().casefold()
                != str(context._turn_response_language.get("language") or "").strip().casefold()
            )
            main_router_hint["response_language"] = context._turn_response_language
            if context._router_off_topic:
                logger.info("[SCOPE] router called this message off-topic: %r",
                            str(user_message or "")[:80])
        except Exception:
            pass
        chip_click_context = self._consume_chip_click_context(context=context)
        main_router_route, main_router_hint = self._apply_chip_click_route_lock(
            context=context,
            selected_phase_id=selected_phase_id,
            main_router_route=main_router_route,
            main_router_hint=main_router_hint,
            chip_click_context=chip_click_context,
        )
        _turn_hotel_code = str(getattr(context, "hotel_code", "") or "").strip().lower()
        try:
            from services import prebooking_settings_service as _pbs_turn

            _turn_admin_hotel = _pbs_turn.is_admin_managed(_turn_hotel_code)
        except Exception:
            _turn_admin_hotel = False
        _turn_form_pending = bool(str(context.pending_action or "").strip())

        # NOT ONE OF OURS IS DECLINED (Pre-booking tab hotels). With one of our hotels in
        # focus, the router chooses no hotel only for a place that is not ours: all 29
        # such T24 turns on prod, 13-15 Oct, were about Bangalore, Mysore, Kerala or
        # Jammu. Left to the lanes it still reached a form: the support lane asked
        # "which hotel - Residency or Retro?" and the repeated Jammu refund opened a
        # Residency form (15 Oct); the answerer, even with both hotels loaded, offered
        # our team for "mysore staff is too rude rise complaint" and "yes connect me"
        # opened the form (UAT, 16 Oct). The hotel's own decline answers it (DB row
        # `scope.refusal_other_business`) and nothing is raised.
        _router_ids_now = [
            str(v).strip() for v in list(main_router_hint.get("selected_property_ids") or []) if str(v).strip()
        ]
        _focus_ids_now = self._normalize_property_id_list(
            list((context.pending_data or {}).get("_active_property_ids") or [])
            if isinstance(context.pending_data, dict) else []
        )
        _router_chose_no_hotel = bool(
            _turn_admin_hotel
            and not _router_ids_now
            and _focus_ids_now
            and not bool(main_router_hint.get("broaden_properties"))
        )
        if (
            _router_chose_no_hotel
            and main_router_route in {"book", "auto", "service"}
            and not _turn_form_pending
        ):
            _decline = await self._db_prompt_text("scope.refusal_other_business")
            if not _decline:
                _decline = self.scope_refusal_text(
                    "other_business",
                    hotel_name=str((capabilities_summary or {}).get("business_name") or "").strip(),
                    hotel_code=_turn_hotel_code,
                )
            logger.info(
                "[SCOPE] router chose no hotel while %s was in focus (not one of ours) - "
                "declined, nothing raised (route=%s svc=%s msg=%r)",
                _focus_ids_now, main_router_route, str(main_router_hint.get("service_id") or "-"),
                str(user_message or "")[:60],
            )
            not_ours_decision = OrchestrationDecision(
                action="respond_only",
                intent="faq",
                confidence=float(main_router_hint.get("confidence") or 0.9),
                response_text=_decline,
                target_service_id="",
                metadata={
                    "source": "main_router_not_ours",
                    "orchestration_trace_id": orchestration_trace_id,
                    "main_router_route": main_router_route,
                },
            )
            _log_decision({
                "ts": datetime.now(UTC).isoformat(),
                "trace_id": orchestration_trace_id,
                "session_id": str(getattr(context, "session_id", "")),
                "phase": {"id": selected_phase_id, "name": selected_phase_name},
                "user_message": user_message,
                "routing": {
                    "orchestrator_action": "skipped_not_ours",
                    "orchestrator_service_id": "",
                    "main_router_route": main_router_route,
                    "main_router_service_id": str(main_router_hint.get("service_id") or ""),
                    "main_router_book_ids": [],
                    "clarification_protected": False,
                    "final_action": "respond_only",
                    "final_service_id": "",
                    "prompt_source": "scope.refusal_other_business",
                    "service_resolution_source": "main_router_not_ours",
                },
                "response_text": str((not_ours_decision.response_text or "")[:500]),
                "intent": "faq",
                "missing_fields": [],
                "context_switched": False,
                "answer_first_guard_skipped": True,
                "pending_action_at_start": str(context.pending_action or ""),
            })
            return not_ours_decision

        # A HOTEL NAMED IN ANSWER TO "WHICH HOTEL?" CONTINUES THAT REQUEST (Pre-booking
        # tab hotels). Our own question, with our own hotels as its answers, asked for
        # one request. The router read the tapped hotel as a new question about that
        # hotel: "call manager" -> "which hotel?" -> tap T24 Retro got Retro's overview
        # and no form (prod, 15 Oct). When the whole reply is one of our hotels (its name
        # or an alias from the admin), the request it was asked for continues.
        if _awaited_service_id and _turn_admin_hotel and not _turn_form_pending:
            _picked_hotel = await self._property_named_by_message(
                hotel_code=_turn_hotel_code, message=language_user_message,
            )
            if _picked_hotel:
                logger.info(
                    "[AWAITED-HOTEL] %s picked for %s - continuing that request (router said %s/%s)",
                    _picked_hotel, _awaited_service_id, main_router_route,
                    str(main_router_hint.get("service_id") or "-"),
                )
                main_router_route = "service"
                main_router_hint["route"] = "service"
                main_router_hint["service_id"] = _awaited_service_id
                main_router_hint["selected_property_ids"] = [_picked_hotel]
                main_router_hint["book_ids"] = []
                main_router_hint["confidence"] = max(float(main_router_hint.get("confidence") or 0.0), 0.95)

        # Live weather — combine TWO classification signals:
        #   1. Router's `weather_for` field (set by the main_router LLM during
        #      its routing call, when the router actually runs)
        #   2. The parallel city extractor task we kicked off above (runs on
        #      EVERY turn, including when the router short-circuits to fallback
        #      because of pending_action, chip-click locks, or other states
        #      that would skip weather classification otherwise)
        # The extractor returns a city string or "" — non-empty = weather ask.
        # Whichever signal fires first wins, so the heavy-orchestrator path
        # never silently skips weather anymore. Zero added latency since the
        # extractor runs in parallel with the router. Result is stashed on
        # context.pending_data so every downstream branch injects without
        # re-fetching.
        if isinstance(context.pending_data, dict):
            context.pending_data.pop("_turn_live_weather", None)
        _weather_for = str(main_router_hint.get("weather_for") or "").strip()
        try:
            _extracted_city = await _weather_extractor_task if _weather_extractor_task else ""
        except Exception:
            _extracted_city = ""
        _extracted_city = (_extracted_city or "").strip()
        # Decide the pre-extracted city to pass into the fetch:
        #   - router said "property" → use property fallback (empty string forces it)
        #   - router gave an explicit city → use that
        #   - router was SKIPPED (early-fallback due to pending_action) → use
        #     the extractor's result if any. The router only short-circuits to
        #     fallback when pending_action is set; in that case it returns
        #     route='auto' with no weather classification, so the extractor is
        #     our only signal for weather questions inside service flows.
        # If the router actually ran and emitted weather_for='', that's a
        # confident "not weather" call — we trust it and skip the fetch, even
        # if the extractor happened to find a city (e.g. found "Mumbai" inside
        # a room-booking message).
        _router_short_circuited = bool(str(context.pending_action or "").strip())
        _pre_city: Optional[str] = None
        if _weather_for:
            _pre_city = "" if _weather_for.lower() == "property" else _weather_for
        elif _router_short_circuited and _extracted_city:
            _pre_city = _extracted_city

        # Weather is never a service request and never an acceptance of a prior
        # handoff offer. If the router itself classified weather (weather_for
        # set) AND routed to a service (typically support-intake via rule
        # 1a-extra slip), force it back to book. We use ONLY the router's
        # signal here — not the extractor — because the extractor can falsely
        # find a city in a non-weather message (e.g. "book a room at orchid
        # hotel mumbai" returns "Mumbai" but the user isn't asking weather).
        # The extractor's job is to RESCUE missed weather classifications, not
        # to override the router's routing decision.
        if _weather_for and main_router_route == "service":
            main_router_route = "book"
            main_router_hint["route"] = "book"
            main_router_hint["service_id"] = ""
        if _pre_city is not None:
            try:
                from services.weather_service import resolve_and_fetch_weather
                _router_pids_for_weather = [
                    str(v).strip()
                    for v in list(main_router_hint.get("selected_property_ids") or [])
                    if str(v).strip()
                ]
                _wx = await resolve_and_fetch_weather(
                    context,
                    user_message,
                    pre_extracted_city=_pre_city,
                    property_ids_override=_router_pids_for_weather or None,
                )
                if _wx and isinstance(context.pending_data, dict):
                    context.pending_data["_turn_live_weather"] = _wx
            except Exception as e:
                logger.warning("Live weather fetch failed: %s", e)

        # Layer 1 — Property focus.
        # Primary: selected_property_ids the router emitted explicitly.
        # Fallback A: if the router only emitted book_ids, derive properties
        #   from those books. Keeps focus correct when the LLM forgets to fill
        #   in `selected_property_ids` but still picked specific books.
        # Fallback B: if both are empty AND we are on a book/auto route, fall
        #   back to EVERY available property so the answerer always has KB to
        #   work with — never let a generic ask ("tell me about your hotel")
        #   collapse the KB to nothing and dump us into the heavy orchestrator.
        main_router_property_ids = self._normalize_property_id_list(
            [str(v).strip() for v in list(main_router_hint.get("selected_property_ids") or []) if str(v).strip()]
        )
        main_router_book_ids = [
            str(value).strip()
            for value in list(main_router_hint.get("book_ids") or [])
            if str(value).strip()
        ]
        # Tracks whether main_router_book_ids came from the LLM (apply the
        # selection cap) or from a fallback that loads every book in scope
        # (no cap — we want full coverage). Without this, fallback-loaded
        # book lists would be truncated to 12, hiding most books from the
        # answerer in single-property generic asks.
        _book_ids_from_llm = bool(main_router_book_ids)
        _hotel_code_for_books = str(getattr(context, "hotel_code", "") or "").strip()

        # Broaden-intent override: the router emits `broaden_properties=true`
        # when the guest accepts a post-form "or another of our properties?"
        # offer. We gate it on the previous assistant turn actually being a
        # form/ticket completion — outside that window the intent is too
        # ambiguous to act on. When both fire: wipe the router's narrow ids
        # AND the stale `_active_property_ids` that the carry-existing
        # fallback below would otherwise resurrect. With both cleared,
        # Fallback B loads every property's books so the answerer's
        # `book_properties` contains the full set.
        if (
            bool(main_router_hint.get("broaden_properties"))
            and _previous_assistant_was_form_completion(context)
        ):
            main_router_property_ids = []
            main_router_book_ids = []
            _book_ids_from_llm = False
            if isinstance(context.pending_data, dict):
                for _stale_key in (
                    "_active_property_ids",
                    "_active_property_names",
                    "_selected_property_scope_id",
                    "_selected_property_scope_ids",
                    "_selected_property_scope_name",
                ):
                    if _stale_key in context.pending_data:
                        context.pending_data[_stale_key] = (
                            "" if _stale_key.endswith(("_id", "_name")) else []
                        )
        # The books the router itself chose, kept before the fallbacks below
        # fill in every book in scope. Read by the admin menu topics.
        _router_chosen_book_ids = list(main_router_book_ids) if _book_ids_from_llm else []

        # SCOPE IS THE ROUTER'S JOB — no deterministic override here.
        # This spot used to run a "PRIOR-NARROW PROTECTION" step: whenever the
        # prior turn was pinned to a single property, it intersected the router's
        # THIS-turn selection back down to that one pin unless the guest's words
        # hit a hardcoded phrase list ("compare" / "all hotels" / …). It was meant
        # to stop the router dragging in unrequested properties on a vague
        # follow-up ("tell me about the spa"). But it also silently collapsed
        # legitimate fleet / compare answers the router got RIGHT ("list all your
        # hotels", "which one is elder friendly") back to the single pin — and the
        # persist-focus call below then RE-pinned it, locking the whole
        # conversation onto that one property until the guest happened to type an
        # exact magic phrase. The router already receives `active_property_ids`
        # and owns this call now: keep the current property on a narrow follow-up,
        # widen to the fleet on a compare / list / "which one" ask. The router
        # prompt carries that rule instead of this brittle keyword gate.
        # `_user_broadened` is still read by the persist-focus guard below.
        _user_broadened = bool(main_router_hint.get("broaden_properties"))
        _existing_active_ids = self._normalize_property_id_list(
            list((context.pending_data or {}).get("_active_property_ids") or [])
            if isinstance(context.pending_data, dict)
            else []
        )
        if (
            _user_broadened
            and main_router_property_ids
            and _existing_active_ids
            and set(main_router_property_ids) == set(_existing_active_ids)
        ):
            main_router_property_ids = []
            main_router_hint["selected_property_ids"] = []

        # Carry-existing FIRST: when the router emitted empty selected_property_ids
        # but the prior turn locked in a property scope, honor that prior scope
        # before deriving from book_ids. Without this swap, a vague follow-up
        # like "tell me about the spa" — where the router emits []  + spa-related
        # book_ids — would silently fan out to every property that has those
        # books (Fallback A), polluting the answerer's view of which properties
        # are in scope.
        # Not when the router chose no hotel at a Pre-booking tab hotel: there that
        # means a place that is not ours (see NOT ONE OF OURS above). Carrying the
        # focus told the answerer "mysore staff is too rude" was about T24 Residency
        # and gave it only Residency's contact page; it offered our team, the guest's
        # "Yes, please" opened the form and the complaint was filed to Residency (UAT,
        # 15 Oct). Every hotel is loaded instead (Fallback B), as for any place we
        # do not have.
        if (
            not _user_broadened
            and not main_router_property_ids
            and main_router_route in {"book", "auto"}
        ):
            if _existing_active_ids and not _router_chose_no_hotel:
                main_router_property_ids = list(_existing_active_ids)

        # Deterministic repair: when the guest explicitly compares the CURRENT
        # hotel with a PREVIOUS / EARLIER one, do not rely on the router to
        # remember to union past_property_context into selected_property_ids.
        # If we leave this to the model, it can recognize the previous hotel in
        # the reason text but still forward only the current hotel, which makes
        # the answerer say it cannot compare. The closed set here is exactly the
        # hotels already discussed in this conversation.
        if self._is_compare_with_discussed_hotels_request(user_message):
            _compare_active_ids = self._normalize_property_id_list(
                list(main_router_property_ids or [])
            )
            if not _compare_active_ids and isinstance(context.pending_data, dict):
                _compare_active_ids = self._normalize_property_id_list(
                    list((context.pending_data or {}).get("_active_property_ids") or [])
                )
            _past_ctx = (
                dict((context.pending_data or {}).get("_past_property_context") or {})
                if isinstance(context.pending_data, dict)
                else {}
            )
            _compare_focus_ids = self._normalize_property_id_list(
                [*_compare_active_ids, *list(_past_ctx.keys())]
            )
            if len(_compare_focus_ids) >= 2:
                main_router_route = "book"
                main_router_property_ids = list(_compare_focus_ids)

        if (
            not _user_broadened
            and not main_router_property_ids
            and main_router_book_ids
            and not _router_chose_no_hotel
        ):
            # Fallback A: derive properties from the books the router named.
            # Only used when no prior scope exists (first-turn-ish state).
            # (Never for "no hotel" at a Pre-booking tab hotel: every hotel has the
            # same book ids, so a book would pick one of them at random.)
            try:
                from services.kb_table_reader import get_structured_book_catalog_db

                _all_books_for_derive = await get_structured_book_catalog_db(
                    hotel_code=_hotel_code_for_books,
                    property_slugs=None,
                    only_active=True,
                )
                _book_to_property = {
                    self._normalize_identifier(b.get("id")): self._normalize_identifier(b.get("property_id"))
                    for b in (_all_books_for_derive or [])
                    if isinstance(b, dict) and self._normalize_identifier(b.get("id"))
                }
                main_router_property_ids = self._normalize_property_id_list(
                    [
                        _book_to_property.get(self._normalize_identifier(bid), "")
                        for bid in main_router_book_ids
                    ]
                )
            except Exception:
                pass

        if not main_router_property_ids and main_router_route in {"book", "auto"}:
            # Fallback B: load every property's books so the answerer can answer
            # generic questions ("tell me about your hotel") instead of being
            # routed to the gpt-4o heavy orchestrator with empty book context.
            try:
                from services.kb_table_reader import get_structured_book_catalog_db

                _all_books_full = await get_structured_book_catalog_db(
                    hotel_code=_hotel_code_for_books,
                    property_slugs=None,
                    only_active=True,
                )
                main_router_property_ids = self._normalize_property_id_list(
                    list({
                        self._normalize_identifier(b.get("property_id"))
                        for b in (_all_books_full or [])
                        if isinstance(b, dict) and self._normalize_identifier(b.get("property_id"))
                    })
                )
                if not main_router_book_ids:
                    main_router_book_ids = [
                        self._normalize_identifier(b.get("id"))
                        for b in (_all_books_full or [])
                        if isinstance(b, dict) and self._normalize_identifier(b.get("id"))
                    ]
            except Exception:
                pass

        # A selection that covers essentially the WHOLE catalogue with no broaden
        # intent is a transient COVERAGE / footprint answer ('what cities do you
        # have', 'show me all your hotels') — the guest is NOT focusing on every
        # hotel. Persisting it as the focus would overwrite their real interest and
        # pollute the discussed/past-property tracking, so a later 'which did we
        # discuss' wrongly reports the whole map. Skip persisting the focus in that
        # case (the books for THIS answer still load below); keep the prior focus.
        # A genuine broaden (broaden_properties=true) DOES reset scope — persist it.
        _persist_focus_ok = True
        try:
            _name_map = (context.pending_data or {}).get("_property_name_map")
            _catalog_n = len(_name_map) if isinstance(_name_map, dict) else 0
            if (
                not _user_broadened
                and _catalog_n >= 3
                and len(main_router_property_ids) >= max(_catalog_n - 1, int(_catalog_n * 0.85))
            ):
                _persist_focus_ok = False
        except Exception:
            _persist_focus_ok = True

        if main_router_property_ids and _persist_focus_ok and main_router_route in {"book", "auto", "clarify", "service"}:
            self._persist_book_answerer_focus(
                context=context,
                focus_ids=main_router_property_ids,
                selected_phase_id=selected_phase_id,
            )

        # Contact-info book lookups must stay anchored to the active property
        # scope. Without that anchor, a generic contact-information book id can
        # resolve against the first matching property in the catalog and leak
        # the wrong hotel's phone/email into the reply.
        _contact_book_route = (
            main_router_route == "book"
            and any(self._normalize_identifier(book_id) == "contact_information" for book_id in main_router_book_ids)
        )
        if _contact_book_route and not main_router_property_ids:
            _contact_scope_ids = self._normalize_property_id_list(
                list((context.pending_data or {}).get("_active_property_ids") or [])
                if isinstance(context.pending_data, dict)
                else []
            )
            if not _contact_scope_ids and isinstance(context.pending_data, dict):
                _legacy_contact_scope = str(
                    context.pending_data.get("_selected_property_scope_id") or ""
                ).strip()
                if _legacy_contact_scope:
                    _contact_scope_ids = self._normalize_property_id_list([_legacy_contact_scope])
            if _contact_scope_ids:
                main_router_property_ids = list(_contact_scope_ids)
                main_router_hint["selected_property_ids"] = list(_contact_scope_ids)
            else:
                main_router_route = "clarify"
                main_router_hint["route"] = "clarify"
                main_router_hint["service_id"] = ""
                main_router_hint["selected_property_ids"] = []
                main_router_hint["book_ids"] = []
                main_router_hint["clarification_question"] = (
                    "Which property would you like the contact details for?"
                )
                main_router_book_ids = []

        # Layer 2 — Books to load. If the router didn't narrow within the
        # selected properties, load every active book under those properties
        # so the answerer still has full coverage.
        if not main_router_book_ids and main_router_property_ids:
            try:
                from services.kb_table_reader import get_structured_book_catalog_db

                _scoped_books = await get_structured_book_catalog_db(
                    hotel_code=_hotel_code_for_books,
                    property_slugs=list(main_router_property_ids),
                    only_active=True,
                )
                main_router_book_ids = [
                    self._normalize_identifier(row.get("id"))
                    for row in (_scoped_books or [])
                    if isinstance(row, dict) and self._normalize_identifier(row.get("id"))
                ]
            except Exception:
                main_router_book_ids = []
        # No count cap on picked books — the router can pick as many as the
        # question warrants. The downstream char-budget (route_max_chars)
        # already bounds payload size; capping book *count* would silently
        # hide whole books from the answerer.
        books_only_mode = bool(getattr(settings, "chat_kb_only_mode", False))
        # Shared book-context limits (0 => unlimited where supported).
        _setting_max_books = int(getattr(settings, "chat_llm_book_route_max_books", 0) or 0)
        _setting_max_pages_per_book = int(
            getattr(settings, "chat_llm_book_route_max_pages_per_book", 0) or 0
        )
        _setting_max_chars = int(getattr(settings, "chat_llm_book_route_max_chars", 0) or 0)
        route_max_books = _setting_max_books if _setting_max_books > 0 else 0
        route_max_pages_per_book = (
            _setting_max_pages_per_book if _setting_max_pages_per_book > 0 else 0
        )
        route_auto_char_cap = max(
            12000,
            int(getattr(settings, "chat_llm_book_route_auto_char_cap", 160000) or 160000),
        )
        route_max_chars = (
            _setting_max_chars
            if _setting_max_chars > 0
            else min(self._compute_kb_char_budget(), route_auto_char_cap)
        )
        book_route_context: dict[str, Any] = {}
        book_route_status = "not_book_route"
        if main_router_route == "book" and bool(getattr(settings, "chat_llm_book_routing_enabled", True)):
            book_route_status = "book_route_selected"

            if not isinstance(context.pending_data, dict):
                context.pending_data = {}
            _pending_book_cache = context.pending_data
            # Scope the books to THIS turn's selected properties, NOT the stale
            # active scope. `main_router_property_ids` is the narrowed set the
            # router chose for this turn (and was just persisted above). Reading
            # `_active_property_ids` here instead let a PRIOR turn's broader scope
            # leak its books in — e.g. after the guest narrowed to one Coimbatore
            # property, a sibling still sitting in `_active_property_ids` kept
            # loading its pages, so the answerer "compared" with a property the
            # guest never asked about. Fall back to the active scope only when the
            # router emitted no property focus this turn.
            _active_prop_slugs = [
                str(slug).strip()
                for slug in (
                    list(main_router_property_ids)
                    or list(_pending_book_cache.get("_active_property_ids") or [])
                )
                if str(slug).strip()
            ]
            _book_route_cache_ttl = self._session_cache_ttl_seconds(
                getattr(settings, "chat_book_route_context_cache_ttl_seconds", 300)
            )
            _book_route_now = time.time()
            _book_route_cache_key = json.dumps(
                {
                    "route": "book",
                    "book_ids": list(main_router_book_ids),
                    "active_property_ids": list(_active_prop_slugs),
                    "max_books": int(route_max_books or 0),
                    "max_pages_per_book": int(route_max_pages_per_book or 0),
                    "max_chars": int(route_max_chars or 0),
                    "kb_db": bool(getattr(settings, "kb_read_from_tables", False)),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            _cached_book_ctx_key = str(_pending_book_cache.get("_cached_book_route_context_key") or "")
            _cached_book_ctx_ts_raw = _pending_book_cache.get("_cached_book_route_context_ts")
            _cached_book_ctx_ts = 0.0
            try:
                _cached_book_ctx_ts = float(_cached_book_ctx_ts_raw or 0.0)
            except (TypeError, ValueError):
                _cached_book_ctx_ts = 0.0
            _cached_book_ctx = _pending_book_cache.get("_cached_book_route_context")
            _book_ctx_cache_fresh = (
                _cached_book_ctx_key == _book_route_cache_key
                and isinstance(_cached_book_ctx, dict)
                and bool(str(_cached_book_ctx.get("context_text") or "").strip())
                and ((_book_route_now - _cached_book_ctx_ts) <= _book_route_cache_ttl)
            )
            if _book_ctx_cache_fresh:
                book_route_context = dict(_cached_book_ctx)
                book_route_status = "book_route_context_cache_hit"
            else:
                # Phase 3: prefer DB-backed reader when enabled (property isolation
                # at SQL level, no full-library load).
                _use_db = bool(getattr(settings, "kb_read_from_tables", False))
                if _use_db and main_router_book_ids:
                    from services.kb_table_reader import build_book_context_from_ids_db

                    _hotel_code = str(getattr(context, "hotel_code", "") or "").strip()
                    _result = await build_book_context_from_ids_db(
                        hotel_code=_hotel_code,
                        book_ids=main_router_book_ids,
                        property_slugs=_active_prop_slugs or None,
                        max_chars=route_max_chars,
                    )
                    book_route_context = _result.to_dict()
                elif main_router_book_ids:
                    route_max_pages = (
                        route_max_books * route_max_pages_per_book
                        if (route_max_books and route_max_pages_per_book)
                        else 0
                    )
                    book_route_context = config_service.build_book_context_from_ids(
                        book_ids=main_router_book_ids,
                        max_books=route_max_books,
                        max_pages=route_max_pages,
                        max_chars=route_max_chars,
                    )
                else:
                    book_route_context = config_service.build_user_query_book_context(
                        query=str(user_message or "").strip(),
                        max_books=route_max_books if route_max_books else 8,
                        max_pages_per_book=route_max_pages_per_book if route_max_pages_per_book else 8,
                        max_chars=route_max_chars,
                    )
                if isinstance(book_route_context, dict) and str(
                    book_route_context.get("context_text") or ""
                ).strip():
                    _pending_book_cache["_cached_book_route_context_key"] = _book_route_cache_key
                    _pending_book_cache["_cached_book_route_context"] = book_route_context
                    _pending_book_cache["_cached_book_route_context_ts"] = _book_route_now
            if not isinstance(book_route_context, dict):
                book_route_context = {}
            if not str(book_route_context.get("context_text") or "").strip():
                main_router_hint["route"] = "auto"
                main_router_route = "auto"
                book_route_status = "book_route_empty_context_fallback_auto"
            else:
                book_route_status = "book_route_context_ready"
        elif main_router_route == "book":
            main_router_hint["route"] = "auto"
            main_router_route = "auto"
            book_route_status = "book_route_disabled_fallback_auto"
        if books_only_mode and main_router_route in {"auto", "clarify"}:
            # Strict KB-only mode: never fall back to raw full-KB blobs.
            # Build a best-effort book context for informational routes.
            if not isinstance(book_route_context, dict):
                book_route_context = {}
            if not str(book_route_context.get("context_text") or "").strip():
                book_route_context = config_service.build_user_query_book_context(
                    query=str(user_message or "").strip(),
                    max_books=route_max_books if route_max_books else 8,
                    max_pages_per_book=route_max_pages_per_book if route_max_pages_per_book else 8,
                    max_chars=route_max_chars,
                )
                if str(book_route_context.get("context_text") or "").strip():
                    book_route_status = "books_only_auto_context_ready"
                else:
                    book_route_status = "books_only_auto_context_empty"
        try:
            from services.flow_logger import log_book_routing_event

            _mr_hint = main_router_hint if isinstance(main_router_hint, dict) else {}
            log_book_routing_event(
                event="main_router_pre_orchestrator",
                session_id=str(getattr(context, "session_id", "") or ""),
                tenant_id=str(getattr(context, "hotel_code", "") or ""),
                route=main_router_route,
                payload={
                    # Top-level fields so the routing log table is legible at a glance
                    # (was empty before, making "why did book→room?" impossible to trace).
                    "user_message": str(user_message or "")[:500],
                    "service_id": _mr_hint.get("service_id") or _mr_hint.get("target_service_id"),
                    "reason": _mr_hint.get("reason"),
                    "from_topic": _mr_hint.get("from_topic") or _mr_hint.get("prior_topic"),
                    "to_topic": _mr_hint.get("to_topic") or _mr_hint.get("topic"),
                    "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
                    "main_router": self._sanitize_json(main_router_hint),
                    "book_route_status": book_route_status,
                    "allowed_service_count": len(allowed_service_ids),
                    "out_of_phase_service_count": len(out_of_phase_services_detailed),
                    "book_context": {
                        "book_ids": main_router_book_ids[:20],
                        "book_count": int(book_route_context.get("book_count") or 0),
                        "page_count": int(book_route_context.get("page_count") or 0),
                        "page_ids": list(book_route_context.get("page_ids") or [])[:30],
                        "context_chars": len(str(book_route_context.get("context_text") or "")),
                        "source_signature": str(book_route_context.get("source_signature") or ""),
                        "truncated": bool(book_route_context.get("truncated")),
                    },
                },
            )
        except Exception:
            pass
        # ── Property-scoped KB loading ──────────────────────────────────
        # Instead of loading ALL KB files into one blob and trying to scope
        # after the fact, we use the property index to load ONLY the KB pages
        # for properties the guest is currently discussing.
        #
        # Three scenarios:
        #   1) Single property (1 KB file)  → load full KB as before
        #   2) Multi-property, active IDs known (turn 2+) → load only those
        #   3) Multi-property, no active IDs (turn 1) → manifest + full pipeline
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        _pending = context.pending_data
        _cached_property_count = int(_pending.get("_known_property_scope_count") or 0)
        _cached_active_ids = self._normalize_property_id_list(list(_pending.get("_active_property_ids") or []))
        _cached_active_names = [
            str(name).strip()
            for name in list(_pending.get("_active_property_names") or [])
            if str(name).strip()
        ]
        _runtime_past_ctx = dict(_pending.get("_past_property_context") or {})
        # Legacy fallback — if old-style single fields exist but new array doesn't
        if not _cached_active_ids:
            _legacy_id = str(_pending.get("_selected_property_scope_id") or "").strip()
            _legacy_name = str(_pending.get("_selected_property_scope_name") or "").strip()
            if _legacy_id:
                _cached_active_ids = [_legacy_id]
                _cached_active_names = [_legacy_name] if _legacy_name else [_legacy_id]
        _cached_active_ids, _prefetch_pin_applied = self._apply_stay_property_pin(
            context=context,
            selected_phase_id=selected_phase_id,
            property_ids=_cached_active_ids,
            source="pre_kb_scope",
        )
        if _prefetch_pin_applied:
            _stay_property_name = str(_pending.get("_stay_property_name") or "").strip()
            if _stay_property_name and _stay_property_name not in _cached_active_names:
                _cached_active_names = [_stay_property_name, *_cached_active_names]
            context.pending_data["_active_property_ids"] = list(_cached_active_ids)
            if _cached_active_names:
                context.pending_data["_active_property_names"] = list(_cached_active_names)

        if books_only_mode:
            if main_router_route == "service":
                full_kb_text = ""
            else:
                full_kb_text = str(book_route_context.get("context_text") or "").strip()
                if not full_kb_text:
                    _books_only_context = config_service.build_user_query_book_context(
                        query=str(user_message or "").strip(),
                        max_books=route_max_books if route_max_books else 8,
                        max_pages_per_book=route_max_pages_per_book if route_max_pages_per_book else 8,
                        max_chars=route_max_chars,
                    )
                    if isinstance(_books_only_context, dict):
                        book_route_context = _books_only_context
                        full_kb_text = str(_books_only_context.get("context_text") or "").strip()
                    if full_kb_text:
                        book_route_status = "books_only_fallback_context_ready"
            property_scope = {
                "mode": "books_only",
                "active_property_id": _cached_active_ids[0] if len(_cached_active_ids) == 1 else "",
                "active_property_name": _cached_active_names[0] if len(_cached_active_names) == 1 else "",
                "matched_property_ids": list(_cached_active_ids),
                "history_property_ids": [],
                "selected_property_ids": list(_cached_active_ids),
                "property_manifest": [],
                "expected_property_count": _cached_property_count,
                "detected_property_count": _cached_property_count,
                "count_mismatch": False,
                "requires_clarification": False,
                "clarification_question": "",
            }
            active_property_id = _cached_active_ids[0] if len(_cached_active_ids) == 1 else ""
            active_property_name = _cached_active_names[0] if len(_cached_active_names) == 1 else ""
            known_property_count = _cached_property_count
            property_manifest = []
            if not main_router_book_ids:
                main_router_book_ids = [
                    str(row.get("id") or "").strip()
                    for row in list(book_route_context.get("books") or [])
                    if isinstance(row, dict) and str(row.get("id") or "").strip()
                ]
        elif main_router_route == "book" and str(book_route_context.get("context_text") or "").strip():
            full_kb_text = str(book_route_context.get("context_text") or "").strip()
            property_scope = {
                "mode": "book_route",
                "active_property_id": _cached_active_ids[0] if len(_cached_active_ids) == 1 else "",
                "active_property_name": _cached_active_names[0] if len(_cached_active_names) == 1 else "",
                "matched_property_ids": list(_cached_active_ids),
                "history_property_ids": [],
                "selected_property_ids": list(_cached_active_ids),
                "property_manifest": [],
                "expected_property_count": _cached_property_count,
                "detected_property_count": _cached_property_count,
                "count_mismatch": False,
                "requires_clarification": False,
                "clarification_question": "",
            }
            active_property_id = _cached_active_ids[0] if len(_cached_active_ids) == 1 else ""
            active_property_name = _cached_active_names[0] if len(_cached_active_names) == 1 else ""
            known_property_count = _cached_property_count
            property_manifest = []
            if not main_router_book_ids:
                main_router_book_ids = [
                    str(row.get("id") or "").strip()
                    for row in list(book_route_context.get("books") or [])
                    if isinstance(row, dict) and str(row.get("id") or "").strip()
                ]
        elif _cached_property_count > 1 and _cached_active_ids:
            # ── Fast path: multi-property, active IDs known ──
            # Load ONLY the active properties' KB pages via property index.
            # No full KB load, no text-matching, no LLM scanning.
            if main_router_route == "service":
                full_kb_text = ""
            else:
                try:
                    _scoped_result = await config_service.get_kb_text_for_properties(
                        property_ids=list(_cached_active_ids),
                        include_manifest=True,
                        include_common=True,
                        past_property_context=_runtime_past_ctx if _runtime_past_ctx else None,
                        max_sources=200,
                        allow_llm_generation=False,
                    )
                    full_kb_text = str(_scoped_result.get("scoped_kb_text") or "").strip()
                except Exception as _scoped_err:
                    print(f"[ORCH] get_kb_text_for_properties failed, falling back to full KB: {_scoped_err}")
                    full_kb_text = config_service.get_full_kb_text_with_sources(max_chars=None, max_sources=200)

            _mode = "single" if len(_cached_active_ids) == 1 else "multi"
            property_scope = {
                "mode": _mode,
                "active_property_id": _cached_active_ids[0] if len(_cached_active_ids) == 1 else "",
                "active_property_name": _cached_active_names[0] if len(_cached_active_names) == 1 else "",
                "matched_property_ids": list(_cached_active_ids),
                "history_property_ids": [],
                "selected_property_ids": list(_cached_active_ids),
                "property_manifest": [],
                "expected_property_count": _cached_property_count,
                "detected_property_count": _cached_property_count,
                "count_mismatch": False,
                "requires_clarification": False,
                "clarification_question": "",
            }
            active_property_id = _cached_active_ids[0] if len(_cached_active_ids) == 1 else ""
            active_property_name = _cached_active_names[0] if len(_cached_active_names) == 1 else ""
            known_property_count = _cached_property_count
            property_manifest = []
        else:
            # ── Full pipeline: first turn or single-property setup ──
            # For single-property setups, load the full KB directly.
            # For multi-property turn 1, the pipeline builds manifest + scoped text.
            if main_router_route == "service":
                history_text = self._build_property_scope_history_text(context=context)
                pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
                seed_property_id = self._normalize_identifier(
                    pending_raw.get("_selected_property_scope_id")
                    or pending_raw.get("selected_property_id")
                    or ""
                )
                kb_context = await config_service.build_property_scoped_kb_context(
                    user_message=user_message,
                    history_text=history_text,
                    seed_property_id=seed_property_id,
                    max_sources=200,
                    max_scope_chars=1,
                    preloaded_full_kb_text="",
                    allow_llm_generation=False,
                )
            else:
                if not isinstance(context.pending_data, dict):
                    context.pending_data = {}
                _pending_data = context.pending_data
                _kb_cache_ttl_seconds = self._session_cache_ttl_seconds(300.0)
                _kb_now = time.time()
                _cached_kb_text = str(_pending_data.get("_cached_kb_text") or "").strip()
                _cached_kb_ts_raw = _pending_data.get("_cached_kb_ts")
                _cached_kb_ts = 0.0
                try:
                    _cached_kb_ts = float(_cached_kb_ts_raw or 0.0)
                except (TypeError, ValueError):
                    _cached_kb_ts = 0.0
                _kb_cache_fresh = bool(_cached_kb_text) and (
                    (_kb_now - _cached_kb_ts) <= _kb_cache_ttl_seconds
                )
                if _kb_cache_fresh:
                    full_kb_text = _cached_kb_text
                else:
                    full_kb_text = config_service.get_full_kb_text_with_sources(
                        max_chars=None,
                        max_sources=200,
                    )
                    if full_kb_text:
                        _pending_data["_cached_kb_text"] = full_kb_text
                        _pending_data["_cached_kb_ts"] = _kb_now
                        _pending_data["_cached_kb_version"] = str(int(_kb_now))
                kb_context = await self._prepare_full_kb_context_for_message(
                    user_message=user_message,
                    context=context,
                    full_kb_text=full_kb_text,
                )
            full_kb_text = str((kb_context or {}).get("full_kb_text") or "").strip()
            property_scope = (kb_context or {}).get("property_scope") if isinstance(kb_context, dict) else {}
            if not isinstance(property_scope, dict):
                property_scope = {}

            active_property_id = str(property_scope.get("active_property_id") or "").strip()
            active_property_name = str(property_scope.get("active_property_name") or "").strip()
            if not isinstance(context.pending_data, dict):
                context.pending_data = {}
            if active_property_id:
                self._sync_property_scope_pending(
                    pending_data=context.pending_data,
                    property_ids=[active_property_id],
                    selected_property_name=active_property_name,
                )
            known_property_count = int(property_scope.get("detected_property_count") or 0)
            if known_property_count > 0:
                context.pending_data["_known_property_scope_count"] = known_property_count
            property_manifest = property_scope.get("property_manifest", []) if isinstance(property_scope, dict) else []
            if property_manifest:
                context.pending_data["_property_name_map"] = self._merge_property_name_map(
                    context.pending_data.get("_property_name_map"),
                    property_manifest if isinstance(property_manifest, list) else [],
                )

        # Augment full_kb_text with extracted_knowledge from ALL service KB records.
        # This ensures admin-entered KB content (stored in service_kb_records, not in flat files)
        # is always visible to the orchestrator when it needs to answer directly.
        service_kb_records = capabilities_summary.get("service_kb_records", [])
        if (
            not books_only_mode
            and isinstance(service_kb_records, list)
            and main_router_route not in {"book", "service"}
        ):
            extracted_parts: list[str] = []
            for rec in service_kb_records[:30]:
                if not isinstance(rec, dict):
                    continue
                ek = str(rec.get("extracted_knowledge") or "").strip()
                sid = str(rec.get("service_id") or "").strip()
                if ek and sid:
                    extracted_parts.append(f"[Service: {sid}]\n{ek}")
            if extracted_parts:
                combined_service_kb = "\n\n---\n\n".join(extracted_parts)
                full_kb_text = (
                    (full_kb_text + "\n\n" if full_kb_text else "")
                    + "=== SERVICE KNOWLEDGE BASE ===\n\n"
                    + combined_service_kb
                )

        history_bundle = self._build_history_bundle(
            context=context,
            memory_snapshot=memory_snapshot,
            last_window_messages=10,
            last_window_chars=max(3000, int(getattr(settings, "llm_orchestration_history_chars", 8000) or 8000)),
            full_window_messages=max(
                60,
                int(getattr(settings, "llm_orchestration_full_history_messages", 120) or 120),
            ),
            full_window_chars=max(
                7000,
                int(getattr(settings, "llm_orchestration_full_history_chars", 12000) or 12000),
            ),
        )
        stay_property_pin_id = str(_pending.get("_stay_property_id") or "").strip()
        stay_property_pin_name = str(_pending.get("_stay_property_name") or "").strip()
        property_scope_payload = {
            "mode": str(property_scope.get("mode") or "").strip(),
            "active_property_id": active_property_id,
            "active_property_name": active_property_name,
            "matched_property_ids": list(property_scope.get("matched_property_ids") or []),
            "history_property_ids": list(property_scope.get("history_property_ids") or []),
            "selected_property_ids": list(property_scope.get("selected_property_ids") or []),
            "detected_properties": [
                {
                    "id": str(row.get("id") or "").strip(),
                    "name": str(row.get("name") or "").strip(),
                    "aliases": list(row.get("aliases") or []),
                }
                for row in property_manifest[:12]
                if isinstance(row, dict)
            ],
            "expected_property_count": int(property_scope.get("expected_property_count") or 0),
            "detected_property_count": int(property_scope.get("detected_property_count") or 0),
            "count_mismatch": bool(property_scope.get("count_mismatch")),
            "requires_clarification": bool(property_scope.get("requires_clarification")),
            "clarification_question": str(property_scope.get("clarification_question") or "").strip(),
            "stay_property_pin": {
                "enabled": bool(
                    stay_property_pin_id
                    and self._is_stay_property_pin_phase(selected_phase_id)
                ),
                "property_id": stay_property_pin_id,
                "property_name": stay_property_pin_name,
            },
        }
        # Property clarification is now fully LLM-driven.
        # The orchestrator system prompt contains property resolution rules and
        # receives property_scope in the payload — it decides when to ask for
        # clarification vs answer from the full KB. No hardcoded guard needed.
        faq_candidates = self._build_faq_candidates(capabilities_summary=capabilities_summary)
        _now = datetime.now(UTC)
        _turn_response_language = self._response_language_preference(
            user_message=language_user_message,
            history_rows=history_bundle.get("history_last_10", []),
            last_assistant_message=str(history_bundle.get("last_assistant_message") or ""),
            router_language=getattr(context, "_turn_response_language", None),
        )
        _previous_response_language = self._last_assistant_response_language(context)
        _language_switch = bool(
            _previous_response_language
            and _turn_response_language
            and str(_previous_response_language.get("language") or "").strip().casefold()
            != str(_turn_response_language.get("language") or "").strip().casefold()
        )
        (
            _answer_history,
            _answer_last_assistant,
            _language_switch_history_suppressed,
        ) = self._history_for_explicit_language_switch(
            history_rows=history_bundle.get("history_last_10", []),
            last_assistant_message=str(history_bundle.get("last_assistant_message") or ""),
            current_user_message=str(language_user_message or user_message or ""),
            language_switch=_language_switch,
        )
        payload = {
            "trace_id": orchestration_trace_id,
            "timestamp": _now.isoformat(),
            "current_date": _now.strftime("%Y-%m-%d"),
            "current_day": _now.strftime("%A"),
            "user_message": str(user_message or ""),
            "response_language": self._sanitize_json(_turn_response_language),
            "response_language_preference": self._sanitize_json(_turn_response_language),
            "previous_response_language": self._sanitize_json(_previous_response_language),
            "language_switch": _language_switch,
            "language_switch_history_suppressed": _language_switch_history_suppressed,
            "state": context.state.value,
            "pending_action": str(context.pending_action or ""),
            "pending_action_context": self._build_pending_action_context(context),
            "pending_data_public": self._sanitize_json(self._coerce_public_pending(context.pending_data)),
            "last_active_service": {
                "id": str((context.pending_data or {}).get("_last_service_id") or ""),
                "name": str((context.pending_data or {}).get("_last_service_name") or ""),
            },
            "selected_phase": {
                "id": selected_phase_id,
                "name": selected_phase_name,
            },
            "memory_summary": self._summary_within_budget(memory_snapshot.get("summary"), 6000),
            "memory_facts": self._sanitize_json(memory_snapshot.get("facts", {})),
            "suspended_services": [
                {"service_id": s.get("service_id"), "service_name": s.get("service_name"), "collected_so_far": list((s.get("pending_data") or {}).keys())}
                for s in (context.suspended_services or [])
            ],
            "property_scope": self._sanitize_json(property_scope_payload),
            "active_property_ids": list(_cached_active_ids) if _cached_active_ids else [],
            "past_property_context": _runtime_past_ctx if _runtime_past_ctx else {},
            "main_router": self._sanitize_json(main_router_hint),
            "book_route_context": self._sanitize_json(
                {
                    "route": main_router_route,
                    "book_ids": main_router_book_ids,
                    "book_count": int(book_route_context.get("book_count") or 0),
                    "page_count": int(book_route_context.get("page_count") or 0),
                    "source_signature": str(book_route_context.get("source_signature") or ""),
                }
            ),
            "full_knowledge_base": full_kb_text or None,
            "faq_candidates": self._sanitize_json(faq_candidates),
            "history": _answer_history,
            "history_last_10": _answer_history,
            "full_history_context": history_bundle.get("full_history_context", {}),
            "last_user_message": str(history_bundle.get("last_user_message") or ""),
            "last_assistant_message": _answer_last_assistant,
            "services": self._sanitize_json([
                {k: v for k, v in svc.items() if k not in ("extracted_knowledge", "generated_system_prompt", "service_prompt_pack")}
                for svc in services_snapshot
                if isinstance(svc, dict)
            ]),
            "policy_snapshot": {
                "selected_phase_id": selected_phase_id,
                "selected_phase_name": selected_phase_name,
                "allowed_service_ids_in_current_phase": allowed_service_ids,
                "allowed_service_names_in_current_phase": allowed_service_names,
                "out_of_phase_services": self._sanitize_json(out_of_phase_services),
                "phase_service_policy": {
                    "current_phase_id": selected_phase_id,
                    "current_phase_name": selected_phase_name,
                    "allowed_services": self._sanitize_json(allowed_services_detailed),
                    "blocked_out_of_phase_services": self._sanitize_json(out_of_phase_services_detailed),
                },
                "ticketing_enabled_by_service": {
                    self._normalize_identifier(row.get("id")): bool(row.get("ticketing_enabled", True))
                    for row in services_snapshot
                    if isinstance(row, dict) and self._normalize_identifier(row.get("id"))
                },
                "execution_rules": [
                    "Response text must be final user-facing text; do not rely on handler text generation.",
                    "Out-of-phase service asks must not trigger transactional execution.",
                    "Ticket creation intent is allowed only when service ticketing is enabled.",
                    "KB-grounded generic requests may keep target_service_id empty only when backed by KB evidence.",
                ],
            },
            "complaint_routing_note": (
                "IMPORTANT: 'ticketing_enabled=true' on a service means the service CAN raise a support ticket "
                "if the guest reports a problem with it — it does NOT mean the service is a complaint handler. "
                "Room bookings, food orders, and all other service requests are handled as normal service requests. "
                "Set action=create_ticket only when the request makes sense in the guest's current journey stage. "
                "(1) The guest needs staff to physically do something during their stay and they are actually in-house (missing items, maintenance, housekeeping) — use ticket.category='request'. "
                "(2) The guest is reporting a real complaint that is physically possible right now in their current stage — use ticket.category='complaint'. "
                "A pre-arrival guest saying 'my room is dirty', 'there is a cockroach in my room', 'send towels', or similar is NOT a valid ticket here unless they explicitly ask to speak to staff. "
                "Asking about a service, requesting a booking, or asking for information is NEVER a reason to create a ticket."
            ),
            "response_contract": {
                "normalized_query": "string",
                "intent": "core intent",
                "confidence": "0..1",
                "action": "respond_only|collect_info|dispatch_handler|create_ticket|resume_pending|cancel_pending",
                "target_service_id": "exact service id from services list when service-specific; empty for KB-grounded generic requests",
                "response_text": "assistant response",
                "pending_action": "string|null",
                "pending_data_updates": {"slot": "value"},
                "missing_fields": ["field_id"],
                "answered_current_query": "bool",
                "blocking_fields": ["field_id"],
                "deferrable_fields": ["field_id"],
                "followup_question": "single question or empty",
                "suggested_actions": ["short action"],
                "use_handler": "bool",
                "handler_intent": "intent string when use_handler=true",
                "interrupt_pending": "bool",
                "resume_pending": "bool",
                "cancel_pending": "bool",
                "requires_human_handoff": "bool",
                "selected_property_name": "name of the property the guest is currently discussing, or empty if general/unknown",
                "selected_property_ids": ["property id(s) the guest is interested in — from detected_properties list"],
                "ticket": {
                    "required": "bool",
                    "ready_to_create": "bool",
                    "generic_request": "bool",
                    "phase_applicable": "bool|null",
                    "approval_required": "bool",
                    "reason": "string",
                    "issue": "string",
                    "category": "string",
                    "sub_category": "string",
                    "priority": "low|medium|high|critical",
                    "evidence": ["1-3 short KB facts backing the request"],
                },
                "metadata": {
                    "faq_resolution": [
                        {
                            "faq_id": "faq identifier from faq_candidates",
                            "relation": "override|merge|ignore",
                            "applied": "bool",
                            "confidence": "0..1",
                            "reason": "short explanation",
                            "fact": "specific fact used from FAQ",
                        }
                    ],
                    "resolved_facts": [
                        {
                            "source": "faq:<id>|kb",
                            "fact": "short factual line used in final answer",
                        }
                    ],
                    "any": "json",
                },
            },
        }

        # --- WEATHER INJECTION ---
        # Weather was fetched upstream once per turn using the router's
        # `weather_for` flag. Just read the stashed result here.
        _turn_wx = (
            context.pending_data.get("_turn_live_weather")
            if isinstance(context.pending_data, dict)
            else None
        )
        if isinstance(_turn_wx, dict) and _turn_wx:
            if not isinstance(payload.get("memory_facts"), dict):
                payload["memory_facts"] = {}
            payload["memory_facts"]["live_weather"] = _turn_wx
        # -------------------------

        system_prompt = (
            "You are the main concierge assistant for this hotel. "
            "For informational questions, answer fully yourself from the hotel's KB. "
            "For bookings, orders, or service execution, never collect transactional details yourself and route to the right service agent when one is available.\n"
            "You are the single authority on routing and responding — no other layer will override your decision.\n"
            "\n"
            "=== ABSOLUTE LANGUAGE RULE ===\n"
            "NEVER use the words pre_booking, pre_checkin, during_stay, post_checkout, 'current phase', "
            "'this phase', 'not available in this phase', or any internal system terminology in any guest-facing response. "
            "When a service is unavailable right now, phrase it naturally: "
            "'That will be available once you check in', 'We can arrange this during your stay', "
            "'This is something we can help with when you arrive', etc. "
            "Guests must never know about internal phase names.\n"
            "\n"
            "=== SERVICE AVAILABILITY (read before every step) ===\n"
            "The current guest journey phase is in selected_phase.id. "
            "The ONLY services you may dispatch or offer are those listed in allowed_service_ids_in_current_phase. "
            "Services listed in blocked_out_of_phase_services are NOT available now — do NOT dispatch to them, do NOT offer them as options, do NOT ask clarification questions about them. "
            "If the guest asks for something that only maps to blocked_out_of_phase_services: "
            "1) share factual info from the KB about what the service offers, "
            "2) explicitly state that booking/ordering/requesting it is not possible right now, "
            "3) use the 'availability_hint' field from that service entry to tell the guest when it will be available. "
            "Exception: if NO configured service matches, you may keep target_service_id empty and handle it as a KB-grounded generic request ONLY when the guest is asking for information, or when it is clearly a genuine manual hotel/staff task that may need human follow-up. "
            "If the guest is explicitly trying to execute/book/order/reserve/request something now and no configured service matches, KB facts do NOT make it executable. "
            "Set action=respond_only, keep target_service_id empty, state clearly that the bot cannot do that right now, and offer staff connection only when the request is a genuine manual hotel/staff task or the guest explicitly asks for human help. "
            "Do NOT answer with descriptive service details as if the bot can perform the task. Only do this when you can cite KB evidence; never guess.\n"
            "Phrase this naturally — NEVER mention phase names.\n"
            "\n"
            "=== PRE-ARRIVAL REQUEST SCOPE (reason about the request, not keywords) ===\n"
            "Read selected_phase.id. When it indicates the guest has NOT yet arrived / checked in "
            "(a pre-booking / pre-arrival / pre-check-in stage), any request that can ONLY be fulfilled once they are "
            "physically staying at the hotel is NOT actionable now. Judge the REQUEST itself — ask yourself: 'could "
            "this be done before the guest has even checked in?'. In-stay-only actions include housekeeping (cleaning, "
            "towels, fixing something in the room), in-room maintenance (AC, plumbing, TV), room service / in-room "
            "dining delivery, spa appointments, and restaurant table reservations for the stay — plus anything tied to "
            "a specific room or to being on-property. This list is illustrative, not exhaustive: use judgment.\n"
            "This ALSO covers a CURRENT-PROBLEM report about a room or in-stay experience where the guest speaks as if "
            "ALREADY in a room — 'I have an issue with my bed', 'my bed is broken', 'my room is dirty', 'the AC isn't "
            "working in my room', 'the TV is broken'. In a pre-arrival stage the guest has no room yet, so the problem "
            "cannot be real now: do NOT empathize-then-escalate, do NOT route to a support service, and do NOT ask "
            "'would you like our team to reach out'. "
            "DISTINGUISH this from a plain INFORMATION question about a room feature — 'what kind of bed do rooms have', "
            "'is the bed king-size', 'do rooms have a bathtub', 'tell me about the beds' — which is NOT a complaint: "
            "ANSWER those normally from the KB and never decline them.\n"
            "For such an in-stay request OR in-stay complaint during a pre-arrival stage: DO NOT route it to any "
            "support / contact / intake service, DO NOT open a form, DO NOT create a ticket, DO NOT offer a staff "
            "callback. Set action=respond_only with target_service_id empty and warmly decline — explain it is "
            "something the team takes care of during their stay / once they have checked in, then offer to help with a "
            "booking or any pre-arrival question instead. Phrase naturally; never mention internal phase names.\n"
            "NOT in-stay actions (these still follow their normal paths, including staff handoff): booking a room/stay, "
            "event / venue / banquet / meeting-space bookings, general pre-arrival questions, help with an existing "
            "booking or reservation, and any explicit request to talk to a human. The staff/manual-task exception above "
            "covers ONLY these genuine pre-arrival concerns — it NEVER covers in-stay actions.\n"
            "A room/suite booking ('book the suite', 'id like to book the suite', 'i want to book the suite', any named "
            "room type) is ALWAYS a room booking, NEVER an in-stay action — do NOT apply this decline to it even when the "
            "immediately prior turn was an in-stay decline (water / towels / housekeeping). A previous in-stay decline "
            "must NOT carry over onto a room-booking request; route it to the room booking flow as normal.\n"
            "\n"
            "=== INFORMATIONAL ANSWER CLOSING RULE ===\n"
            "When action=respond_only, answer the guest's question fully but do NOT volunteer booking, reservation, ordering, request, or availability help in the closing.\n"
            "Do NOT append lines like 'Would you like help booking?', 'If you'd like, I can arrange that', or 'Would you like assistance making a reservation?'.\n"
            "Only list currently available services when the guest explicitly asks what services or options are available right now.\n"
            "\n"
            "=== MAIN ROUTER HINT (authoritative pre-decision) ===\n"
            "The payload contains main_router.route from a dedicated routing pass: service|book|clarify|auto.\n"
            "Honor it unless it clearly conflicts with safety or available context.\n"
            "If main_router.route=service and main_router.service_id is valid, route to that service.\n"
            "If main_router.route=book, keep target_service_id empty and answer from full_knowledge_base, "
            "which may already be narrowed to selected books.\n"
            "If main_router.route=clarify, ask main_router.clarification_question as-is (action=respond_only).\n"
            "\n"
            "=== KNOWLEDGE PRIORITY ===\n"
            "For every factual or informational ask, inspect full_knowledge_base carefully before answering.\n"
            "If the answer exists anywhere in full_knowledge_base, use those specific facts directly.\n"
            "Do not give a generic fallback if full_knowledge_base already contains the needed detail.\n"
            "Prefer complete factual answers over generic summaries. If the KB lists options or specific offerings, name them explicitly.\n"
            "Never generalize one fact into a different or stronger claim. A room amenity, nearby landmark, or service mention does NOT prove a different facility, menu, package, policy, or availability unless the KB explicitly says so.\n"
            "When the guest asks for one exact detail or field and that exact detail is NOT present in the KB, say that exact detail is not available. Do NOT fill the gap with nearby facts unless the guest also asked for those.\n"
            "Comparative asks such as cheapest, most affordable, best value, best, largest, smallest, closest, or similar need an explicit KB basis. If price/size/distance/rating data is missing, say that basis is not available instead of naming a winner.\n"
            "When the guest explicitly asks for room size, area, or dimensions and the KB contains it, include the exact value.\n"
            "COMPLETENESS: When listing rooms, packages, amenities, or any offerings — list ALL of them from the KB. "
            "Never show a partial list. If the KB has 5 room types, include all 5, but present each one selectively instead of dumping every stored field unless the guest asked for those specs. "
            "Do NOT say 'and more' or summarize — be exhaustive.\n"
            "ROOM PRESENTATION STYLE: For room options, lead with what makes each room uniquely appealing to guests "
            "(experience, view, inclusions, standout features). Do not lead with square-foot/area specs, "
            "and mention size only if the guest explicitly asks for size/area.\n"
            "When a short summary is enough, prioritize feel, standout benefits, and who the option suits over dry specification dumps.\n"
            "\n"
            "=== FAQ RESOLUTION (SMART MERGE/OVERRIDE) ===\n"
            "The payload includes faq_candidates (admin-curated Q/A entries).\n"
            "Treat faq_candidates as high-priority current facts, especially for temporary updates and offer inclusions.\n"
            "When FAQ and KB conflict, FAQ wins for current reality (relation='override').\n"
            "When FAQ adds extra detail not present in KB, include BOTH (relation='merge').\n"
            "If FAQ is unrelated, ignore it (relation='ignore').\n"
            "Never drop a relevant FAQ inclusion. Example: if an FAQ says Lux Suite includes complimentary dinner,\n"
            "your final room answer must include that inclusion when describing Lux Suite.\n"
            "For amenities/facilities asks, enforce status consistency: if FAQ says an amenity is under maintenance\n"
            "or unavailable, do NOT present it as open/available in the same response.\n"
            "For broad amenities list answers, separate into clear groups such as 'Currently available' and\n"
            "'Temporarily unavailable', so status is unambiguous.\n"
            "If helpful, you may mention steady-state context in this pattern: 'Usually X, but currently Y.'\n"
            "Run a final self-check before output: no amenity appears in both available and unavailable form.\n"
            "For every relevant FAQ candidate, write metadata.faq_resolution with faq_id, relation, applied, confidence, and reason.\n"
            "Also write metadata.resolved_facts with the compact facts actually used in response_text (source=faq:<id> or kb).\n"
            "\n"
            "=== PROPERTY RESOLUTION (YOU decide — no hardcoded rules) ===\n"
            "The payload includes:\n"
            "  - property_scope.detected_properties: ALL properties in the KB\n"
            "  - active_property_ids: properties the guest is currently interested in (may be empty, 1, or multiple)\n"
            "  - past_property_context: brief summaries of properties the guest discussed earlier but moved on from\n"
            "  - full_knowledge_base: FULL KB for active properties (or ALL properties if none active yet)\n"
            "Read these AND conversation history BEFORE deciding.\n"
            "\n"
            "CRITICAL — ALWAYS OUTPUT PROPERTY TRACKING:\n"
            "  In EVERY response, set these two fields in your JSON output:\n"
            "  - selected_property_name: the property name the guest is currently interested in (empty string if general/unknown)\n"
            "  - selected_property_ids: array of ALL property IDs the guest is currently interested in (from detected_properties list, empty array if general/unknown)\n"
            "  HOW THIS WORKS:\n"
            "  - These fields control which property KBs the system loads on the NEXT turn.\n"
            "  - If you set selected_property_ids to ['mumbai_orchid', 'ira_orchid_mumbai'], BOTH properties' full KB will be available next turn.\n"
            "  - If you set it to just ['mumbai_orchid'], only that property's KB will be loaded next turn.\n"
            "  - If you leave it empty, ALL properties' KB will be available next turn.\n"
            "  - IMPORTANT: When the guest is discussing MULTIPLE properties (e.g. 'which Mumbai hotel has offers?', or you just listed rooms from 2 hotels), "
            "include ALL relevant property IDs — do NOT pick just one.\n"
            "  - If the guest switches property ('actually I want Shimla'), update to the new property's ID. The old one moves to past_property_context automatically.\n"
            "  - If the guest asks about a property in past_property_context, add it back to selected_property_ids to get full details.\n"
            "\n"
            "WHEN TO ASK which property:\n"
            "  - ONLY when the guest asks a property-specific question (check-in time, room types, amenities, pricing, booking) "
            "AND you genuinely cannot determine which property from conversation history or the current message.\n"
            "  - If the guest already mentioned or selected a property earlier in the conversation, use that — do NOT re-ask.\n"
            "  - If active_property_ids already has entries, the guest is discussing those — use them.\n"
            "\n"
            "WHEN TO ANSWER WITHOUT ASKING which property:\n"
            "  - General/comparative questions: 'which is best for couples', 'recommend a location', 'top 5 for adventure', "
            "'how many properties', 'list all', 'compare', 'suggest', 'plan a trip', 'budget options' → answer from ALL properties.\n"
            "  - Exploratory/planning questions: 'I want to travel', 'where should I go', 'family trip' → recommend from full KB.\n"
            "  - When the guest mentions a CITY (e.g. 'Mumbai') and multiple properties are in that city → include ALL of them in selected_property_ids and answer about all.\n"
            "  - Greetings, chit-chat, general hotel questions → answer directly.\n"
            "  - ANY question where the KB gives a clear answer across properties → answer it.\n"
            "\n"
            "PROPERTY SWITCHING:\n"
            "  - If the guest says 'actually I want Shimla' or 'let me try Mumbai instead', treat this as a property switch.\n"
            "  - Acknowledge the switch naturally and start answering from the new property's KB.\n"
            "  - Update selected_property_ids to the new property (the system moves old ones to past context automatically).\n"
            "\n"
            "NEVER ask for property clarification twice in a row. If the last assistant message already asked, "
            "try to answer from whatever context you have or ask a different clarifying question.\n"
            "NEVER ask 'Which property would you like details for?' as a robotic default — phrase it naturally based on what the guest asked.\n"
            "\n"
            "POST-BOOKING PROPERTY FOCUS (pre_checkin / during_stay / post_checkout):\n"
            "When stay_property_pin.enabled is true, the guest has BOOKED at a specific property — that is their hotel.\n"
            "  - Answer ONLY from that property's knowledge. This is where they are staying.\n"
            "  - Do NOT volunteer information about other properties unless the guest explicitly asks "
            "(e.g. 'what about your Mumbai hotel?', 'do you have this at another location?').\n"
            "  - Treat the pinned property as the default for ALL questions — rooms, amenities, dining, services, policies.\n"
            "  - If the guest asks something generic like 'what time is checkout?', answer for their booked property only.\n"
            "  - When active booking context exists, treat that stay as the default frame of reference for interpreting the message.\n"
            "  - Resolve ambiguity toward the guest's own stay, not toward generic hotel information.\n"
            "  - If a question can be answered from the guest's active stay plus the booked property's knowledge, answer that way.\n"
            "  - Do NOT default to a general hotel-policy answer when a more guest-specific answer is already grounded in context.\n"
            "  - Ask a clarifying question only when there is genuine ambiguity that cannot be resolved from active booking context, booked-property context, or conversation history.\n"
            "\n"
            "=== STEP 1: READ HISTORY ===\n"
            "Always read history_last_10 first, then full_history_context before deciding anything. "
            "Use last_user_message and last_assistant_message as high-priority anchors for continuity. "
            "Resolve pronouns ('it', 'that', 'same', 'this') against the last assistant message.\n"
            "\n"
            "=== GUEST FACTS (hotel already knows these — never re-ask) ===\n"
            "The payload includes memory_facts and known_context with data the hotel already has on file.\n"
            "Think like a hotel: a checked-in guest's name, room number, phone, and email are ALWAYS on file.\n"
            "  - during_stay / post_checkout: NEVER ask for guest name, room number, phone, or email.\n"
            "  - pre_checkin: NEVER ask for name, phone, or email — they were collected at booking.\n"
            "  - pre_booking: only request contact info if the service genuinely needs it and it is absent from known_context.\n"
            "UNIVERSAL: If a value is already in memory_facts or known_context, treat it as known and do NOT ask for it again.\n"
            "When a service agent is dispatched, it will also receive known_context — it will handle the details.\n"
            "\n"
            "=== STEP 2: MID-FLOW CHECK ===\n"
            "Read pending_action and pending_action_context carefully. "
            "If pending_action is set, the guest is mid-flow in a service — read pending_action_context to understand exactly what was happening. "
            "Stay on that service and route back to it immediately using action=dispatch_handler with the same target_service_id. "
            "If pending_action starts with collect_generic_kb_request or confirm_generic_kb_request, continue that same KB-grounded request yourself. "
            "Keep target_service_id empty unless a configured service later becomes clearly correct. "
            "A short reply that adds no new information (a bare acknowledgement, agreement, refusal, a number, a name, or a date) when pending_action is set is normally a direct continuation of that flow — do not re-route it. "
            "EXCEPTION — staff / support / contact intake. Read your OWN immediately preceding message and judge what it actually did. "
            "A bare acknowledgement counts as agreement ONLY when that message ASKED the guest a question seeking their consent to involve staff (e.g. you asked whether they would like you to connect them, pass it to the team, or arrange a callback) AND the guest's reply agrees to it. "
            "Merely MENTIONING the team, or STATING that you can raise something with them, is NOT asking — a statement invites no answer, so a short reply after it is not consent. "
            "If your previous message did not ask such a question, a bare acknowledgement is NOT agreement: do NOT set target_service_id to a support/contact service and do NOT emit a collecting action for it. Acknowledge briefly and ask what they need. "
            "Judge this from the MEANING of your previous message and the guest's reply in any language — never from a fixed list of words. "
            "A VAGUE MESSAGE IS NOT A SUPPORT REQUEST: when the guest says something with no specific request in it (for example just saying they need help), that alone does NOT mean they want staff. "
            "Do NOT select a support / contact / intake service for it. Let the reply offer a couple of genuinely relevant things for this property so they can choose — without reciting a fixed menu. Only treat it as a support request once they actually ask for a person or describe a problem that needs one. "
            "NEVER SAY YOU RAISED SOMETHING YOU DID NOT RAISE: only tell the guest you have raised / logged / passed on their request, or that the team will get back to them, when you are ACTUALLY opening the intake form or creating the ticket in this same turn. "
            "If you are not doing that, no ticket exists and nobody is notified, so saying it is a false promise. In that case either ASK whether they want you to involve staff and wait for their answer, or simply help with what they asked — but do not claim any handoff happened. "
            "A question about the current service (e.g. asking about vehicle type during airport transfer, asking about room details during room booking, asking about menu during table booking) is NOT an interrupt — dispatch to the same service agent so it can answer and continue collecting slots. "
            "When the guest confirms a booking ('yes', 'yes confirm', 'ok', 'proceed', 'go ahead') while pending_action is set: dispatch to the service agent — the service agent owns confirmation and ticket creation. Do NOT set action=create_ticket yourself. "
            "When the guest confirms a KB-grounded generic request while pending_action starts with confirm_generic_kb_request: "
            "set action=create_ticket, target_service_id='', ticket.required=true, ticket.ready_to_create=true, and keep the same KB evidence. "
            "When the guest cancels or refuses ('no', 'cancel', 'stop', 'forget it') while pending_action is set: set action=cancel_pending and acknowledge politely. "
            "Only interrupt the pending flow if the guest explicitly and clearly asks to start a completely unrelated and different service.\n"
            "Also check last_active_service — if it has an id, the guest was just interacting with that service last turn. "
            "If the current message is still related to that service (questions about its menu, options, availability, booking, or anything that service handles), dispatch to it again. "
            "Only switch away if the guest clearly asks for a different service or a completely unrelated topic.\n"
            "\n"
            "=== STEP 2B: AMBIGUITY CHECK ===\n"
            "If the message is short, vague, or has no clear intent AND pending_action is empty AND last_assistant_message does not provide enough context to interpret it — "
            "do not guess at intent and do not route to any service. Set action=respond_only and generate a friendly clarification question asking what the guest needs. "
            "Example: 'Could you tell me a bit more about what you are looking for?'\n"
            "\n"
            "=== STEP 2C: MULTI-SERVICE CLARIFICATION ===\n"
            "ONLY applies when ALL of these are true: pending_action is empty AND the guest's message is a service request.\n"
            "PHASE GATE — do this FIRST before anything else in this step:\n"
            "  1. Look at allowed_service_ids_in_current_phase (the list of services available RIGHT NOW).\n"
            "  2. Ask: does the guest's request match ANY service in that list?\n"
            "  3. If ZERO services in allowed_service_ids_in_current_phase match → STOP. Skip this step. Go to STEP 3B immediately.\n"
            "  4. NEVER ask a clarification question that offers services from blocked_out_of_phase_services.\n"
            "If the phase gate passes (at least one allowed service matches):\n"
            "  - Check: could the request belong to TWO OR MORE services in allowed_service_ids_in_current_phase?\n"
            "  - Compare service names and descriptions: if multiple services are plausible for the same ask, treat it as ambiguous.\n"
            "  - If yes → ask ONE short question naming only the allowed matching services.\n"
            "  - In ambiguity cases, keep target_service_id empty and do not dispatch yet.\n"
            "  - If only one matches → dispatch directly (no question).\n"
            "  - If the message is specific enough to rule out ambiguity → dispatch directly.\n"
            "  - If guest is answering a previous clarification → dispatch to what they picked.\n"
            "\n"
            "=== STEP 3: DECIDE WHAT THE GUEST NEEDS ===\n"
            "\n"
            "A) INFORMATION QUESTION — guest is asking about hotel facilities, timings, policies, menus, room types, or any general details.\n"
            "   -> Answer directly from full_knowledge_base. Set action=respond_only.\n"
            "   -> If full_knowledge_base contains specific offerings, treatments, timings, policies, options, or amenities, include them explicitly instead of giving a generic summary.\n"
            "   -> Asking about room types, availability, prices, or facilities is NEVER a complaint.\n"
            "   -> Never invent facts. If details are not present, say they are not available.\n"
            "\n"
            "B) SERVICE REQUEST — guest wants to book, order, request, or arrange something.\n"
            "   -> From the very first message requesting a service — even just 'I need a room', 'book a table', 'get me a cab' — IMMEDIATELY set action=dispatch_handler. Do not answer first, do not ask for details yourself.\n"
            "   -> The service agent handles ALL slot collection and responses. Your job is only to dispatch to the right service.\n"
            "   -> target_service_id MUST be an exact ID from allowed_service_ids_in_current_phase (e.g. 'room_booking_support'). NEVER use 'complaint', 'complaint_service', 'complain', or any complaint-flavored ID for a service request.\n"
            "   -> If the service is only in blocked_out_of_phase_services: set action=respond_only. "
            "Provide factual information about the service (what it offers, hours, pricing if known). "
            "Then EXPLICITLY tell the guest that booking/ordering/requesting this service is not possible at this moment. "
            "Use the 'availability_hint' field from that service's entry in blocked_out_of_phase_services to tell the guest WHEN it will become available. "
            "Examples: 'We do offer spa treatments from 9 AM to 11 PM — however, spa bookings can only be made once you check in. "
            "I will be happy to help you book a session then!', "
            "'Airport transfers are available — I can help you arrange one after your booking is confirmed.', "
            "'In-room dining has a wonderful menu! You will be able to place orders once you check in.' "
            "The key: share the info, clearly state you cannot book/order it now, and warmly tell them when they can. "
            "Do NOT promise, invite, offer, or imply the guest can use it now. NEVER mention phase names (pre_checkin, during_stay, etc.).\n"
            "   -> Wanting to book a room, order food, or request any service is NEVER a complaint.\n"
            "   -> If NO allowed configured service matches, but full_knowledge_base clearly says the hotel does allow or handle this request "
            "in the guest's current context (including cases that require staff approval), you may manage it as a KB-grounded generic request.\n"
            "      For a KB-grounded generic request:\n"
            "      - keep target_service_id empty\n"
            "      - set ticket.generic_request=true\n"
            "      - set ticket.phase_applicable=true only when the request makes sense right now from KB + guest context\n"
            "      - set ticket.approval_required=true when KB says approval or manual review is needed\n"
            "      - set ticket.evidence to 1-3 short fact lines copied or paraphrased from full_knowledge_base / policy text\n"
            "      - if details are missing: action=collect_info and pending_action=collect_generic_kb_request\n"
            "      - if all details are present but you still need the guest to approve submission: action=collect_info and pending_action=confirm_generic_kb_request\n"
            "      - if the guest already confirmed and it is ready: action=create_ticket, ticket.required=true, ticket.ready_to_create=true, pending_action=null\n"
            "      - if KB is unclear, silent, or contradictory: do NOT guess, do NOT collect transactional details, do NOT create a ticket\n"
            "\n"
            "C) STAFF-ACTION REQUEST — guest needs something that requires a hotel staff member to physically do something, "
            "but it is NOT a booking or an order and no specific service is configured for it.\n"
            "   Think carefully about WHETHER the request is even possible right now before treating it as staff-actionable.\n"
            "   - Missing items: 'no towels', 'need extra pillows', 'no toiletries', 'minibar not stocked'\n"
            "   - Maintenance: 'AC not working', 'hot water issue', 'lights flickering', 'TV remote broken'\n"
            "   - Housekeeping: 'room not cleaned', 'need fresh sheets', 'trash not picked up'\n"
            "   - Disturbances: 'noisy neighbors', 'construction noise', 'someone knocking on my door'\n"
            "   - Any situation where the guest is stuck and needs staff to act\n"
            "   HOW TO HANDLE:\n"
            "   -> during_stay: This is the guest's home right now. Take ownership. "
            "Empathize naturally ('I'm sorry about that' / 'Let me get that sorted for you right away'), "
            "then set action=create_ticket with ticket.category='request', ticket.sub_category='in_room_assistance', "
            "and ticket.priority based on urgency (no hot water or AC = high, extra pillow = medium). "
            "Tell the guest someone from the team will take care of it shortly. "
            "Do NOT tell them to 'contact housekeeping' or 'call the front desk' — YOU are the front desk.\n"
            "   -> pre_booking / pre_checkin: These requests are NOT actionable here yet. Do NOT create a ticket. "
            "Do NOT offer callback or human handoff unless the guest explicitly asks for a person. "
            "Respond with a short decline that this chat helps with hotel information and room booking, and that in-stay requests like this are handled directly by the property team once the guest has checked in.\n"
            "   -> post_checkout: Only relevant if they left something behind or have a billing issue. "
            "Otherwise respond warmly.\n"
            "   IMPORTANT — do NOT create tickets for:\n"
            "   - Questions: 'do you have extra pillows?' (just answer yes/no from KB)\n"
            "   - Future requests: 'can I get extra towels when I check in?' (just note it)\n"
            "   - Things the guest can do themselves: 'how do I connect to WiFi?' (just answer)\n"
            "   - General feedback without urgency: 'the breakfast was okay' (just acknowledge)\n"
            "   The test: would a real front desk person write this down and send someone to the room? "
            "If yes → create ticket. If they would just answer verbally → respond_only.\n"
            "\n"
            "D) COMPLAINT — guest explicitly expresses dissatisfaction, frustration, or reports something that went wrong.\n"
            "   -> This is different from C: in C the guest just needs something done. "
            "In D the guest is unhappy — there is negative emotion or something has failed.\n"
            "   -> Examples: 'this is unacceptable', 'I've been waiting 30 minutes', 'the food was cold', "
            "'cockroach in my room', 'worst experience', 'I want to speak to a manager'\n"
            "   -> PHASE-AWARE COMPLAINT GATE: Before creating a complaint ticket, ask: does this complaint make sense given where the guest is right now?\n"
            "      - If the guest has NOT yet checked in (pre-booking or pre-checkin phase) and reports an in-room issue (cockroach, broken AC, dirty room, etc.) → the complaint is impossible in context. Respond with empathy but do NOT create a ticket, do NOT offer callback, and do NOT open a support form unless the guest explicitly asks to speak to staff.\n"
            "      - If the guest is during their stay (during_stay) → in-room and on-property complaints ARE valid. Create a ticket.\n"
            "      - If the guest has checked out (post-checkout) → only post-stay complaints (billing, lost items, quality feedback) are valid.\n"
            "      - Apply common sense: a complaint must be physically possible given the guest's current journey stage.\n"
            "   -> Set action=create_ticket, ticket.category='complaint', and set priority higher than normal (high or critical if the guest sounds upset).\n"
            "   -> Empathize FIRST, then tell them you've noted it and the team will address it. Never be defensive.\n"
            "   -> NEVER use complaint routing for: room inquiries, booking requests, food orders, or any request to do something.\n"
            "\n"
            "E) HUMAN HANDOFF — set requires_human_handoff=true ONLY when the guest explicitly asks to talk to a person / staff / team for a real hotel matter.\n"
            "   -> Do NOT set requires_human_handoff just because the guest sounds upset.\n"
            "   -> Do NOT set requires_human_handoff for pre-arrival in-room complaints or in-stay requests unless the guest explicitly asks for staff.\n"
            "\n"
            "=== ROUTING SANITY CHECK ===\n"
            "Before writing output, ask yourself: if a guest said this to me at the front desk, what would I do?\n"
            "  - Would I just answer them? → respond_only\n"
            "  - Would I route them to a specific department to book/order? → dispatch_handler\n"
            "  - Would I write it down and send someone to help? → create_ticket (category=request)\n"
            "  - Would I escalate because the guest is upset? → create_ticket (category=complaint)\n"
            "When in doubt between respond_only and create_ticket, ask: does this need a human to physically do something? If yes, create the ticket.\n"
            "\n"
            "=== GROUNDING RULE ===\n"
            "Use only provided policy + knowledge data. Never invent prices, timings, availability, or capabilities.\n"
            "If the guest asks about weather and memory_facts.live_weather is present, "
            "state the city the reading is for and the reading itself. "
            "The injected object's `source` field tells you whether the reading is for the "
            "city the guest explicitly named (`asked_city`), the property they are currently "
            "focused on (`active_property`), or the hotel's main city (`hotel_default`). "
            "If the guest named a city not present in the injected payload, say you do not "
            "have a live reading for that location and offer the active property's reading "
            "instead. Never invent temperatures or conditions.\n"
            "If the guest asks about weather and memory_facts.live_weather is ABSENT, "
            "reply with ONE short, friendly bullet saying you don't have a live weather "
            "reading right now and suggest a weather app or quick web check. "
            "NEVER offer to have staff/team reach out for weather — weather is NOT in the "
            "hotel's KB and is NOT a hotel deliverable. NEVER ask 'Would you like our team to reach out' "
            "for any weather question, in any context.\n"
            "\n"
            "=== TONE & LANGUAGE ===\n"
            "You are speaking to a hotel guest. Use warm, natural, human language.\n"
            "NEVER use: 'escalate', 'ticket', 'reference number', 'ticket ID', 'logged', 'automated ticketing', 'system issue'.\n"
            "INSTEAD use: 'I have noted this', 'our team will look into this', 'let me connect you with our team'.\n"
            "For complaints: empathize first, then offer help. Never jump to data collection after a complaint.\n"
            "For requests to speak with staff: offer contact info or say someone will reach out — do not use internal process language.\n"
            "\n"
            "=== RESPONSE FORMAT ===\n"
            "When action=respond_only, response_text must be easy to scan and natural to read:\n"
            "- No long paragraphs.\n"
            "- Simple asks may be answered in 1-3 short lines.\n"
            "- If structure helps, use a short lead-in plus a few compact points or mini sections.\n"
            "- Do NOT force '-' bullets on every answer.\n"
            "- Use bold selectively for the main nouns or answer terms.\n"
            "- Use at most 1-2 relevant emojis when they improve readability naturally.\n"
            "- Keep the answer selective; do not dump every field when a concise answer is enough.\n"
            "\n"
            "=== OUTPUT ===\n"
            "Return strict JSON only."
        )
        # Inject booking context + phase rules into orchestrator prompt
        _orch_booking_block = self._build_booking_context_prompt_block(context)
        if _orch_booking_block:
            system_prompt = system_prompt + "\n" + _orch_booking_block

        # ── ROUTER-DECISIVE SHORT-CIRCUITS ───────────────────────────────────
        # When the main router has confidently decided book or service, skip the
        # heavy orchestrator entirely. This collapses 3+ LLM hops into one and
        # prevents the continuity_router from overriding the router's call into
        # the wrong service (the in-room-dining-→-room_booking failure mode).
        _has_pending_action = bool(str(context.pending_action or "").strip())
        _book_context_text = str(book_route_context.get("context_text") or "").strip() if isinstance(book_route_context, dict) else ""

        # Compact service lists for the book_answerer.
        # services_available_now = current-phase services only (what guest can do now).
        # services_unavailable_now = other-phase services with an availability_hint
        # so the bot can politely explain when those become bookable instead of
        # stonewalling guests who ask for them.
        _services_available_now_compact = [
            {
                "id": svc.get("id"),
                "name": svc.get("name"),
                "description": svc.get("description"),
                "ticketing_enabled": svc.get("ticketing_enabled"),
            }
            for svc in (allowed_services_detailed or [])
            if isinstance(svc, dict)
        ]
        _services_unavailable_now_compact = [
            {
                "id": svc.get("id"),
                "name": svc.get("name"),
                "description": svc.get("description"),
                "availability_hint": svc.get("availability_hint"),
            }
            for svc in (out_of_phase_services_detailed or [])
            if isinstance(svc, dict)
        ]
        _last_assistant_message_for_resume = str(history_bundle.get("last_assistant_message") or "")
        _last_service_id_for_resume = self._normalize_identifier(
            (context.pending_data or {}).get("_last_service_id")
            if isinstance(context.pending_data, dict)
            else ""
        )
        if (
            main_router_route == "auto"
            and not _has_pending_action
            and _last_service_id_for_resume == "room_booking"
            and self._is_generic_affirmation(user_message)
            and self._assistant_message_invites_room_booking(_last_assistant_message_for_resume)
        ):
            main_router_route = "service"
            if not isinstance(main_router_hint, dict):
                main_router_hint = {}
            main_router_hint["route"] = "service"
            main_router_hint["service_id"] = _last_service_id_for_resume
            main_router_hint["confidence"] = max(
                float(main_router_hint.get("confidence") or 0.0),
                0.82,
            )
            main_router_hint["contextual_room_booking_resume"] = True

        main_router_route, main_router_hint = await self._maybe_override_main_route_with_service_continuity(
            user_message=user_message,
            context=context,
            history_bundle=history_bundle,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            services_snapshot=services_snapshot,
            main_router_route=main_router_route,
            main_router_hint=main_router_hint if isinstance(main_router_hint, dict) else {},
        )

        if (
            main_router_route == "book"
            and _book_context_text
            and not _has_pending_action
        ):
            answer_text, inline_chips, focus_ids, kb_books = await self._run_book_answerer_llm(
                user_message=user_message,
                book_context_text=_book_context_text,
                history_bundle=history_bundle,
                faq_candidates=faq_candidates,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                context=context,
                memory_snapshot=memory_snapshot,
                in_phase_services=_services_available_now_compact,
                out_of_phase_services=_services_unavailable_now_compact,
                book_route_context=book_route_context,
                router_selected_property_ids=main_router_property_ids,
            )
            if answer_text:
                self._persist_book_answerer_focus(
                    context=context,
                    focus_ids=focus_ids,
                    selected_phase_id=selected_phase_id,
                )
                # TOPIC EXIT: the guest's turn was answered by the book answerer,
                # which (per the ROOM & OFFER OWNERSHIP rule) only happens for
                # non-room/offer topics — venues, dining, amenities, contact, etc.
                # That means the guest has left whatever service was last active.
                # Drop the stale service lead so a later bare "book it" / "yes" /
                # "give me contact details" cannot be force-resumed back into that
                # old service (e.g. room booking) by the continuity / service-router
                # / contextual-resume paths, all of which key off `_last_service_id`.
                # A genuine room continuation never reaches here — room/offer turns
                # resolve to route=service, which re-sets the lead instead.
                if isinstance(context.pending_data, dict):
                    _dropped_lead = self._normalize_identifier(
                        context.pending_data.get("_last_service_id")
                    )
                    if _dropped_lead:
                        context.pending_data.pop("_last_service_id", None)
                        context.pending_data.pop("_last_service_name", None)
                book_decision = OrchestrationDecision(
                    action="respond_only",
                    intent="faq",
                    confidence=float(main_router_hint.get("confidence") or 0.85),
                    response_text=answer_text,
                    target_service_id="",
                    suggested_actions=list(inline_chips or []),
                    selected_property_ids=list(focus_ids or []),
                    metadata={
                        "source": "book_answerer",
                        "kb_books": list(kb_books or []),
                        "orchestration_trace_id": orchestration_trace_id,
                        "main_router_route": "book",
                        "main_router_book_ids": list(main_router_book_ids)[:20],
                        "router_book_ids": list(_router_chosen_book_ids)[:20],
                        "book_route_status": book_route_status,
                        "suggested_actions_source": "book_answerer_inline" if inline_chips else "fallback",
                        "focus_property_ids": list(focus_ids or []),
                    },
                )
                book_decision = self._normalize_faq_resolution_metadata(
                    book_decision, faq_candidates=faq_candidates
                )
                book_decision = await self._ensure_suggested_actions(
                    user_message=user_message,
                    context=context,
                    decision=book_decision,
                    selected_phase_id=selected_phase_id,
                    selected_phase_name=selected_phase_name,
                    target_service=None,
                    prefetched_services=prefetched_services,
                )
                # GUARANTEE (book fast-path): the answerer must never PROMISE a
                # handoff ("let me get someone from our team...") without OPENING
                # the form. This lightweight path builds a respond_only decision
                # and RETURNS before the orchestrator's escalation guard (~12233),
                # so we enforce the same contract here: if the answer is the
                # handoff-acceptance line and a support-intake service exists in
                # this phase, flip the decision to open the intake form. Keys on
                # the bot's OWN output line (a contract it must keep) — not on any
                # user-keyword list.
                _book_ans_lc = str(book_decision.response_text or "").lower()
                _book_support_sid = next(
                    (sid for sid in (allowed_service_ids or [])
                     if any(tok in str(sid).lower() for tok in (
                         "support", "handoff", "contact_staff", "callback",
                         "helpdesk", "raise_request"))),
                    "",
                )
                if (
                    ("let me get someone from our team" in _book_ans_lc
                     or "get someone from our team to help" in _book_ans_lc)
                    and _book_support_sid
                ):
                    book_decision.action = "collect_info"
                    book_decision.target_service_id = _book_support_sid
                    book_decision.pending_action = "collect_form_details"
                    book_decision.missing_fields = []
                    book_decision.suggested_actions = []
                    book_decision.response_text = (
                        "Please review the details below and submit so our team can assist."
                    )
                    try:
                        book_decision.ticket.required = True
                        book_decision.ticket.ready_to_create = False
                    except Exception:
                        pass
                    book_decision.metadata["support_form_forced_from_escalation"] = True
                    book_decision.metadata["support_form_force_trigger"] = "handoff_promise_line_book_path"
                _log_decision({
                    "ts": datetime.now(UTC).isoformat(),
                    "trace_id": orchestration_trace_id,
                    "session_id": str(getattr(context, "session_id", "")),
                    "phase": {"id": selected_phase_id, "name": selected_phase_name},
                    "user_message": user_message,
                    "routing": {
                        "orchestrator_action": "skipped_book_route",
                        "orchestrator_service_id": "",
                        "main_router_route": "book",
                        "main_router_service_id": "",
                        "main_router_book_ids": list(main_router_book_ids)[:12],
                        "clarification_protected": False,
                        "final_action": "respond_only",
                        "final_service_id": "",
                        "prompt_source": "book_answerer",
                        "service_resolution_source": "main_router_book",
                    },
                    "response_text": str((book_decision.response_text or "")[:500]),
                    "intent": str(book_decision.intent or ""),
                    "missing_fields": [],
                    "context_switched": False,
                    "answer_first_guard_skipped": True,
                    "pending_action_at_start": str(context.pending_action or ""),
                })

                # ── Chip speculation: pre-generate answers for the chips we
                # just shipped, so the next turn is instant if the guest clicks
                # one. Fire-and-forget — the live request returns immediately;
                # speculation runs in the background while the guest reads.
                # Cost: up to N extra LLM calls per turn (one per chip), which
                # the user has explicitly opted into for the click-latency win.
                # Gated on the same setting the chips parallel runner uses, so
                # chip generation can be turned off without dead background
                # tasks. Only runs when speculative chips ship — typed messages
                # take the normal path.
                try:
                    _spec_session_id = str(getattr(context, "session_id", "") or "")
                    _spec_chips = [
                        str(chip).strip()
                        for chip in (book_decision.suggested_actions or [])
                        if str(chip or "").strip()
                    ][: self._chip_speculation_max_per_session]
                    if (
                        _spec_session_id
                        and _spec_chips
                        and self._legacy_chip_speculation_enabled()
                        and isinstance(book_route_context, dict)
                        and str(_book_context_text or "").strip()
                    ):
                        _spec_args = {
                            "session_id": _spec_session_id,
                            "user_message_just_asked": user_message,
                            "answer_just_given": str(book_decision.response_text or ""),
                            "book_context_text": _book_context_text,
                            "history_bundle": history_bundle,
                            "faq_candidates": faq_candidates,
                            "selected_phase_id": selected_phase_id,
                            "selected_phase_name": selected_phase_name,
                            "context": context,
                            "memory_snapshot": memory_snapshot,
                            "in_phase_services": _services_available_now_compact,
                            "out_of_phase_services": _services_unavailable_now_compact,
                            "book_route_context": book_route_context,
                            "main_router_hint": main_router_hint,
                            "main_router_book_ids": main_router_book_ids,
                            "book_route_status": book_route_status,
                            "orchestration_trace_id": orchestration_trace_id,
                        }
                        for _chip in _spec_chips:
                            self._schedule_chip_speculation(
                                chip_text=_chip,
                                **_spec_args,
                            )
                except Exception:
                    pass

                self.schedule_post_response_chip_job(
                    session_id=str(getattr(context, "session_id", "") or ""),
                    assistant_turn_id=assistant_turn_id,
                    user_message=user_message,
                    context=context,
                    decision=book_decision,
                    selected_phase_id=selected_phase_id,
                    selected_phase_name=selected_phase_name,
                    target_service=None,
                    prefetched_services=prefetched_services,
                    history_bundle=history_bundle,
                    faq_candidates=faq_candidates,
                    memory_snapshot=memory_snapshot,
                    in_phase_services=_services_available_now_compact,
                    out_of_phase_services=_services_unavailable_now_compact,
                    book_route_context=book_route_context,
                    main_router_hint=main_router_hint,
                    main_router_book_ids=main_router_book_ids,
                    book_route_status=book_route_status,
                    orchestration_trace_id=orchestration_trace_id,
                )
                return book_decision

        if (
            main_router_route == "service"
            and not _has_pending_action
        ):
            _router_service_id = self._normalize_identifier(main_router_hint.get("service_id"))
            _allowed_set = {self._normalize_identifier(v) for v in allowed_service_ids if self._normalize_identifier(v)}
            if _router_service_id and _router_service_id in _allowed_set:
                _target_svc = next(
                    (svc for svc in services_snapshot
                     if isinstance(svc, dict)
                     and self._normalize_identifier(svc.get("id")) == _router_service_id),
                    None,
                )
                if isinstance(_target_svc, dict):
                    _current_active_ids = self._normalize_property_id_list(
                        list((context.pending_data or {}).get("_active_property_ids") or [])
                    )
                    _current_active_names = [
                        str(name).strip()
                        for name in list((context.pending_data or {}).get("_active_property_names") or [])
                        if str(name).strip()
                    ]
                    _service_focus_ids = list(main_router_property_ids or _current_active_ids)
                    # NOT ONE OF OURS (hotels set up in the Pre-booking tab). The router
                    # keeps the hotel in focus for every support or complaint turn, and
                    # chose none only when the guest named somewhere else - "refund for
                    # my booking in kerala hotel" with T24 Retro in focus. Falling back
                    # to the focus filed that request as a T24 Retro ticket (15 Oct);
                    # leave it unchosen so the property gate asks which hotel.
                    _router_chose_none = False
                    try:
                        from services import prebooking_settings_service as _pbs_none

                        _svc_blob_none = f"{_target_svc.get('id') or ''} {_target_svc.get('name') or ''}".lower()
                        _router_chose_none = bool(
                            not main_router_property_ids
                            and _current_active_ids
                            and any(
                                tok in _svc_blob_none
                                for tok in ("support", "handoff", "contact_staff", "callback",
                                            "helpdesk", "raise_request", "complaint")
                            )
                            and _pbs_none.is_admin_managed(str(getattr(context, "hotel_code", "") or "").strip().lower())
                        )
                    except Exception:
                        _router_chose_none = False
                    if _router_chose_none:
                        _service_focus_ids = []
                    _service_focus_name = ""
                    if len(_service_focus_ids) == 1:
                        _service_focus_name = self._resolve_property_name_from_pending(
                            pending_data=context.pending_data,
                            property_id=_service_focus_ids[0],
                            fallback=(
                                _current_active_names[0]
                                if len(_service_focus_ids) == len(_current_active_ids) == 1 and _current_active_names
                                else ""
                            ),
                        )
                    base_decision = OrchestrationDecision(
                        action="dispatch_handler",
                        intent="service_request",
                        confidence=float(main_router_hint.get("confidence") or 0.85),
                        target_service_id=_router_service_id,
                        response_text="",
                        selected_property_name=_service_focus_name,
                        selected_property_ids=list(_service_focus_ids),
                        metadata={
                            "source": "main_router_service_short_circuit",
                            "orchestration_trace_id": orchestration_trace_id,
                            "main_router_route": "service",
                            "service_resolution_source": "main_router",
                        },
                    )
                    self._suspend_active_service_task(
                        context=context,
                        services_snapshot=services_snapshot,
                        next_service_id=_router_service_id,
                    )
                    service_decision = await self._run_service_agent(
                        user_message=user_message,
                        context=context,
                        memory_snapshot=memory_snapshot,
                        service=_target_svc,
                        base_decision=base_decision,
                        selected_phase_id=selected_phase_id,
                        selected_phase_name=selected_phase_name,
                    )
                    if not isinstance(context.pending_data, dict):
                        context.pending_data = {}
                    context.pending_data["_last_service_id"] = _router_service_id
                    context.pending_data["_last_service_name"] = str(_target_svc.get("name") or _router_service_id)
                    merged_decision = self._merge_decisions(base_decision, service_decision)
                    # A HOTEL THE GUEST DID NOT CHOOSE STAYS UNCHOSEN (hotels set up in
                    # the Pre-booking tab, T24 onward). With no hotel named, the router
                    # keeps every hotel in scope. Asked "i want to book room", the
                    # room-booking agent wrote about both T24 hotels and still returned
                    # only the first one's id; that id became the hotel in focus, a
                    # booking ticket was raised for T24 Residency and the reply became
                    # "Great choice! ... T24 Residency" (14 Oct). An agent may not narrow
                    # what the router left open on this turn; its answer still goes out.
                    # Zone, Mango and Sarovar unchanged.
                    try:
                        from services import prebooking_settings_service as _pbs_focus

                        _agent_focus_ids = self._normalize_property_id_list(
                            list(merged_decision.selected_property_ids or [])
                        )
                        if (
                            len(_service_focus_ids) >= 2
                            and _agent_focus_ids
                            and len(_agent_focus_ids) < len(_service_focus_ids)
                            and set(_agent_focus_ids) <= set(_service_focus_ids)
                            and _pbs_focus.is_admin_managed(
                                str(getattr(context, "hotel_code", "") or "").strip().lower()
                            )
                        ):
                            logger.info(
                                "[SCOPE] %s chose %s while the router kept %s - no hotel was "
                                "chosen, keeping the router's", _router_service_id,
                                _agent_focus_ids, _service_focus_ids,
                            )
                            merged_decision.selected_property_ids = list(_service_focus_ids)
                            merged_decision.selected_property_name = ""
                        if _router_chose_none and (
                            merged_decision.selected_property_ids or str(merged_decision.selected_property_name or "").strip()
                        ):
                            logger.info(
                                "[SCOPE] %s chose %s but the router chose no hotel (not one of ours) - "
                                "dropping it", _router_service_id, _agent_focus_ids,
                            )
                            merged_decision.selected_property_ids = []
                            merged_decision.selected_property_name = ""
                    except Exception as _focus_exc:
                        logger.debug("agent focus check skipped: %r", _focus_exc)
                    _merged_focus_ids = self._normalize_property_id_list(
                        list(merged_decision.selected_property_ids or [])
                    )
                    if _merged_focus_ids:
                        if set(_merged_focus_ids) != set(_current_active_ids):
                            _cleared_keys = self._clear_pending_state_for_property_switch(
                                context=context,
                                services_snapshot=services_snapshot,
                                current_service_id=_router_service_id,
                            )
                            if _cleared_keys:
                                merged_decision.metadata.setdefault(
                                    "llm_property_switch_cleared_pending_keys",
                                    list(_cleared_keys),
                                )
                        self._persist_book_answerer_focus(
                            context=context,
                            focus_ids=_merged_focus_ids,
                            selected_phase_id=selected_phase_id,
                            selected_property_name=str(
                                merged_decision.selected_property_name or ""
                            ).strip(),
                        )
                    merged_decision = self._normalize_generic_kb_request_decision(merged_decision)
                    merged_decision = await self._enforce_support_property_choice(
                        decision=merged_decision, target_service=_target_svc, context=context,
                    )
                    merged_decision.metadata.setdefault("orchestration_trace_id", orchestration_trace_id)
                    merged_decision.metadata.setdefault("source", "main_router_service_short_circuit")
                    ticket_creation_ready = bool(
                        isinstance(merged_decision.ticket, TicketDecision)
                        and bool(merged_decision.ticket.ready_to_create)
                    )
                    if (
                        not ticket_creation_ready
                        and str(merged_decision.action or "").strip().lower()
                        not in {"cancel_pending", "resume_pending"}
                    ):
                        merged_decision = await self._enforce_answer_first_policy(
                            user_message=user_message,
                            decision=merged_decision,
                            context=context,
                            selected_phase_id=selected_phase_id,
                            selected_phase_name=selected_phase_name,
                            target_service=_target_svc,
                            full_kb_text=full_kb_text,
                        )
                        merged_decision = self._normalize_generic_kb_request_decision(merged_decision)
                    merged_decision = await self._ensure_suggested_actions(
                        user_message=user_message,
                        context=context,
                        decision=merged_decision,
                        selected_phase_id=selected_phase_id,
                        selected_phase_name=selected_phase_name,
                        target_service=_target_svc,
                        prefetched_services=prefetched_services,
                    )
                    _log_decision({
                        "ts": datetime.now(UTC).isoformat(),
                        "trace_id": orchestration_trace_id,
                        "session_id": str(getattr(context, "session_id", "")),
                        "phase": {"id": selected_phase_id, "name": selected_phase_name},
                        "user_message": user_message,
                        "routing": {
                            "orchestrator_action": "skipped_service_route",
                            "orchestrator_service_id": _router_service_id,
                            "main_router_route": "service",
                            "main_router_service_id": _router_service_id,
                            "main_router_book_ids": [],
                            "clarification_protected": False,
                            "final_action": str(merged_decision.action or ""),
                            "final_service_id": _router_service_id,
                            "prompt_source": "service_agent",
                            "service_resolution_source": "main_router",
                        },
                        "response_text": str((merged_decision.response_text or "")[:500]),
                        "intent": str(merged_decision.intent or ""),
                        "missing_fields": list(merged_decision.missing_fields or []),
                        "context_switched": bool((merged_decision.metadata or {}).get("context_switched")),
                        "answer_first_guard_skipped": False,
                        "pending_action_at_start": str(context.pending_action or ""),
                    })

                    # Universal chip speculation: kick off pre-gen for the
                    # chips this service-route turn shipped, so a click on the
                    # next turn is also instant. We don't have a book_route_
                    # context built here (service routes skip it), so the
                    # universal kickoff will build one on demand from active
                    # properties. Fire-and-forget — tracked so the next turn
                    # cancels it if the guest moves on.
                    try:
                        _svc_session_id = str(getattr(context, "session_id", "") or "")
                        _svc_spec_task = asyncio.create_task(
                            self._speculate_for_decision_universal(
                                decision=merged_decision,
                                user_message=user_message,
                                history_bundle=history_bundle,
                                faq_candidates=faq_candidates,
                                memory_snapshot=memory_snapshot,
                                selected_phase_id=selected_phase_id,
                                selected_phase_name=selected_phase_name,
                                context=context,
                                in_phase_services=_services_available_now_compact,
                                out_of_phase_services=_services_unavailable_now_compact,
                                book_route_context=None,
                                main_router_hint=main_router_hint,
                                main_router_book_ids=[],
                                book_route_status="service_route_speculation",
                                orchestration_trace_id=orchestration_trace_id,
                            )
                        )
                        self._track_speculation_task(_svc_session_id, _svc_spec_task)
                    except Exception:
                        pass

                    self.schedule_post_response_chip_job(
                        session_id=str(getattr(context, "session_id", "") or ""),
                        assistant_turn_id=assistant_turn_id,
                        user_message=user_message,
                        context=context,
                        decision=merged_decision,
                        selected_phase_id=selected_phase_id,
                        selected_phase_name=selected_phase_name,
                        target_service=_target_svc,
                        prefetched_services=prefetched_services,
                        history_bundle=history_bundle,
                        faq_candidates=faq_candidates,
                        memory_snapshot=memory_snapshot,
                        in_phase_services=_services_available_now_compact,
                        out_of_phase_services=_services_unavailable_now_compact,
                        book_route_context=None,
                        main_router_hint=main_router_hint,
                        main_router_book_ids=[],
                        book_route_status="service_route_speculation",
                        orchestration_trace_id=orchestration_trace_id,
                    )
                    return merged_decision

        # When the router asked for clarification, do NOT spin up the heavy
        # orchestrator just to re-derive an answer. The router already produced
        # the clarification text — return it as the response.
        if main_router_route == "clarify" and not _has_pending_action:
            clarify_text = str(main_router_hint.get("clarification_question") or "").strip()
            if clarify_text:
                clarify_decision = OrchestrationDecision(
                    action="respond_only",
                    intent="clarification",
                    confidence=float(main_router_hint.get("confidence") or 0.6),
                    response_text=clarify_text,
                    target_service_id="",
                    metadata={
                        "source": "main_router_clarify_short_circuit",
                        "orchestration_trace_id": orchestration_trace_id,
                        "main_router_route": "clarify",
                    },
                )
                clarify_decision = await self._ensure_suggested_actions(
                    user_message=user_message,
                    context=context,
                    decision=clarify_decision,
                    selected_phase_id=selected_phase_id,
                    selected_phase_name=selected_phase_name,
                    target_service=None,
                    prefetched_services=prefetched_services,
                )
                _log_decision({
                    "ts": datetime.now(UTC).isoformat(),
                    "trace_id": orchestration_trace_id,
                    "session_id": str(getattr(context, "session_id", "")),
                    "phase": {"id": selected_phase_id, "name": selected_phase_name},
                    "user_message": user_message,
                    "routing": {
                        "orchestrator_action": "skipped_clarify_route",
                        "orchestrator_service_id": "",
                        "main_router_route": "clarify",
                        "main_router_service_id": "",
                        "main_router_book_ids": [],
                        "clarification_protected": True,
                        "final_action": "respond_only",
                        "final_service_id": "",
                        "prompt_source": "main_router_clarification",
                        "service_resolution_source": "main_router",
                    },
                    "response_text": str((clarify_decision.response_text or "")[:500]),
                    "intent": str(clarify_decision.intent or ""),
                    "missing_fields": [],
                    "context_switched": False,
                    "answer_first_guard_skipped": True,
                    "pending_action_at_start": str(context.pending_action or ""),
                })
                return clarify_decision

        # When the router said `auto` (no decisive intent), still prefer the
        # lightweight book_answerer over the heavy orchestrator. Hand it ALL
        # context the orchestrator would have seen: book content if any books
        # were loaded, otherwise the full property KB. The LLM decides what to
        # answer with — we don't pre-filter.
        if main_router_route == "auto" and not _has_pending_action:
            _kb_context_for_auto = _book_context_text or str(full_kb_text or "").strip()
            if _kb_context_for_auto:
                auto_answer, auto_inline_chips, auto_focus_ids, auto_kb_books = await self._run_book_answerer_llm(
                    user_message=user_message,
                    book_context_text=_kb_context_for_auto,
                    history_bundle=history_bundle,
                    faq_candidates=faq_candidates,
                    selected_phase_id=selected_phase_id,
                    selected_phase_name=selected_phase_name,
                    context=context,
                    memory_snapshot=memory_snapshot,
                    in_phase_services=_services_available_now_compact,
                    out_of_phase_services=_services_unavailable_now_compact,
                    book_route_context=book_route_context if isinstance(book_route_context, dict) else None,
                    router_selected_property_ids=main_router_property_ids,
                )
                if auto_answer:
                    self._persist_book_answerer_focus(
                        context=context,
                        focus_ids=auto_focus_ids,
                        selected_phase_id=selected_phase_id,
                    )
                    auto_decision = OrchestrationDecision(
                        action="respond_only",
                        intent="faq",
                        confidence=float(main_router_hint.get("confidence") or 0.7),
                        response_text=auto_answer,
                        target_service_id="",
                        suggested_actions=list(auto_inline_chips or []),
                        selected_property_ids=list(auto_focus_ids or []),
                        metadata={
                            "source": "book_answerer_auto_fallback",
                            "kb_books": list(auto_kb_books or []),
                            "orchestration_trace_id": orchestration_trace_id,
                            "main_router_route": "auto",
                            "suggested_actions_source": "book_answerer_inline" if auto_inline_chips else "fallback",
                            "focus_property_ids": list(auto_focus_ids or []),
                        },
                    )
                    auto_decision = self._normalize_faq_resolution_metadata(
                        auto_decision, faq_candidates=faq_candidates
                    )
                    auto_decision = await self._ensure_suggested_actions(
                        user_message=user_message,
                        context=context,
                        decision=auto_decision,
                        selected_phase_id=selected_phase_id,
                        selected_phase_name=selected_phase_name,
                        target_service=None,
                        prefetched_services=prefetched_services,
                    )
                    _log_decision({
                        "ts": datetime.now(UTC).isoformat(),
                        "trace_id": orchestration_trace_id,
                        "session_id": str(getattr(context, "session_id", "")),
                        "phase": {"id": selected_phase_id, "name": selected_phase_name},
                        "user_message": user_message,
                        "routing": {
                            "orchestrator_action": "skipped_auto_route",
                            "orchestrator_service_id": "",
                            "main_router_route": "auto",
                            "main_router_service_id": "",
                            "main_router_book_ids": [],
                            "clarification_protected": False,
                            "final_action": "respond_only",
                            "final_service_id": "",
                            "prompt_source": "book_answerer_auto",
                            "service_resolution_source": "main_router",
                        },
                        "response_text": str((auto_decision.response_text or "")[:500]),
                        "intent": str(auto_decision.intent or ""),
                        "missing_fields": [],
                        "context_switched": False,
                        "answer_first_guard_skipped": True,
                        "pending_action_at_start": str(context.pending_action or ""),
                    })
                    self.schedule_post_response_chip_job(
                        session_id=str(getattr(context, "session_id", "") or ""),
                        assistant_turn_id=assistant_turn_id,
                        user_message=user_message,
                        context=context,
                        decision=auto_decision,
                        selected_phase_id=selected_phase_id,
                        selected_phase_name=selected_phase_name,
                        target_service=None,
                        prefetched_services=prefetched_services,
                        history_bundle=history_bundle,
                        faq_candidates=faq_candidates,
                        memory_snapshot=memory_snapshot,
                        in_phase_services=_services_available_now_compact,
                        out_of_phase_services=_services_unavailable_now_compact,
                        book_route_context=book_route_context if isinstance(book_route_context, dict) else None,
                        main_router_hint=main_router_hint,
                        main_router_book_ids=main_router_book_ids,
                        book_route_status=book_route_status,
                        orchestration_trace_id=orchestration_trace_id,
                    )
                    return auto_decision
        # ── END SHORT-CIRCUITS ───────────────────────────────────────────────

        model = str(getattr(settings, "llm_orchestration_model", "") or "").strip() or None
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            {"role": "system", "content": self._reply_language_directive()},
        ]
        _log_llm_call(
            label="ORCHESTRATOR",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt=system_prompt,
            payload=payload,
        )
        raw = await self._chat_with_json(
            messages=messages,
            model=model,
            temperature=0.0,
            max_tokens=4096,
            trace_context={
                "responder_type": "main",
                "agent": "orchestrator",
                "session_id": str(getattr(context, "session_id", "")),
                "selected_phase_id": selected_phase_id,
                "selected_phase_name": selected_phase_name,
                "trace_id": orchestration_trace_id,
            },
        )
        _log_llm_call(
            label="ORCHESTRATOR RESPONSE",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt="",
            payload={},
            response=raw,
        )
        decision = self._safe_parse_decision(raw, user_message=user_message)
        decision.metadata.setdefault("source", "orchestrator")
        decision.metadata.setdefault("orchestration_trace_id", orchestration_trace_id)
        decision = self._apply_answer_priority_fields(decision)
        decision = self._normalize_faq_resolution_metadata(
            decision,
            faq_candidates=faq_candidates,
        )
        decision = self._normalize_generic_kb_request_decision(decision)
        decision = self._apply_main_router_route_hint(
            decision=decision,
            route_hint=main_router_hint,
            allowed_service_ids=allowed_service_ids,
        )

        # ── A handoff must OPEN THE FORM — never a verbal dead-end. ──────────
        decision = self._normalize_prearrival_instay_request_decision(
            decision=decision,
            user_message=user_message,
            selected_phase_id=selected_phase_id,
        )

        # The book/answer path can promise a handoff in prose ("let me get
        # someone from our team...") OR a responder can set requires_human_handoff
        # — both of which otherwise render as a bare "our team will reach out"
        # message with NO form, silently dropping the guest's request (the
        # reported dead-end). We close that gap with two CONTEXT-driven signals
        # (no hardcoded user-keyword list):
        #   (1) requires_human_handoff — the responder's own context-aware signal
        #       that a human is needed.
        #   (2) the bot's OWN acceptance-promise line — a contract it must keep.
        #       (Only the acceptance phrasing, never the "would you like..." OFFER,
        #       so this can't open a form before the guest has agreed.)
        # We only fire when a support-intake service exists in this phase, and
        # never override a decision that is already opening a form.
        _resp_lc = str(decision.response_text or "").lower()
        _handoff_promise_line = (
            "let me get someone from our team" in _resp_lc
            or "get someone from our team to help" in _resp_lc
        )
        _already_opening_form = (
            str(decision.action or "").strip().lower()
            in {"collect_info", "create_ticket", "dispatch_handler"}
            and bool(self._normalize_identifier(decision.target_service_id))
        )
        _support_sid = next(
            (sid for sid in (allowed_service_ids or [])
             if any(tok in str(sid).lower() for tok in (
                 "support", "handoff", "contact_staff", "callback",
                 "helpdesk", "raise_request"))),
            "",
        )
        _force_from_flag = bool(decision.requires_human_handoff)
        if (_force_from_flag or _handoff_promise_line) and not _already_opening_form and _support_sid:
            decision.metadata["support_form_force_trigger"] = (
                "requires_human_handoff" if _force_from_flag else "handoff_promise_line"
            )
            decision.requires_human_handoff = False
            decision.action = "collect_info"
            decision.target_service_id = _support_sid
            decision.pending_action = "collect_form_details"
            decision.missing_fields = []
            decision.ticket.required = True
            decision.ticket.ready_to_create = False
            decision.suggested_actions = []
            decision.response_text = (
                "Please review the details below and submit so our team can assist."
            )
            decision.metadata["support_form_forced_from_escalation"] = True

        # ── Persist LLM-determined property scope into conversation state ──
        # The orchestrator returns selected_property_name / selected_property_ids.
        # We track active properties (full KB sent) and past properties (brief
        # summary retained) so the LLM always has the right context.
        _llm_prop_name = str(decision.selected_property_name or "").strip()
        _llm_prop_ids = self._normalize_property_id_list(
            [str(pid).strip() for pid in (decision.selected_property_ids or []) if str(pid).strip()]
        )
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}

        if _llm_prop_ids:
            _llm_prop_ids, _post_llm_pin_applied = self._apply_stay_property_pin(
                context=context,
                selected_phase_id=selected_phase_id,
                property_ids=_llm_prop_ids,
                source="post_orchestrator_decision",
            )
            if _post_llm_pin_applied:
                decision.metadata["stay_property_pin_reapplied"] = True

        _prev_active_ids = self._normalize_property_id_list(
            list(context.pending_data.get("_active_property_ids") or [])
        )
        _prev_active_names = list(context.pending_data.get("_active_property_names") or [])
        _past_ctx = dict(context.pending_data.get("_past_property_context") or {})

        # A fleet-attribute / broaden turn ("which hotel is pet friendly") makes
        # nearly the whole catalog active for ONE turn. When the next turn narrows,
        # do NOT dump that entire fleet set into "previously discussed" — the guest
        # engaged with none of them individually. Only a genuine narrow focus/compare
        # (a handful of hotels) rolls into past context. 6+ = a broaden, not interest.
        _prev_was_fleet_scope = len(_prev_active_ids) >= 6
        if _llm_prop_ids and not _prev_was_fleet_scope:
            # Move previously-active properties that are no longer active to past context
            for _old_id in _prev_active_ids:
                if _old_id and _old_id not in _llm_prop_ids and _old_id not in _past_ctx:
                    # Build a brief summary from the old property name
                    _old_idx = _prev_active_ids.index(_old_id) if _old_id in _prev_active_ids else -1
                    _fallback_old_name = (
                        _prev_active_names[_old_idx] if 0 <= _old_idx < len(_prev_active_names) else _old_id
                    )
                    _old_name = self._resolve_property_name_from_pending(
                        pending_data=context.pending_data,
                        property_id=_old_id,
                        fallback=_fallback_old_name,
                    )
                    _past_ctx[_old_id] = f"Previously discussed: {_old_name}"
            # Cap past context to 10 entries max
            if len(_past_ctx) > 10:
                _past_ctx = dict(list(_past_ctx.items())[-10:])

            self._sync_property_scope_pending(
                pending_data=context.pending_data,
                property_ids=_llm_prop_ids,
                active_property_names=list(context.pending_data.get("_active_property_names") or []),
                selected_property_name=_llm_prop_name,
            )
            context.pending_data["_past_property_context"] = _past_ctx
        elif _llm_prop_name and not _llm_prop_ids:
            # LLM gave a name but no IDs — keep legacy behavior
            context.pending_data["_selected_property_scope_name"] = _llm_prop_name
            context.pending_data["selected_property_scope_name"] = _llm_prop_name

        # Expose the active property scope on decision.metadata so the API layer
        # (HTTP + WebSocket) can gate booking-button display on the property
        # count (button only when exactly ONE property is in focus). decision
        # metadata flows through to the response metadata.
        try:
            _active_for_meta = list(
                _llm_prop_ids
                or (context.pending_data.get("_active_property_ids") if isinstance(context.pending_data, dict) else [])
                or []
            )
            decision.metadata["active_property_ids"] = [str(x).strip() for x in _active_for_meta if str(x).strip()]
            _names_for_meta = (
                list(context.pending_data.get("_active_property_names") or [])
                if isinstance(context.pending_data, dict) else []
            )
            if _names_for_meta:
                decision.metadata["active_property_names"] = [str(x).strip() for x in _names_for_meta if str(x).strip()]
        except Exception:
            pass

        if decision.action in {"resume_pending", "cancel_pending"}:
            return await self._ensure_suggested_actions(
                user_message=user_message,
                context=context,
                decision=decision,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
                prefetched_services=prefetched_services,
            )

        decision.target_service_id = self._normalize_identifier(
            decision.target_service_id
            or (decision.metadata or {}).get("target_service_id")
            or (decision.metadata or {}).get("service_id")
            or ""
        )
        decision = self._normalize_generic_kb_request_decision(decision)
        generic_kb_request = self._is_generic_kb_request(decision)

        # Continuity router: use LLM to decide whether this short/ambiguous turn
        # should continue the last active service flow.
        # Gate: only run when main_router did NOT make a decisive book/service call.
        # If main_router said route=book or route=service, that decision is authoritative —
        # overriding it with last-service-continuity routes informational asks (e.g.
        # "tell me more about in-room dining") into the wrong service agent.
        _last_assistant_message = str(history_bundle.get("last_assistant_message") or "")
        _last_service_id = self._normalize_identifier(
            (context.pending_data or {}).get("_last_service_id")
            if isinstance(context.pending_data, dict)
            else ""
        )
        if (
            str(decision.action or "").strip().lower() == "respond_only"
            and not decision.target_service_id
            and _last_service_id
            and str(user_message or "").strip()
            and str(_last_assistant_message or "").strip()
            and main_router_route == "auto"
        ):
            continue_last_service, continuation_reason = await self._resolve_last_service_continuation_with_llm(
                user_message=user_message,
                last_assistant_message=_last_assistant_message,
                last_service_id=_last_service_id,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                services_snapshot=services_snapshot,
                context=context,
            )
            if continue_last_service:
                decision.action = "dispatch_handler"
                decision.target_service_id = _last_service_id
                if not isinstance(decision.metadata, dict):
                    decision.metadata = {}
                decision.metadata["service_resolution_source"] = "continuation_router_llm"
                if continuation_reason:
                    decision.metadata["continuation_router_reason"] = continuation_reason

        # Only call the fallback service router when the orchestrator did NOT return
        # a clarification question. If action=respond_only with non-empty response_text
        # and no target_service_id, it means the orchestrator intentionally chose to ask
        # the user a question (STEP 2B/2C). Calling the service router here would bypass
        # that clarification and force-dispatch to a service.
        _orchestrator_gave_clarification = (
            str(decision.action or "").strip().lower() == "respond_only"
            and str(decision.response_text or "").strip()
            and not decision.target_service_id
        )
        _skip_answer_first = False  # default; overridden later if service runs
        if (
            not decision.target_service_id
            and not _orchestrator_gave_clarification
            and not generic_kb_request
            and main_router_route != "book"
        ):
            resolved_service_id, action_hint = await self._resolve_target_service_with_llm(
                user_message=user_message,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                services_snapshot=services_snapshot,
                context=context,
            )
            _log_llm_call(
                label="SERVICE ROUTER",
                session_id=str(getattr(context, "session_id", "")),
                user_message=user_message,
                system_prompt="(see _resolve_target_service_with_llm)",
                payload={"resolved_service_id": resolved_service_id, "action_hint": action_hint},
            )
            if resolved_service_id:
                decision.target_service_id = resolved_service_id
                decision.metadata.setdefault("service_resolution_source", "service_router_llm")
                if (
                    str(decision.action or "").strip().lower() == "respond_only"
                    and action_hint in {"collect_info", "dispatch_handler", "create_ticket"}
                ):
                    decision.action = action_hint

        service_agent_enabled = bool(getattr(settings, "chat_llm_service_agent_enabled", True))
        target_service_id = self._normalize_identifier(decision.target_service_id)
        target_service = None
        for service in services_snapshot:
            if self._normalize_identifier(service.get("id")) == target_service_id:
                target_service = service
                break

        # Park unfinished active task before dispatching to a different service.
        self._suspend_active_service_task(
            context=context,
            services_snapshot=services_snapshot,
            next_service_id=target_service_id,
        )
        # ─────────────────────────────────────────────────────────────────────
        # TOPIC EXIT — clear any stale service lead when NO service is dispatched.
        # Reaching here with an empty target_service_id means the guest's turn was
        # answered as information / a clarification, not as a service action — so the
        # guest has moved off whatever service was last active. Drop `_last_service_id`
        # so the NEXT context-dependent message (a bare "book it", "yes", "book now",
        # "give me contact details") is resolved against the CURRENT topic instead of
        # being force-resumed into the old service by the orchestrator / continuity /
        # service-router / contextual-resume paths, which all key off this field.
        # A genuine service turn sets target_service_id (re-writing the lead) and
        # never reaches this branch, so real in-flow continuations are unaffected.
        if not target_service_id and isinstance(context.pending_data, dict):
            if self._normalize_identifier(context.pending_data.get("_last_service_id")):
                context.pending_data.pop("_last_service_id", None)
                context.pending_data.pop("_last_service_name", None)
        if not service_agent_enabled or not target_service_id:
            fallback_decision = await self._enforce_answer_first_policy(
                user_message=user_message,
                decision=decision,
                context=context,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
                full_kb_text=full_kb_text,
            )
            fallback_decision = self._normalize_generic_kb_request_decision(fallback_decision)
            _early_result = await self._ensure_suggested_actions(
                user_message=user_message,
                context=context,
                decision=fallback_decision,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=target_service if isinstance(target_service, dict) else None,
                prefetched_services=prefetched_services,
            )
            _log_decision({
                "ts": datetime.now(UTC).isoformat(),
                "trace_id": orchestration_trace_id,
                "session_id": str(getattr(context, "session_id", "")),
                "phase": {"id": selected_phase_id, "name": selected_phase_name},
                "user_message": user_message,
                "routing": {
                    "orchestrator_action": str(decision.action or ""),
                    "orchestrator_service_id": str(decision.target_service_id or ""),
                    "main_router_route": main_router_route,
                    "main_router_service_id": str(main_router_hint.get("service_id") or ""),
                    "main_router_book_ids": list(main_router_book_ids)[:12],
                    "clarification_protected": _orchestrator_gave_clarification,
                    "final_action": str((_early_result.action or "") if _early_result else ""),
                    "final_service_id": "",
                    "prompt_source": "n/a",
                    "service_resolution_source": "none",
                    "early_return_reason": "no_service_agent_enabled_or_id",
                },
                "response_text": str((_early_result.response_text or "")[:500]) if _early_result else "",
                "intent": str((_early_result.intent or "")) if _early_result else "",
                "missing_fields": [],
                "context_switched": False,
                "answer_first_guard_skipped": False,
                "pending_action_at_start": str(context.pending_action or ""),
            })
            self.schedule_post_response_chip_job(
                session_id=str(getattr(context, "session_id", "") or ""),
                assistant_turn_id=assistant_turn_id,
                user_message=user_message,
                context=context,
                decision=_early_result,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=target_service if isinstance(target_service, dict) else None,
                prefetched_services=prefetched_services,
                history_bundle=history_bundle,
                faq_candidates=faq_candidates,
                memory_snapshot=memory_snapshot,
                in_phase_services=_services_available_now_compact,
                out_of_phase_services=_services_unavailable_now_compact,
                book_route_context=book_route_context if isinstance(book_route_context, dict) else None,
                main_router_hint=main_router_hint,
                main_router_book_ids=main_router_book_ids,
                book_route_status=book_route_status,
                orchestration_trace_id=orchestration_trace_id,
            )
            return _early_result

        if not isinstance(target_service, dict):
            fallback_decision = await self._enforce_answer_first_policy(
                user_message=user_message,
                decision=decision,
                context=context,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
                full_kb_text=full_kb_text,
            )
            _early_result = await self._ensure_suggested_actions(
                user_message=user_message,
                context=context,
                decision=fallback_decision,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
                prefetched_services=prefetched_services,
            )
            _log_decision({
                "ts": datetime.now(UTC).isoformat(),
                "trace_id": orchestration_trace_id,
                "session_id": str(getattr(context, "session_id", "")),
                "phase": {"id": selected_phase_id, "name": selected_phase_name},
                "user_message": user_message,
                "routing": {
                    "orchestrator_action": str(decision.action or ""),
                    "orchestrator_service_id": str(decision.target_service_id or ""),
                    "main_router_route": main_router_route,
                    "main_router_service_id": str(main_router_hint.get("service_id") or ""),
                    "main_router_book_ids": list(main_router_book_ids)[:12],
                    "clarification_protected": _orchestrator_gave_clarification,
                    "final_action": str((_early_result.action or "") if _early_result else ""),
                    "final_service_id": "",
                    "prompt_source": "n/a",
                    "service_resolution_source": "none",
                    "early_return_reason": "target_service_not_dict",
                },
                "response_text": str((_early_result.response_text or "")[:500]) if _early_result else "",
                "intent": str((_early_result.intent or "")) if _early_result else "",
                "missing_fields": [],
                "context_switched": False,
                "answer_first_guard_skipped": False,
                "pending_action_at_start": str(context.pending_action or ""),
            })
            self.schedule_post_response_chip_job(
                session_id=str(getattr(context, "session_id", "") or ""),
                assistant_turn_id=assistant_turn_id,
                user_message=user_message,
                context=context,
                decision=_early_result,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
                prefetched_services=prefetched_services,
                history_bundle=history_bundle,
                faq_candidates=faq_candidates,
                memory_snapshot=memory_snapshot,
                in_phase_services=_services_available_now_compact,
                out_of_phase_services=_services_unavailable_now_compact,
                book_route_context=None,
                main_router_hint=main_router_hint,
                main_router_book_ids=main_router_book_ids,
                book_route_status=book_route_status,
                orchestration_trace_id=orchestration_trace_id,
            )
            return _early_result

        service_decision = await self._run_service_agent(
            user_message=user_message,
            context=context,
            memory_snapshot=memory_snapshot,
            service=target_service,
            base_decision=decision,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
        )
        service_decision.metadata.setdefault("orchestration_trace_id", orchestration_trace_id)
        # Track which service was last active so the next turn's orchestrator has context.
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        context.pending_data["_last_service_id"] = target_service_id
        context.pending_data["_last_service_name"] = str(target_service.get("name") or target_service_id)
        if bool((service_decision.metadata or {}).get("context_switched")):
            # Preserve unfinished work from the current service before rerouting.
            suspended = self._suspend_active_service_task(
                context=context,
                services_snapshot=services_snapshot,
                next_service_id="",
                force=True,
            )
            if not suspended:
                # Even with no suspendable data, clear active service pin so main can re-route.
                internal = {
                    key: value
                    for key, value in (context.pending_data or {}).items()
                    if isinstance(key, str) and key.startswith("_")
                }
                context.pending_data = internal
                context.pending_action = None
            if _service_reroute_depth < 1:
                rerouted_decision = await self.orchestrate_turn(
                    user_message=user_message,
                    context=context,
                    capabilities_summary=capabilities_summary,
                    memory_snapshot=memory_snapshot,
                    selected_phase_context=selected_phase_context,
                    prefetched_services=prefetched_services,
                    assistant_turn_id=assistant_turn_id,
                    _service_reroute_depth=_service_reroute_depth + 1,
                )
                if isinstance(rerouted_decision, OrchestrationDecision):
                    rerouted_decision.metadata.setdefault(
                        "context_switch_rerouted_from_service",
                        target_service_id,
                    )
                    return rerouted_decision

        merged_decision = self._merge_decisions(decision, service_decision)
        _current_active_ids_for_service = self._normalize_property_id_list(
            list((context.pending_data or {}).get("_active_property_ids") or [])
        )
        _merged_focus_ids = self._normalize_property_id_list(
            list(merged_decision.selected_property_ids or [])
        )
        if _merged_focus_ids:
            if set(_merged_focus_ids) != set(_current_active_ids_for_service):
                _cleared_keys = self._clear_pending_state_for_property_switch(
                    context=context,
                    services_snapshot=services_snapshot,
                    current_service_id=target_service_id,
                )
                if _cleared_keys:
                    merged_decision.metadata.setdefault(
                        "llm_property_switch_cleared_pending_keys",
                        list(_cleared_keys),
                    )
            self._persist_book_answerer_focus(
                context=context,
                focus_ids=_merged_focus_ids,
                selected_phase_id=selected_phase_id,
                selected_property_name=str(
                    merged_decision.selected_property_name or ""
                ).strip(),
            )
        merged_decision = self._normalize_generic_kb_request_decision(merged_decision)
        merged_decision = await self._enforce_support_property_choice(
            decision=merged_decision, target_service=target_service, context=context,
        )
        # Skip answer-first enforcement for first-turn intros (it overwrites the room list),
        # for confirmation overrides (already deterministically correct),
        # and for ready-to-create ticket confirmations.
        ticket_creation_ready = bool(
            str(merged_decision.action or "").strip().lower() == "create_ticket"
            and bool(merged_decision.ticket.required)
            and bool(merged_decision.ticket.ready_to_create)
        )
        _md_action = str(merged_decision.action or "").strip().lower()
        _md_meta = merged_decision.metadata if isinstance(merged_decision.metadata, dict) else {}
        _md_response = str(merged_decision.response_text or "").strip()
        _md_answered = bool(merged_decision.answered_current_query)
        _md_blocking = list(merged_decision.blocking_fields or [])
        _guard_knowledge_text, _ = self._build_answer_first_knowledge_base(
            user_message=user_message,
            context=context,
            target_service=target_service,
            full_kb_text=full_kb_text,
        )
        _response_has_kb_content = bool(_guard_knowledge_text and _md_response)
        _is_mid_flow_collection = (
            _md_action == "collect_info"
            and not _md_meta.get("is_first_service_turn")
            and bool(_md_blocking)
        )
        _is_kb_factual_answer = (
            _md_action == "respond_only"
            and _response_has_kb_content
            and _md_answered
        )
        _is_ambiguous_dispatch = (
            _md_action in {"dispatch_handler", "respond_only"}
            and not _md_answered
        )
        _guard_needed = _is_mid_flow_collection or _is_kb_factual_answer or _is_ambiguous_dispatch
        _skip_answer_first = bool(
            not _guard_needed
            or _md_meta.get("confirmation_override")
            or ticket_creation_ready
            or _md_action in {"cancel_pending", "resume_pending"}
            or main_router_route == "clarify"
        )
        if _skip_answer_first and not ticket_creation_ready:
            _skip_reason = (
                "smart_skip_no_validation_target" if not _guard_needed
                else "smart_skip_clarify_or_lifecycle"
            )
            merged_decision.metadata.setdefault("answer_first_guard_skipped_reason", _skip_reason)
        if ticket_creation_ready:
            merged_decision.metadata.setdefault("answer_first_guard_skipped_reason", "ticket_creation_ready")
        if not _skip_answer_first:
            merged_decision = await self._enforce_answer_first_policy(
                user_message=user_message,
                decision=merged_decision,
                context=context,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=target_service,
                full_kb_text=full_kb_text,
            )
            merged_decision = self._normalize_generic_kb_request_decision(merged_decision)

        # ── RESUME PROMPT ────────────────────────────────────────────────────
        # After the current turn settles with no active pending, ask user ONCE
        # if they want to resume a suspended service.
        if (
            context.suspended_services
            and not context.resume_prompt_sent
            and not str(merged_decision.pending_action or "").strip()
        ):
            susp = context.suspended_services[0]
            susp_name = susp.get("service_name", "a previous request")
            merged_decision.response_text = (
                str(merged_decision.response_text or "").rstrip()
                + f"\n\nAlso, you were in the middle of **{susp_name}** — would you like to continue where you left off?"
            )
            context.resume_prompt_sent = True
        # ─────────────────────────────────────────────────────────────────────

        final = await self._ensure_suggested_actions(
            user_message=user_message,
            context=context,
            decision=merged_decision,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            target_service=target_service,
            prefetched_services=prefetched_services,
        )
        final = self._normalize_generic_kb_request_decision(final)
        _log_decision({
            "ts": datetime.now(UTC).isoformat(),
            "trace_id": orchestration_trace_id,
            "session_id": str(getattr(context, "session_id", "")),
            "phase": {"id": selected_phase_id, "name": selected_phase_name},
            "user_message": user_message,
            "routing": {
                "orchestrator_action": str(decision.action or ""),
                "orchestrator_service_id": str(decision.target_service_id or ""),
                "main_router_route": main_router_route,
                "main_router_service_id": str(main_router_hint.get("service_id") or ""),
                "main_router_book_ids": list(main_router_book_ids)[:12],
                "clarification_protected": _orchestrator_gave_clarification,
                "final_action": str(final.action or "") if final else "",
                "final_service_id": str(final.target_service_id or "") if final else "",
                "prompt_source": ("generated" if target_service and str(target_service.get("generated_system_prompt") or "").strip() else "template") if target_service else "n/a",
                "service_resolution_source": str((merged_decision.metadata or {}).get("service_resolution_source") or "orchestrator"),
            },
            "response_text": str(final.response_text or "")[:500] if final else "",
            "intent": str(final.intent or "") if final else "",
            "missing_fields": list(final.missing_fields or []) if final else [],
            "ticket": {
                "required": bool(getattr((final or decision).ticket, "required", False)) if (final or decision) else False,
                "ready_to_create": bool(getattr((final or decision).ticket, "ready_to_create", False)) if (final or decision) else False,
                "generic_request": bool(getattr((final or decision).ticket, "generic_request", False)) if (final or decision) else False,
                "evidence": list(getattr((final or decision).ticket, "evidence", []) or [])[:3] if (final or decision) else [],
            },
            "context_switched": bool((merged_decision.metadata or {}).get("context_switched")),
            "answer_first_guard_skipped": _skip_answer_first,
            "pending_action_at_start": str(context.pending_action or ""),
        })
        self.schedule_post_response_chip_job(
            session_id=str(getattr(context, "session_id", "") or ""),
            assistant_turn_id=assistant_turn_id,
            user_message=user_message,
            context=context,
            decision=final,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            target_service=target_service if isinstance(target_service, dict) else None,
            prefetched_services=prefetched_services,
            history_bundle=history_bundle,
            faq_candidates=faq_candidates,
            memory_snapshot=memory_snapshot,
            in_phase_services=_services_available_now_compact,
            out_of_phase_services=_services_unavailable_now_compact,
            book_route_context=book_route_context if isinstance(book_route_context, dict) else None,
            main_router_hint=main_router_hint,
            main_router_book_ids=main_router_book_ids,
            book_route_status=book_route_status,
            orchestration_trace_id=orchestration_trace_id,
        )
        return final


# A few extracted-mixin staticmethods call sibling staticmethods by class name.
# Expose LLMOrchestrationService in those modules now that the class exists — it
# can't be a module-level import there (that would be a circular import).
from services import (
    llm_orchestration_chips_mixin as _lom_chips,
    llm_orchestration_grounding_mixin as _lom_grounding,
    llm_orchestration_directives_mixin as _lom_directives,
)
for _lom in (_lom_chips, _lom_grounding, _lom_directives):
    _lom.LLMOrchestrationService = LLMOrchestrationService


llm_orchestration_service = LLMOrchestrationService()
