"""LLMOrchestrationChipsMixin: methods extracted from llm_orchestration_service.py (behavior-preserving).

Moved verbatim (39 methods). LLMOrchestrationService inherits this mixin, so ``self.*``
resolution is unchanged. This module never imports llm_orchestration_service — no cycle.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any, Optional
from config.settings import settings
from llm.client import llm_client
from schemas.chat import ConversationContext
from schemas.orchestration import OrchestrationDecision, SuggestionChip
from services.config_service import config_service
from services import conversation_cache_service
from services.conversation_memory_service import conversation_memory_service
from services.faq_context_service import build_faq_block
from services.prompt_registry_service import (
    PromptMissingError,
    prompt_registry,
)
from services.llm_call_logging import _log_llm_call

logger = logging.getLogger("services.llm_orchestration_service")




class LLMOrchestrationChipsMixin:
    # ── WHAT THIS GUEST HAS ALREADY USED, FOR THE WHOLE CHAT ────────────────
    # A chip the guest TAPPED is spent: that topic has been asked and answered,
    # and offering it again is the repeat that reads as broken. A chip that was
    # merely SHOWN and ignored is not spent - the guest never used it, and
    # blocking it for the rest of the chat only empties the chip row. (Measured:
    # blocking merely-shown chips killed 3 of 3 good suggestions on one turn,
    # because all three had appeared once, unclicked, earlier.)
    # So this remembers what was USED, for the whole conversation. What was only
    # shown stays governed by the short window it always had, which is enough to
    # stop the same row appearing twice in a row.
    _chips_shown_by_conversation: dict[str, list[str]] = {}
    _CHIPS_SHOWN_PER_CONVERSATION = 60   # oldest fall off first
    _CHIPS_SHOWN_CONVERSATIONS = 400     # bounded, oldest conversation evicted

    @classmethod
    def _chips_shown_key(cls, session_id: Any, conversation_uid: Any) -> str:
        """One key both writer and reader can build from what they each hold."""
        return f"{str(session_id or '').strip()}::{str(conversation_uid or '').strip()}"

    @classmethod
    def remember_chips_shown(cls, session_id: Any, conversation_uid: Any, texts: Any) -> None:
        """Record chips the guest actually USED (tapped). Not the ones merely shown."""
        try:
            key = cls._chips_shown_key(session_id, conversation_uid)
            if key == "::":
                return
            store = cls._chips_shown_by_conversation
            seen = store.setdefault(key, [])
            lowered = {t.strip().lower() for t in seen}
            for raw in (texts or []):
                text = str(raw or "").strip()
                if text and text.lower() not in lowered:
                    lowered.add(text.lower())
                    seen.append(text)
            if len(seen) > cls._CHIPS_SHOWN_PER_CONVERSATION:
                del seen[: len(seen) - cls._CHIPS_SHOWN_PER_CONVERSATION]
            if len(store) > cls._CHIPS_SHOWN_CONVERSATIONS:
                for old_key in list(store)[: len(store) - cls._CHIPS_SHOWN_CONVERSATIONS]:
                    store.pop(old_key, None)
        except Exception:
            pass  # never let bookkeeping break a turn

    @classmethod
    def chips_shown_in_conversation(cls, session_id: Any, conversation_uid: Any) -> list[str]:
        try:
            return list(cls._chips_shown_by_conversation.get(
                cls._chips_shown_key(session_id, conversation_uid), []
            ))
        except Exception:
            return []

    @staticmethod
    def suggestion_chips_disabled_for_hotel(hotel_code: Any) -> bool:
        """True when this tenant has suggestion chips switched off entirely.

        Opt-OUT list from settings.chat_suggestion_chips_disabled_hotels_json —
        a hotel_code that is not listed behaves exactly as before. Sarovar is
        listed because the chip job kept emitting "Yes, please"/"No, thanks"
        after replies that asked no question, and the router then read the
        resulting bare "yes" as acceptance of a staff handoff that was never
        offered (spurious pre-arrival tickets).
        """
        code = str(hotel_code or "").strip().lower()
        if not code:
            return False
        from services import prebooking_settings_service

        admin = prebooking_settings_service.settings_for(code)
        if admin is not None:
            return not bool(admin.get("chips_enabled", True))
        raw = str(
            getattr(settings, "chat_suggestion_chips_disabled_hotels_json", "") or ""
        ).strip()
        if not raw:
            return False
        try:
            parsed = json.loads(raw)
        except Exception:
            logger.warning(
                "chat_suggestion_chips_disabled_hotels_json is not valid JSON; ignoring"
            )
            return False
        if not isinstance(parsed, list):
            return False
        return code in {str(item or "").strip().lower() for item in parsed}

    @classmethod
    def _cancel_post_response_chip_job(cls, session_id: str) -> int:
        """Cancel the current post-response chip job for a session."""
        if not session_id:
            return 0
        job = cls._post_response_chip_jobs.pop(session_id, None)
        if not isinstance(job, dict):
            return 0
        task = job.get("task")
        try:
            if task is not None and not task.done():
                task.cancel()
                return 1
        except Exception:
            pass
        return 0

    @staticmethod
    def _normalize_chip_text(text: Any) -> str:
        """Normalize a chip / user message for cache matching.
        Lowercase, strip leading/trailing whitespace, collapse internal runs of
        whitespace, drop trailing punctuation. Designed so the chip we shipped
        ('What time does the spa open?') matches the click that comes back
        ('What time does the spa open?') case-insensitively, while still
        rejecting unrelated typed messages."""
        s = str(text or "").strip().lower()
        if not s:
            return ""
        s = re.sub(r"\s+", " ", s)
        s = s.rstrip("?.!,:; ")
        return s

    def _consume_chip_speculation(
        self,
        *,
        session_id: str,
        user_message: str,
    ) -> Optional[dict[str, Any]]:
        """Return the cached speculation entry for a clicked chip.

        The entry may already hold a completed `decision`, or it may still
        reference an in-flight background task. The caller decides whether to
        await that task or fall back to the normal live routing path. The
        session cache is always cleared as a side effect so stale chips from an
        older turn never bleed into a fresh one."""
        if not session_id:
            return None
        cache = self._chip_speculation_cache.pop(session_id, None)
        if not cache:
            return None
        norm = self._normalize_chip_text(user_message)
        if not norm:
            return None
        now = time.time()
        for entry in cache:
            if not isinstance(entry, dict):
                continue
            try:
                age = now - float(entry.get("ts") or 0.0)
            except (TypeError, ValueError):
                age = self._chip_speculation_ttl_seconds + 1
            if age > self._chip_speculation_ttl_seconds:
                continue
            if entry.get("chip_norm") == norm:
                return entry
        return None

    async def _speculate_chip_answer(
        self,
        *,
        chip_text: str,
        user_message_just_asked: str,
        answer_just_given: str,
        book_context_text: str,
        history_bundle: dict[str, Any],
        faq_candidates: list[dict[str, Any]],
        selected_phase_id: str,
        selected_phase_name: str,
        context: Any,
        memory_snapshot: dict[str, Any],
        in_phase_services: list[dict[str, Any]],
        out_of_phase_services: list[dict[str, Any]],
        book_route_context: dict[str, Any],
        main_router_hint: dict[str, Any],
        main_router_book_ids: list[str],
        book_route_status: str,
        orchestration_trace_id: str,
    ) -> Optional[OrchestrationDecision]:
        """Pre-generate the answer for a single chip in the background.

        Runs after we've already shipped the current turn's reply, while the
        guest is reading. If they click the chip on the next turn, the cached
        decision is served instantly with zero LLM wait. If they type something
        else, the cache is discarded — we burn 1 background LLM call per chip
        as the cost of instant clicks. Failures are silent: speculation must
        never affect the live request path."""
        try:
            extended = dict(history_bundle or {})
            prior = list(extended.get("history_last_10") or [])
            if user_message_just_asked:
                prior.append({"role": "user", "content": str(user_message_just_asked)})
            if answer_just_given:
                prior.append({"role": "assistant", "content": str(answer_just_given)})
            extended["history_last_10"] = prior[-10:]
            extended["last_user_message"] = str(user_message_just_asked or "")
            extended["last_assistant_message"] = str(answer_just_given or "")

            answer_text, inline_chips, focus_ids = await self._run_book_answerer_llm(
                user_message=chip_text,
                book_context_text=book_context_text,
                history_bundle=extended,
                faq_candidates=faq_candidates,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                context=context,
                memory_snapshot=memory_snapshot,
                in_phase_services=in_phase_services,
                out_of_phase_services=out_of_phase_services,
                book_route_context=book_route_context,
                wait_for_chips=True,
            )
            if not answer_text:
                return None
            decision = OrchestrationDecision(
                action="respond_only",
                intent="faq",
                confidence=float(main_router_hint.get("confidence") or 0.85),
                response_text=answer_text,
                target_service_id="",
                suggested_actions=list(inline_chips or []),
                selected_property_ids=list(focus_ids or []),
                metadata={
                    "source": "book_answerer_speculation",
                    "orchestration_trace_id": orchestration_trace_id,
                    "main_router_route": "book",
                    "main_router_book_ids": list(main_router_book_ids)[:20],
                    "book_route_status": book_route_status,
                    "suggested_actions_source": "book_answerer_inline" if inline_chips else "fallback",
                    "focus_property_ids": list(focus_ids or []),
                    "speculated_for_chip": str(chip_text),
                },
            )
            decision = self._normalize_faq_resolution_metadata(
                decision, faq_candidates=faq_candidates
            )
            # Snapshot the args required to speculate ONE MORE LEVEL DEEP — i.e.
            # for the chips that came back inside this cached decision. When
            # the guest clicks one of those chips on the next turn, the
            # cache-hit path uses these args to launch its own speculation,
            # producing the tree-of-clicks effect (every click is instant,
            # not just the first one). The history_bundle stored here is
            # ALREADY extended with (user_message_just_asked, answer_just_given);
            # the next-level speculation will extend it again with
            # (chip_text → answer_text) before running.
            return decision
        except Exception:
            return None

    def _schedule_chip_speculation(
        self,
        *,
        session_id: str,
        chip_text: str,
        user_message_just_asked: str,
        answer_just_given: str,
        book_context_text: str,
        history_bundle: dict[str, Any],
        faq_candidates: list[dict[str, Any]],
        selected_phase_id: str,
        selected_phase_name: str,
        context: Any,
        memory_snapshot: dict[str, Any],
        in_phase_services: list[dict[str, Any]],
        out_of_phase_services: list[dict[str, Any]],
        book_route_context: dict[str, Any],
        main_router_hint: dict[str, Any],
        main_router_book_ids: list[str],
        book_route_status: str,
        orchestration_trace_id: str,
    ) -> None:
        if not session_id or not self._legacy_chip_speculation_enabled():
            return
        chip_norm = self._normalize_chip_text(chip_text)
        if not chip_norm:
            return

        cache = self._chip_speculation_cache.setdefault(session_id, [])
        for existing in cache:
            if not isinstance(existing, dict):
                continue
            if existing.get("chip_norm") == chip_norm:
                return

        spec_args = {
            "book_context_text": book_context_text,
            "history_bundle": history_bundle or {},
            "faq_candidates": faq_candidates or [],
            "selected_phase_id": selected_phase_id,
            "selected_phase_name": selected_phase_name,
            "context": context,
            "memory_snapshot": memory_snapshot or {},
            "in_phase_services": in_phase_services or [],
            "out_of_phase_services": out_of_phase_services or [],
            "book_route_context": book_route_context or {},
            "main_router_hint": main_router_hint or {},
            "main_router_book_ids": list(main_router_book_ids or []),
            "book_route_status": book_route_status,
            "orchestration_trace_id": orchestration_trace_id,
        }
        entry: dict[str, Any] = {
            "chip_norm": chip_norm,
            "chip_text": str(chip_text or "").strip(),
            "decision": None,
            "task": None,
            "ts": time.time(),
            "spec_args": spec_args,
            "user_message_just_asked": str(user_message_just_asked or "").strip(),
            "answer_just_given": str(answer_just_given or "").strip(),
        }
        cache.append(entry)
        if len(cache) > self._chip_speculation_max_per_session:
            del cache[: len(cache) - self._chip_speculation_max_per_session]

        async def _runner() -> Optional[OrchestrationDecision]:
            decision = await self._speculate_chip_answer(
                chip_text=chip_text,
                user_message_just_asked=user_message_just_asked,
                answer_just_given=answer_just_given,
                **spec_args,
            )
            if decision is not None:
                entry["decision"] = decision
                entry["user_message_just_asked"] = str(chip_text or "").strip()
                entry["answer_just_given"] = str(getattr(decision, "response_text", "") or "").strip()
            return decision

        task = asyncio.create_task(_runner())
        entry["task"] = task
        self._track_speculation_task(session_id, task)

    def _build_post_response_chip_runtime_snapshot(
        self,
        *,
        context: ConversationContext,
        decision: OrchestrationDecision,
        selected_phase_id: str,
        selected_phase_name: str,
        resolved_target_service: dict[str, Any] | None,
        memory_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending_data_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
        pending_data_public = self._coerce_public_pending(pending_data_raw)
        phase_id = self._normalize_phase_identifier(selected_phase_id) or self._normalize_phase_identifier(
            pending_data_public.get("selected_phase_id")
        )
        active_service: dict[str, Any] = {}
        if isinstance(resolved_target_service, dict):
            active_service = {
                "id": str(resolved_target_service.get("id") or "").strip(),
                "name": str(
                    resolved_target_service.get("name")
                    or resolved_target_service.get("service_name")
                    or ""
                ).strip(),
                "type": str(resolved_target_service.get("type") or "service").strip(),
                "description": str(resolved_target_service.get("description") or "").strip(),
            }
        else:
            last_service_id = self._normalize_identifier(
                pending_data_public.get("service_id")
                or pending_data_public.get("last_service_id")
                or ""
            )
            if last_service_id:
                service_row = config_service.get_service(last_service_id) or {}
                active_service = {
                    "id": str(service_row.get("id") or last_service_id).strip(),
                    "name": str(service_row.get("name") or last_service_id).strip(),
                    "type": str(service_row.get("type") or "service").strip(),
                    "description": str(service_row.get("description") or "").strip(),
                }

        snapshot = {
            "conversation_state": str(getattr(getattr(context, "state", None), "value", context.state) or "").strip(),
            "pending_action": str(context.pending_action or "").strip(),
            "pending_data_public": dict(pending_data_public),
            "known_context": {
                "guest_name": str(getattr(context, "guest_name", "") or "").strip(),
                "room_number": str(getattr(context, "room_number", "") or "").strip(),
            },
            "memory_snapshot": dict(memory_snapshot or conversation_memory_service.get_snapshot(context) or {}),
            "phase_id_hint": phase_id,
            "phase_name_hint": str(selected_phase_name or "").strip(),
            "active_service_id": str(active_service.get("id") or "").strip(),
            "active_service": dict(active_service),
            "conversation_history": self._build_recent_history_rows(
                context=context,
                append_assistant_message=str(decision.response_text or "").strip(),
            ),
        }
        return snapshot

    def _normalize_suggested_actions(self, value: Any) -> list[str]:
        return self._chip_texts(self._normalize_suggestion_chips(value))

    @staticmethod
    def _build_chip_evidence_fingerprint(
        *,
        text: str,
        turn_trace_id: str,
        origin_responder: str,
        origin_service_id: str,
        active_property_ids: list[str],
        selected_book_ids: list[str],
        evidence_excerpt: str,
        target_service_id: str = "",
    ) -> str:
        payload = json.dumps(
            {
                "text": " ".join(str(text or "").strip().lower().split()),
                "turn_trace_id": str(turn_trace_id or "").strip(),
                "origin_responder": str(origin_responder or "").strip().lower(),
                "origin_service_id": str(origin_service_id or "").strip().lower(),
                "target_service_id": str(target_service_id or "").strip().lower(),
                "active_property_ids": [str(v).strip().lower() for v in list(active_property_ids or []) if str(v).strip()],
                "selected_book_ids": [str(v).strip().lower() for v in list(selected_book_ids or []) if str(v).strip()],
                "evidence_excerpt": " ".join(str(evidence_excerpt or "").strip().lower().split()),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _normalize_suggestion_chip(
        self,
        value: Any,
        *,
        defaults: dict[str, Any] | None = None,
    ) -> SuggestionChip | None:
        base = dict(defaults or {})
        raw: dict[str, Any]
        if isinstance(value, SuggestionChip):
            raw = value.model_dump(mode="json")
        elif isinstance(value, dict):
            raw = dict(value)
        else:
            text = str(value or "").strip()
            if not text:
                return None
            raw = {"text": text}

        if not str(raw.get("text") or "").strip() and str(raw.get("label") or "").strip():
            raw["text"] = str(raw.get("label") or "").strip()
        if not str(raw.get("evidence_excerpt") or "").strip() and str(raw.get("evidence") or "").strip():
            raw["evidence_excerpt"] = str(raw.get("evidence") or "").strip()
        merged = {**base, **raw}
        try:
            chip = SuggestionChip.model_validate(merged)
        except Exception:
            return None
        if not chip.text:
            return None
        # Display polish: chips must start with a capital letter ("which room…"
        # → "Which room…"). Only the FIRST character is upper-cased; the rest is
        # untouched (proper nouns, etc. stay as written), and scripts without
        # case (Hindi/Devanagari) are unaffected.
        _chip_display_text = chip.text[:72]
        if _chip_display_text:
            _chip_display_text = _chip_display_text[0].upper() + _chip_display_text[1:]
        return SuggestionChip.model_validate(
            {
                **chip.model_dump(mode="json"),
                "text": _chip_display_text,
                "evidence_fingerprint": chip.evidence_fingerprint
                or self._build_chip_evidence_fingerprint(
                    text=chip.text,
                    turn_trace_id=chip.turn_trace_id,
                    origin_responder=chip.origin_responder,
                    origin_service_id=chip.origin_service_id,
                    target_service_id=chip.target_service_id,
                    active_property_ids=list(chip.active_property_ids or []),
                    selected_book_ids=list(chip.selected_book_ids or []),
                    evidence_excerpt=chip.evidence_excerpt,
                ),
            }
        )

    def _normalize_suggestion_chips(
        self,
        value: Any,
        *,
        defaults: dict[str, Any] | None = None,
    ) -> list[SuggestionChip]:
        if not isinstance(value, list):
            return []
        normalized: list[SuggestionChip] = []
        seen: set[str] = set()
        for item in value:
            chip = self._normalize_suggestion_chip(item, defaults=defaults)
            if chip is None:
                continue
            key = chip.text.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(chip)
            if len(normalized) >= self._chip_speculation_max_per_session:
                break
        return normalized

    def _chip_texts(self, chips: list[SuggestionChip]) -> list[str]:
        texts: list[str] = []
        for chip in list(chips or [])[: self._chip_speculation_max_per_session]:
            if not isinstance(chip, SuggestionChip):
                continue
            text = str(chip.text or "").strip()
            if text:
                texts.append(text)
        return texts

    def _legacy_chip_speculation_enabled(self) -> bool:
        return bool(getattr(settings, "chat_chip_speculation_enabled", True)) and not bool(
            getattr(settings, "chat_suggestion_prefetch_enabled", True)
        )

    @staticmethod
    def _dedupe_chip_candidate_texts(candidates: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in list(candidates or []):
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").strip().split())
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            row = dict(item)
            row["text"] = text
            deduped.append(row)
            if len(deduped) >= max(1, int(limit or 3)):
                break
        return deduped

    def _has_clear_context_for_chip_fallback(
        self,
        *,
        runtime_snapshot: dict[str, Any],
        active_property_ids: list[str],
        resolved_target_service: dict[str, Any] | None,
        assistant_response: str = "",
    ) -> bool:
        # Any non-empty bot response is enough context for generic during-stay chips
        if str(assistant_response or "").strip():
            return True
        if list(active_property_ids or []):
            return True
        if isinstance(resolved_target_service, dict) and str(resolved_target_service.get("id") or "").strip():
            return True
        pending_action = str((runtime_snapshot or {}).get("pending_action") or "").strip()
        if pending_action:
            return True
        pending_public = (runtime_snapshot or {}).get("pending_data_public")
        pending_public = pending_public if isinstance(pending_public, dict) else {}
        for key in (
            "selected_property_scope_name",
            "selected_property_scope_id",
            "selected_property_scope_ids",
            "active_property_ids",
            "active_property_names",
        ):
            value = pending_public.get(key)
            if isinstance(value, list) and value:
                return True
            if isinstance(value, dict) and value:
                return True
            if str(value or "").strip():
                return True
        active_service = (runtime_snapshot or {}).get("active_service")
        if isinstance(active_service, dict) and str(active_service.get("id") or "").strip():
            return True
        return False

    def _build_deterministic_chip_fallback_candidates(
        self,
        *,
        runtime_snapshot: dict[str, Any],
        assistant_response: str,
        grounding_text: str,
    ) -> list[dict[str, Any]]:
        pending_public = (runtime_snapshot or {}).get("pending_data_public")
        pending_public = pending_public if isinstance(pending_public, dict) else {}

        property_names: list[str] = []
        seen_names: set[str] = set()
        for raw_name in list(pending_public.get("active_property_names") or []):
            name = " ".join(str(raw_name or "").strip().split())
            if not name:
                continue
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            property_names.append(name)
            if len(property_names) >= 3:
                break

        selected_name = " ".join(str(pending_public.get("selected_property_scope_name") or "").strip().split())
        if selected_name and selected_name.lower() not in seen_names:
            property_names.insert(0, selected_name)

        primary_name = property_names[0] if property_names else ""
        response_text = str(assistant_response or "").strip()
        grounding = str(grounding_text or "").strip()
        combined = "\n".join(part for part in [response_text, grounding] if part).strip()

        candidates: list[dict[str, Any]] = []
        combined_lower = combined.lower()
        has_rooms = ("rooms" in combined_lower) or ("room types" in combined_lower) or ("suites" in combined_lower)
        has_amenities = ("amenities" in combined_lower) or ("facilities" in combined_lower)
        has_spa = ("spa" in combined_lower) or ("wellness" in combined_lower)
        has_dining = ("dining" in combined_lower) or ("restaurant" in combined_lower) or ("cuisine" in combined_lower)
        has_location = ("locat" in combined_lower) or ("airport" in combined_lower) or ("address" in combined_lower)
        has_policies = ("check-in" in combined_lower) or ("check out" in combined_lower) or ("checkout" in combined_lower)
        has_events = ("events" in combined_lower) or ("banquet" in combined_lower) or ("meeting" in combined_lower)

        if primary_name:
            # 6 unconditional templates so the pool always has twice the window
            # size (3), guaranteeing the dedup can always rotate to fresh chips.
            # Use concise templates so chips stay ≤8 words even for multi-word
            # property names (e.g. "Orchid Rishivan Rishikesh" = 3 words →
            # "What rooms does Orchid Rishivan Rishikesh have?" = 7 words ✓).
            candidates.append({"text": f"Tell me about {primary_name}"})
            candidates.append({"text": f"What rooms does {primary_name} have?"})
            candidates.append({"text": f"What amenities does {primary_name} offer?"})
            candidates.append({"text": f"Where is {primary_name} located?"})
            candidates.append({"text": f"Check-in time at {primary_name}?"})
            candidates.append({"text": f"Dining options at {primary_name}?"})
            if has_spa:
                candidates.append({"text": f"Spa services at {primary_name}?"})
            if has_events:
                candidates.append({"text": f"Event venues at {primary_name}?"})
        if len(property_names) > 1:
            candidates.append({"text": f"Tell me about {property_names[1]}"})

        if not primary_name:
            # Always provide generic during-stay chips regardless of content flags
            # so multi-property group responses always get seeded chips.
            candidates.append({"text": "What amenities are available?"})
            candidates.append({"text": "What dining options are there?"})
            candidates.append({"text": "What services can I request?"})
            candidates.append({"text": "What activities are nearby?"})
            if has_rooms:
                candidates.append({"text": "What room types are available?"})
            if has_spa:
                candidates.append({"text": "What wellness amenities are available?"})
            if has_location:
                candidates.append({"text": "Where is this property located?"})

        with_evidence: list[dict[str, Any]] = []
        for item in self._dedupe_chip_candidate_texts(candidates, limit=6):
            text = str(item.get("text") or "").strip()
            evidence = self._find_supporting_evidence_excerpt(
                text=text,
                grounding_text=combined,
            )
            if not evidence and response_text:
                evidence = response_text[:220]
            with_evidence.append(
                {
                    "text": text,
                    "evidence_excerpt": evidence,
                }
            )
        # Return up to 6 candidates so the caller's dedup has a full pool to
        # rotate through; final slicing to 3 happens at the call site.
        return self._dedupe_chip_candidate_texts(with_evidence, limit=6)

    def schedule_post_response_chip_job(
        self,
        *,
        session_id: str,
        assistant_turn_id: str = "",
        user_message: str,
        context: ConversationContext,
        decision: OrchestrationDecision,
        selected_phase_id: str,
        selected_phase_name: str,
        target_service: dict[str, Any] | None = None,
        prefetched_services: list[dict[str, Any]] | None = None,
        history_bundle: dict[str, Any] | None = None,
        faq_candidates: list[dict[str, Any]] | None = None,
        memory_snapshot: dict[str, Any] | None = None,
        in_phase_services: list[dict[str, Any]] | None = None,
        out_of_phase_services: list[dict[str, Any]] | None = None,
        book_route_context: dict[str, Any] | None = None,
        main_router_hint: dict[str, Any] | None = None,
        main_router_book_ids: list[str] | None = None,
        book_route_status: str = "",
        orchestration_trace_id: str = "",
    ) -> None:
        if not session_id:
            return
        if not isinstance(decision, OrchestrationDecision):
            return
        if not str(decision.response_text or "").strip():
            return
        # Tenant-level chip kill switch (opt-out list). Skipping the job here also
        # saves the chip LLM call; /api/chat/suggestions then finds no job and
        # returns no chips.
        if self.suggestion_chips_disabled_for_hotel(getattr(context, "hotel_code", "")):
            logger.info(
                "[CHIP_TURN] skipped session=%s reason=chips_disabled_for_hotel hotel=%s",
                session_id,
                str(getattr(context, "hotel_code", "") or ""),
            )
            return
        # Skip ONLY for active form-collection turns with no pre-emitted chips.
        # Cache-hit replays can carry a non-respond_only cached decision (e.g.
        # a service-agent slot turn) — we still want follow-up chips for the
        # next turn so the UI never goes silent after a chip click.
        action = str(decision.action or "").strip().lower()
        seeded = self._normalize_suggested_actions(
            getattr(decision, "suggested_actions", None) or []
        )[: self._chip_speculation_max_per_session]
        if action == "collect_info" and not seeded:
            logger.info(
                "[CHIP_TURN] skipped session=%s turn=%s reason=form_collect_no_seeded action=%s pending_action=%s response_chars=%d",
                session_id,
                str(assistant_turn_id or "")[:16],
                action,
                str(getattr(decision, "pending_action", "") or "").strip(),
                len(str(decision.response_text or "")),
            )
            return

        # Read previous job's chips before cancelling so we can:
        #  1. Use them as seeded fallback when this turn's decision has no suggested_actions (Bug 4)
        #  2. Pass them to the suggestion runner so it can avoid repeating them (Bug 1)
        prev_job = self._post_response_chip_jobs.get(session_id)
        prev_job_fresh: list[SuggestionChip] = (
            self._normalize_suggestion_chips(prev_job.get("chips") or [])
            if isinstance(prev_job, dict) else []
        )
        prev_job_seeded: list[SuggestionChip] = (
            self._normalize_suggestion_chips(prev_job.get("seeded_chips") or [])
            if isinstance(prev_job, dict) else []
        )
        # Build the "recently shown" text set for deduplication.
        # Merge: fresh chips from previous runner (most likely what user saw) +
        # whatever the prefetch service batches recorded.
        try:
            from services.suggestion_prefetch_service import suggestion_prefetch_service as _pf_svc
            _pf_recently = _pf_svc.get_recently_shown_chip_texts(session_id, n_batches=3)
        except Exception:
            _pf_recently = []
        _seen_dedup: set[str] = set()
        recently_shown_chip_texts: list[str] = []
        for _chip_src in (prev_job_fresh or prev_job_seeded):
            _t = str(getattr(_chip_src, "text", "") or "").strip()
            if _t and _t.lower() not in _seen_dedup:
                _seen_dedup.add(_t.lower())
                recently_shown_chip_texts.append(_t)
        for _t in _pf_recently:
            if _t and _t.lower() not in _seen_dedup:
                _seen_dedup.add(_t.lower())
                recently_shown_chip_texts.append(_t)
        recently_shown_chip_texts = recently_shown_chip_texts[:9]
        # ...plus everything else this conversation has already been offered.
        _pd_conv = context.pending_data if isinstance(getattr(context, "pending_data", None), dict) else {}
        _conv_uid_for_chips = str(_pd_conv.get("_active_conversation_uid") or "").strip()
        for _t in self.chips_shown_in_conversation(session_id, _conv_uid_for_chips):
            if _t and _t.lower() not in _seen_dedup:
                _seen_dedup.add(_t.lower())
                recently_shown_chip_texts.append(_t)
        recently_shown_chip_texts = recently_shown_chip_texts[:40]

        self._cancel_post_response_chip_job(session_id)
        resolved_target_service = self._resolve_target_service_for_followups(
            decision=decision,
            target_service=target_service,
            prefetched_services=prefetched_services,
            in_phase_services=in_phase_services,
            out_of_phase_services=out_of_phase_services,
        )
        origin_responder = "service" if isinstance(resolved_target_service, dict) else "main"
        origin_service_id = str(
            (resolved_target_service or {}).get("id") if isinstance(resolved_target_service, dict) else ""
            or ""
        ).strip()
        pending_data = context.pending_data if isinstance(context.pending_data, dict) else {}
        public_pending = self._coerce_public_pending(pending_data)
        active_property_ids = self._normalize_property_id_list(
            list(public_pending.get("active_property_ids") or [])
        )
        selected_book_ids = [
            str(book_id).strip()
            for book_id in list(main_router_book_ids or [])
            if str(book_id).strip()
        ]
        primary_grounding_text = str(
            (book_route_context or {}).get("context_text") if isinstance(book_route_context, dict) else ""
            or ""
        ).strip()
        target_service_kb_by_id: dict[str, str] = {}
        for service_row in list(prefetched_services or []) + list(in_phase_services or []):
            if not isinstance(service_row, dict):
                continue
            service_id = self._normalize_identifier(service_row.get("id"))
            if not service_id:
                continue
            pack = service_row.get("service_prompt_pack") or {}
            if not isinstance(pack, dict):
                pack = {}
            kb_text = str(
                service_row.get("extracted_knowledge")
                or pack.get("extracted_knowledge")
                or ""
            ).strip()
            if kb_text:
                try:
                    kb_text = self._scope_service_knowledge_to_runtime_property(
                        service_knowledge=kb_text,
                        user_message=user_message,
                        context=context,
                    )
                except Exception:
                    pass
            if kb_text:
                target_service_kb_by_id[service_id] = kb_text
        if origin_responder == "service":
            service_kb = ""
            if isinstance(resolved_target_service, dict):
                pack = resolved_target_service.get("service_prompt_pack") or {}
                if not isinstance(pack, dict):
                    pack = {}
                service_kb = str(
                    resolved_target_service.get("extracted_knowledge")
                    or pack.get("extracted_knowledge")
                    or ""
                ).strip()
                if service_kb:
                    try:
                        service_kb = self._scope_service_knowledge_to_runtime_property(
                            service_knowledge=service_kb,
                            user_message=user_message,
                            context=context,
                        )
                    except Exception:
                        pass
            primary_grounding_text = service_kb or primary_grounding_text
        # `chips` holds freshly LLM-generated chips. `seeded_chips` holds the
        # decision's pre-emitted chips (typical on cache-hit replay) and is
        # served as fallback when the runner has not yet produced fresh ones.
        # This preserves the invariant that fresh chips always overwrite
        # seeded chips, while keeping chip-click replays from going silent
        # in the brief window before fresh generation completes.
        seeded_chip_objects = self._validate_generated_suggestion_chips(
            candidates=list(seeded),
            turn_trace_id=str(assistant_turn_id or "").strip(),
            origin_responder=origin_responder,
            origin_service_id=origin_service_id,
            target_service_kb_by_id=target_service_kb_by_id,
            primary_grounding_text=primary_grounding_text,
            assistant_response=str(decision.response_text or "").strip(),
            active_property_ids=active_property_ids,
            selected_book_ids=selected_book_ids,
            allowed_service_ids=list(target_service_kb_by_id.keys()),
            followup_question=str(decision.followup_question or ""),
            hotel_code=str(getattr(context, "hotel_code", "") or ""),
        )
        runtime_snapshot = self._build_post_response_chip_runtime_snapshot(
            context=context,
            decision=decision,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            resolved_target_service=resolved_target_service if isinstance(resolved_target_service, dict) else None,
            memory_snapshot=memory_snapshot,
        )
        # Only use chips that came directly from the orchestration decision's
        # suggested_actions (LLM-generated). Deterministic template fallback
        # and previous-turn carry-overs are disabled — we wait for the async
        # runner to produce fresh LLM chips instead.
        effective_seeded = list(seeded_chip_objects)

        job: dict[str, Any] = {
            "assistant_turn_id": str(assistant_turn_id or "").strip(),
            "assistant_response": str(decision.response_text or "").strip(),
            "runtime_snapshot": runtime_snapshot,
            "chips": [],
            "seeded_chips": effective_seeded,
            "task": None,
            "ts": time.time(),
        }
        self._post_response_chip_jobs[session_id] = job
        logger.info(
            "[CHIP_TURN] scheduled session=%s turn=%s origin=%s origin_service=%s action=%s pending_action=%s seeded=%d active_props=%d selected_books=%d book_route_status=%s response_chars=%d",
            session_id,
            str(assistant_turn_id or "")[:16],
            origin_responder,
            origin_service_id,
            action,
            str(getattr(decision, "pending_action", "") or "").strip(),
            len(effective_seeded),
            len(active_property_ids),
            len(selected_book_ids),
            str(book_route_status or "").strip(),
            len(str(decision.response_text or "")),
        )

        _recently_shown_for_runner = list(recently_shown_chip_texts)

        async def _runner() -> list[SuggestionChip]:
            logger.warning(
                "[CHIP_RUNNER] start session=%s turn=%s phase=%s user_msg=%r decision_response_chars=%d",
                session_id, assistant_turn_id[:16] if assistant_turn_id else "",
                selected_phase_id, (user_message or "")[:60],
                len(str(decision.response_text or "")),
            )
            try:
                suggestions = await self._run_next_action_suggestion_agent(
                    assistant_turn_id=assistant_turn_id,
                    user_message=user_message,
                    context=context,
                    decision=decision,
                    selected_phase_id=selected_phase_id,
                    selected_phase_name=selected_phase_name,
                    target_service=resolved_target_service,
                    prefetched_services=prefetched_services,
                    book_route_context=book_route_context,
                    main_router_book_ids=list(main_router_book_ids or []),
                    recently_shown_chip_texts=_recently_shown_for_runner,
                    memory_summary=str((memory_snapshot or {}).get("summary") or ""),
                )
            except Exception as _run_exc:
                logger.warning("[CHIP_RUNNER] _run_next_action_suggestion_agent raised: %r", _run_exc)
                suggestions = []
            logger.warning(
                "[CHIP_RUNNER] llm_returned=%d session=%s turn=%s",
                len(suggestions), session_id, assistant_turn_id[:16] if assistant_turn_id else "",
            )
            fresh = self._normalize_suggestion_chips(suggestions)[: self._chip_speculation_max_per_session]
            # Bug 1 fix: prefer chips not recently shown to the guest.
            # If all fresh chips are repeats (KB is narrow), keep them anyway —
            # showing a repeat is better than showing nothing.
            #
            # Dedup uses a normalised form of the chip text — strip filler
            # ("tell me about", "tell me more about", "more about", "what's at",
            # punctuation) so "Tell me about Splash Poolbar" and "Tell me more
            # about Splash Poolbar" collapse to the same key and are dedup'd as
            # duplicates instead of slipping through.
            def _dedup_key(text: str) -> str:
                t = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())
                t = re.sub(r"\b(tell|me|about|more|what'?s|at|the|a|an|please|can|you|i|hear|share|of|on|for)\b", " ", t)
                return " ".join(t.split())
            if fresh and _recently_shown_for_runner:
                _shown_set = {_dedup_key(t) for t in _recently_shown_for_runner}
                unique = [c for c in fresh if _dedup_key(c.text) not in _shown_set]
                if len(unique) < len(fresh):
                    logger.warning(
                        "[CHIP_RUNNER] dedup dropped %d/%d chips against recently_shown",
                        len(fresh) - len(unique), len(fresh),
                    )
                # If ALL chips are repeats, return [] instead of showing
                # the same chips again. The user just saw them; showing them
                # twice in a row is the worst UX. Empty chip row is better
                # than identical-to-last-turn chip row.
                fresh = unique[: self._chip_speculation_max_per_session]
            logger.warning(
                "[CHIP_RUNNER] done session=%s turn=%s final_chips=%d texts=%r",
                session_id, assistant_turn_id[:16] if assistant_turn_id else "",
                len(fresh), [str(c.text or "")[:40] for c in fresh],
            )
            job["chips"] = fresh
            return fresh

        async def _scoped_runner() -> list[SuggestionChip]:
            """Run the chip job under this turn's hotel.

            Chips are generated after the reply has gone out. On the WebSocket
            path that scheduling happens once the request scope has already been
            reset, so the LLM calls in here logged with no hotel_code at all and
            their cost — a fifth of the turn's — landed unattributed. The
            context we were handed still knows the tenant.
            """
            from services.db_config_service import db_config_service

            scope_code = str(getattr(context, "hotel_code", "") or "").strip()
            scope_token = (
                db_config_service.set_hotel_context(scope_code) if scope_code else None
            )
            try:
                return await _runner()
            finally:
                if scope_token is not None:
                    db_config_service.reset_hotel_context(scope_token)

        task = asyncio.create_task(_scoped_runner())
        job["task"] = task

    async def await_post_response_chip_objects(
        self,
        *,
        session_id: str,
        assistant_turn_id: str = "",
        wait_ms: int = 4000,
    ) -> list[SuggestionChip]:
        if not session_id:
            return []
        job = self._post_response_chip_jobs.get(session_id)
        if not isinstance(job, dict):
            return []
        expected_turn_id = str(assistant_turn_id or "").strip()
        job_turn_id = str(job.get("assistant_turn_id") or "").strip()
        if expected_turn_id and job_turn_id and expected_turn_id != job_turn_id:
            return []

        def _record_served(served: list[SuggestionChip]) -> list[SuggestionChip]:
            texts = [str(c.text or "").strip() for c in served if str(c.text or "").strip()]
            if texts:
                self._recently_shown_chips[session_id] = texts[: self._chip_speculation_max_per_session]
            return served

        # Always prefer fresh chips. If the runner has already finished and
        # produced fresh chips, use those.
        chips = self._normalize_suggestion_chips(job.get("chips"))[: self._chip_speculation_max_per_session]
        if chips:
            return _record_served(chips)
        # Do NOT serve seeded/template chips early — wait for the async runner
        # to produce LLM-generated chips.
        task = job.get("task")
        if task is not None and not task.done():
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=max(0.05, float(wait_ms or 0) / 1000.0),
                )
                if isinstance(result, list):
                    chips = self._normalize_suggestion_chips(result)[: self._chip_speculation_max_per_session]
                    if chips:
                        return _record_served(chips)
            except Exception:
                # Runner failed or timed out — fall through to final re-read.
                pass
        # Re-read fresh chips one more time (runner may have finished
        # between checks).
        chips = self._normalize_suggestion_chips(job.get("chips"))[: self._chip_speculation_max_per_session]
        if chips:
            return _record_served(chips)
        return []

    async def await_post_response_chips(
        self,
        *,
        session_id: str,
        assistant_turn_id: str = "",
        wait_ms: int = 4000,
    ) -> list[str]:
        chips = await self.await_post_response_chip_objects(
            session_id=session_id,
            assistant_turn_id=assistant_turn_id,
            wait_ms=wait_ms,
        )
        return self._chip_texts(chips)

    def get_post_response_chip_runtime_snapshot(
        self,
        *,
        session_id: str,
        assistant_turn_id: str = "",
    ) -> dict[str, Any]:
        if not session_id:
            return {}
        job = self._post_response_chip_jobs.get(session_id)
        if not isinstance(job, dict):
            return {}
        expected_turn_id = str(assistant_turn_id or "").strip()
        job_turn_id = str(job.get("assistant_turn_id") or "").strip()
        if expected_turn_id and job_turn_id and expected_turn_id != job_turn_id:
            return {}
        snapshot = job.get("runtime_snapshot")
        if not isinstance(snapshot, dict):
            return {}
        return {
            "conversation_state": str(snapshot.get("conversation_state") or "").strip(),
            "pending_action": str(snapshot.get("pending_action") or "").strip(),
            "pending_data_public": dict(snapshot.get("pending_data_public") or {}),
            "known_context": dict(snapshot.get("known_context") or {}),
            "memory_snapshot": dict(snapshot.get("memory_snapshot") or {}),
            "phase_id_hint": str(snapshot.get("phase_id_hint") or "").strip(),
            "phase_name_hint": str(snapshot.get("phase_name_hint") or "").strip(),
            "active_service_id": str(snapshot.get("active_service_id") or "").strip(),
            "active_service": dict(snapshot.get("active_service") or {}),
            "conversation_history": [
                dict(row)
                for row in list(snapshot.get("conversation_history") or [])
                if isinstance(row, dict)
            ],
            "assistant_response": str(job.get("assistant_response") or "").strip(),
        }

    def get_post_response_chip_job_status(
        self,
        *,
        session_id: str,
        assistant_turn_id: str = "",
    ) -> dict[str, Any]:
        if not session_id:
            return {
                "exists": False,
                "pending": False,
                "ready": False,
                "chips_count": 0,
                "seeded_count": 0,
            }
        job = self._post_response_chip_jobs.get(session_id)
        if not isinstance(job, dict):
            return {
                "exists": False,
                "pending": False,
                "ready": False,
                "chips_count": 0,
                "seeded_count": 0,
            }
        expected_turn_id = str(assistant_turn_id or "").strip()
        job_turn_id = str(job.get("assistant_turn_id") or "").strip()
        if expected_turn_id and job_turn_id and expected_turn_id != job_turn_id:
            return {
                "exists": False,
                "pending": False,
                "ready": False,
                "chips_count": 0,
                "seeded_count": 0,
            }
        chips = self._normalize_suggestion_chips(job.get("chips"))[: self._chip_speculation_max_per_session]
        seeded = self._normalize_suggestion_chips(job.get("seeded_chips"))[: self._chip_speculation_max_per_session]
        task = job.get("task")
        task_done = bool(task is None or getattr(task, "done", lambda: True)())
        pending = bool(task is not None and not task_done and not chips)
        return {
            "exists": True,
            "pending": pending,
            "ready": bool(chips),
            "chips_count": len(chips),
            "seeded_count": len(seeded),
            "task_done": task_done,
            "assistant_turn_id": job_turn_id,
        }

    @staticmethod
    def _drop_unsupported_existence_chips(chips: list[str], kb_text: str) -> list[str]:
        # Belt-and-suspenders: even though the prompt says "never ask 'do you
        # have X?' unless the KB states X exists positively", LLMs occasionally
        # drift. This deterministic guard drops existence-style chips whose
        # subject does not actually appear anywhere in the active KB text.
        # Non-existence chips (informational follow-ups) are passed through
        # unchanged so we do not over-filter.
        if not chips:
            return []
        kb_lower = (kb_text or "").lower()
        if not kb_lower.strip():
            return list(chips)

        existence_patterns = [
            re.compile(r"^\s*do you (?:have|offer|provide|do)\s+(.+?)\??\s*$", re.IGNORECASE),
            re.compile(r"^\s*do you guys (?:have|offer|provide)\s+(.+?)\??\s*$", re.IGNORECASE),
            re.compile(r"^\s*is there (?:a |an )?(.+?)\??\s*$", re.IGNORECASE),
            re.compile(r"^\s*are there (?:any )?(.+?)\??\s*$", re.IGNORECASE),
            re.compile(r"^\s*does the hotel (?:have|offer|provide)\s+(.+?)\??\s*$", re.IGNORECASE),
            re.compile(r"^\s*does this (?:hotel|property) (?:have|offer|provide)\s+(.+?)\??\s*$", re.IGNORECASE),
        ]
        stopwords = {
            "a", "an", "the", "any", "some", "your", "this", "that", "those", "these",
            "available", "service", "services", "facility", "facilities", "option",
            "options", "kind", "kinds", "type", "types", "sort", "sorts",
            "and", "or", "of", "in", "on", "at", "for", "with", "you", "have",
            "offer", "provide", "hotel", "property",
        }

        kept: list[str] = []
        for chip in chips:
            text = str(chip or "").strip()
            if not text:
                continue
            target_phrase = ""
            for pat in existence_patterns:
                m = pat.match(text)
                if m:
                    target_phrase = m.group(1).strip(" ?.!,")
                    break
            if not target_phrase:
                # Not an existence-question; informational chips pass through.
                kept.append(text)
                continue
            tokens = [
                tok for tok in re.findall(r"[a-zA-Z]{3,}", target_phrase.lower())
                if tok not in stopwords
            ]
            if not tokens:
                # Phrase is too generic to verify; keep it.
                kept.append(text)
                continue
            if any(tok in kb_lower for tok in tokens):
                kept.append(text)
            # else: silently drop — KB has no evidence this exists.
        return kept

    @staticmethod
    def _chip_is_positive_informational(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if not normalized:
            return False
        disallowed_phrases = (
            "book",
            "booking",
            "book now",
            "reserve",
            "reservation",
            "availability",
            "book a table",
            "reserve a table",
            "request",
            "airport transfer",
            "rates",
            "rate",
            "price",
            "cost",
            "cheap",
            "cheapest",
            "breakfast",
            "photo",
            "photos",
            "picture",
            "pictures",
            "image",
            "images",
            "see room",
            "show me photo",
            "call me",
            "callback",
            "can you arrange",
        )
        # Whole-word/phrase match so we don't over-block legitimate chips
        # (e.g. "rate" must not trip on "celebrate", "cost" not on "Costa").
        for phrase in disallowed_phrases:
            if re.search(r"\b" + re.escape(phrase) + r"\b", normalized):
                return False
        return True

    # A chip built ONLY from these says nothing of its own — tapping it sends a
    # word the next turn has to guess the meaning of. Anything else in the chip
    # ("Yes, please arrange housekeeping", "No specific restrictions") is an
    # answer with a subject, and is left alone.
    _BARE_AGREEMENT_WORDS = frozenset({
        "yes", "yeah", "yep", "yup", "sure", "okay", "ok", "no", "nope", "nah",
        "please", "thanks", "thank", "you", "go", "ahead", "do", "it", "that",
        "of", "course", "alright", "fine", "not", "now",
    })

    @classmethod
    def _chip_says_only_agreement(cls, text: str) -> bool:
        """True when the chip is nothing but a yes or a no."""
        words = re.findall(r"[a-z]+", str(text or "").lower())
        return bool(words) and all(w in cls._BARE_AGREEMENT_WORDS for w in words)

    @staticmethod
    def _chip_is_affirmation_or_decline(text: str) -> bool:
        # A short yes/no answer to a question the bot just asked ("Would you
        # like our team to reach out?") is a conversational reply, not a topic
        # or service chip. It legitimately has no KB subject and no own service
        # lane, so it must never be dropped for evidence or service-routing
        # reasons — otherwise the natural "Yes, please" / "No, thanks" pair gets
        # split (only one half survives, which reads as a bug to the guest).
        words = re.findall(r"[a-z]+", str(text or "").lower())
        if not words or len(words) > 4:
            return False
        return words[0] in {
            "yes", "yeah", "yep", "yup", "sure", "okay", "ok",
            "no", "nope", "nah",
        }

    @staticmethod
    def _chip_is_decision_help(text: str) -> bool:
        # A comparison / "help me choose" chip the guest taps when the bot just
        # listed several of its own properties (e.g. "Which is better for a
        # business trip?", "Which one suits a friends' getaway?", "Help me pick
        # one", "Which is closer to the airport?"). These are subjective
        # selection aids with no single literal KB phrase, so they must bypass
        # the literal-evidence requirement (the bot answers by comparing the
        # properties it already has KB for).
        norm = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if not norm:
            return False
        if re.search(r"\bhelp me (choose|pick|decide)\b", norm):
            return True
        # Comparative "which …?" — the guest comparing the options the bot just
        # listed. Covers an explicit subject ("which hotel/room/property…"), a
        # comparative adjective ("Which is better for a family stay?", "Which
        # room is the largest?"), and the "which … for <use-case>" shape
        # ("Which is good for business?"). Price superlatives are NOT special-
        # cased here — price chips are already blocked upstream by
        # _chip_is_positive_informational.
        if re.search(r"\bwhich\b", norm) and (
            re.search(r"\bwhich (one|hotel|property|place|resort|room|suite|villa|cottage)\b", norm)
            or re.search(
                r"\b(better|best|worse|ideal|suitable|suits|suited|largest|biggest|smallest|closest|nearest|quietest)\b",
                norm,
            )
            or re.search(r"\bwhich\b.*\bfor\b", norm)
        ):
            return True
        # Side-by-side comparisons ("What does each have?", "amenities in each",
        # "compare them", "A vs B").
        if re.search(r"\b(in each|for each|of each|does each|do each|compare|versus|vs)\b", norm):
            return True
        return False

    @staticmethod
    def _normalize_chip_match_text(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").strip().lower())).strip()

    @classmethod
    def _chip_content_tokens(cls, text: str) -> list[str]:
        generic = {
            "what", "which", "how", "tell", "more", "about", "can", "could", "would", "please",
            "the", "a", "an", "do", "does", "is", "are", "there", "you", "your", "my", "me",
            "this", "that", "these", "those", "hotel", "property", "available", "offer", "offers",
            "include", "included", "details", "detail", "information", "info", "options", "option",
            "services", "service", "facilities", "facility", "amenities", "feature", "features",
            "room", "rooms", "more", "tell", "show", "share", "have", "types", "type",
            # Affirmation / acknowledgement words. Treating them as generic lets
            # LLM-generated short replies like "Yes, please" / "No, thanks" /
            # "Sure!" / "Okay, go ahead" pass the evidence gate as context-relative
            # chips when the bot just asked a yes/no question.
            "yes", "yep", "yeah", "sure", "okay", "thanks", "nope",
        }
        tokens: list[str] = []
        seen: set[str] = set()
        for token in re.findall(r"[a-z0-9]{3,}", cls._normalize_chip_match_text(text)):
            if token in generic or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    @staticmethod
    def _chip_token_aliases(token: str) -> list[str]:
        alias_map = {
            "hours": ["hour", "open", "opening", "timing", "timings", "am", "pm"],
            "timings": ["timing", "open", "opening", "hours", "am", "pm"],
            "checkin": ["check", "checkin", "arrival", "arrivals"],
            "checkout": ["checkout", "depart", "departure", "leaving"],
            "rules": ["rule", "rules", "policy", "policies", "restricted", "restriction"],
            "policy": ["policy", "policies", "rule", "rules"],
            "spa": ["spa", "wellness"],
            "pool": ["pool", "swimming"],
            "dining": ["dining", "restaurant", "restaurants", "eat", "food"],
        }
        aliases = alias_map.get(str(token or "").strip().lower(), [])
        return [token, *aliases] if token else aliases

    @classmethod
    def _chip_text_supported_by_text(cls, *, text: str, supporting_text: str) -> bool:
        haystack = cls._normalize_chip_match_text(supporting_text)
        if not haystack:
            return False
        tokens = cls._chip_content_tokens(text)
        if not tokens:
            return False
        for token in tokens:
            aliases = cls._chip_token_aliases(token)
            if not any(alias and alias in haystack for alias in aliases):
                return False
        return True

    @classmethod
    def _chip_repeats_answered_fact(cls, *, chip_text: str, assistant_response: str) -> bool:
        chip_norm = cls._normalize_chip_match_text(chip_text)
        response_norm = cls._normalize_chip_match_text(assistant_response)
        if not chip_norm or not response_norm:
            return False
        category_terms = {
            "hours": ["hours", "timings", "time", "open", "opening"],
            "checkin": ["checkin", "check in", "arrival"],
            "checkout": ["checkout", "check out", "departure"],
            "rules": ["rules", "policy", "policies", "restricted", "restriction"],
        }
        detected_category = ""
        for category, terms in category_terms.items():
            if any(term in chip_norm for term in terms):
                detected_category = category
                break
        if not detected_category:
            return False
        subject_tokens = [
            token
            for token in cls._chip_content_tokens(chip_text)
            if token not in {"hours", "timings", "time", "open", "opening", "checkin", "checkout", "rules", "policy", "policies"}
        ]
        if subject_tokens and not all(token in response_norm for token in subject_tokens):
            return False
        if detected_category == "hours":
            return bool(re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", assistant_response.lower()) or "open" in response_norm)
        if detected_category == "checkin":
            return "check in" in response_norm or "checkin" in response_norm
        if detected_category == "checkout":
            return "check out" in response_norm or "checkout" in response_norm
        if detected_category == "rules":
            return "rule" in response_norm or "policy" in response_norm
        return False

    @classmethod
    def _chip_is_redundant_existence_question(
        cls, *, chip_text: str, assistant_response: str
    ) -> bool:
        # The bot's reply already NAMED something and the chip just asks whether
        # it exists (e.g. the reply listed "a wellness centre" and "a FunZone",
        # then a chip asks "Do you have a wellness centre?" / "Is there a
        # FunZone?"). That re-confirms a fact the guest just read — drop it.
        # We fire ONLY on yes/no existence phrasing AND only when the chip's
        # specific subject already appears in the reply, so deepening chips
        # ("What's at the wellness centre?") and genuine pivots to topics NOT in
        # the reply ("Do you have a pool?" after a dining answer) are untouched.
        norm = cls._normalize_chip_match_text(chip_text)
        if not norm:
            return False
        existence_markers = (
            "do you have",
            "do you offer",
            "do you provide",
            "have you got",
            "is there",
            "are there",
        )
        if not any(marker in norm for marker in existence_markers):
            return False
        response_norm = cls._normalize_chip_match_text(assistant_response)
        if not response_norm:
            return False
        tokens = cls._chip_content_tokens(chip_text)
        if not tokens:
            return False
        for token in tokens:
            aliases = cls._chip_token_aliases(token)
            if not any(alias and alias in response_norm for alias in aliases):
                return False
        return True

    def _validate_generated_suggestion_chips(
        self,
        *,
        candidates: list[Any],
        turn_trace_id: str,
        origin_responder: str,
        origin_service_id: str,
        target_service_kb_by_id: dict[str, str],
        primary_grounding_text: str,
        assistant_response: str,
        active_property_ids: list[str],
        selected_book_ids: list[str],
        allowed_service_ids: list[str],
        followup_question: str = "",
        hotel_code: str = "",
    ) -> list[SuggestionChip]:
        # DID THE BOT ACTUALLY ASK ANYTHING THIS TURN?
        # The chip job is told, in its first rule, that "Yes, please"/"No, thanks"
        # are only for a yes/no question it just asked. It does not always obey.
        # Zone declined a cake with "Once you're checked in, the team can help with
        # that." - a statement, with followup_question empty - and the chip job put
        # "Yes, please" in front of the guest anyway. Tapping it sent a bare "yes"
        # that the router then read as accepting a staff handoff nobody offered,
        # which opened the support form and raised a cake ticket.
        # The turn's own decision already carries the answer, so the code checks it
        # instead of trusting the chip model's word for it.
        bot_asked_question = bool(str(followup_question or "").strip()) or (
            "?" in str(assistant_response or "")
        )
        # Pre-booking tab hotels (T24 onward): a reply to the bot's own question is
        # kept on that question, not on the KB - see below.
        try:
            from services import prebooking_settings_service as _pbs_chips

            _replies_to_question_kept = bot_asked_question and _pbs_chips.is_admin_managed(
                str(hotel_code or "").strip().lower()
            )
        except Exception:
            _replies_to_question_kept = False
        defaults = {
            "turn_trace_id": str(turn_trace_id or "").strip(),
            "origin_responder": "service" if str(origin_responder or "").strip().lower() == "service" else "main",
            "origin_service_id": str(origin_service_id or "").strip(),
            "active_property_ids": list(active_property_ids or []),
            "selected_book_ids": list(selected_book_ids or []),
        }
        validated: list[SuggestionChip] = []
        seen: set[str] = set()
        normalized_allowed_service_ids = {
            self._normalize_identifier(service_id)
            for service_id in list(allowed_service_ids or [])
            if self._normalize_identifier(service_id)
        }
        for item in list(candidates or []):
            chip = self._normalize_suggestion_chip(item, defaults=defaults)
            if chip is None:
                continue
            # A yes/no chip with no question behind it is not a reply, it is a
            # trap: it carries no subject of its own, so whatever the next turn
            # decides it meant is unanswerable by the transcript.
            # `_chip_is_affirmation_or_decline` also matches a short chip that
            # merely STARTS with one of those words ("No smoking rooms?", "Ok what
            # about parking?"). Until now it only ever granted an exemption, so a
            # false positive cost nothing; here it deletes, so a chip that asks
            # its own question is kept - a question is not an agreement.
            if (
                self._chip_is_affirmation_or_decline(chip.text)
                and self._chip_says_only_agreement(chip.text)
                and "?" not in str(chip.text or "")
                and not bot_asked_question
            ):
                logger.info(
                    "[CHIP_AGENT] dropping %r - the reply asked no question, so "
                    "there is nothing for a yes/no chip to answer.", chip.text,
                )
                continue
            if not self._chip_is_positive_informational(chip.text):
                continue
            if self._chip_repeats_answered_fact(
                chip_text=chip.text,
                assistant_response=assistant_response,
            ):
                continue
            if self._chip_is_redundant_existence_question(
                chip_text=chip.text,
                assistant_response=assistant_response,
            ):
                continue

            # Yes/No replies to the bot's question are conversational — never
            # route them to a service lane (the LLM sometimes tags "Yes, please"
            # with the handoff service id, which then fails the allowed-service
            # check below and drops half the pair). Keep them in-lane; clicking
            # sends the text and the router re-routes from context.
            if self._chip_is_affirmation_or_decline(chip.text) and chip.target_service_id:
                chip = SuggestionChip.model_validate(
                    {**chip.model_dump(mode="json"), "target_service_id": ""}
                )

            if chip.origin_responder == "service":
                chip = SuggestionChip.model_validate(
                    {
                        **chip.model_dump(mode="json"),
                        "target_service_id": chip.origin_service_id or chip.target_service_id,
                    }
                )
            elif chip.target_service_id:
                normalized_target_service_id = self._normalize_identifier(chip.target_service_id)
                if normalized_target_service_id not in normalized_allowed_service_ids:
                    continue
                chip = SuggestionChip.model_validate(
                    {
                        **chip.model_dump(mode="json"),
                        "target_service_id": normalized_target_service_id,
                    }
                )

            grounding_text = ""
            if chip.target_service_id:
                grounding_text = str(
                    target_service_kb_by_id.get(self._normalize_identifier(chip.target_service_id)) or ""
                ).strip()
            if not grounding_text:
                grounding_text = str(primary_grounding_text or "").strip()
            if not grounding_text:
                continue

            # Context-relative chips: when the chip text uses only generic
            # words (e.g. "What room types are available?"), it has zero
            # content tokens to match against any KB excerpt. These chips
            # are implicitly scoped to whatever the bot just talked about —
            # the LLM was given the response_text and chose to ask a
            # property-agnostic follow-up because the conversation already
            # establishes the subject. As long as there's grounding context
            # (verified above) we accept them with a synthesized excerpt.
            chip_tokens = self._chip_content_tokens(chip.text)
            # A YES OR NO TO THE BOT'S OWN QUESTION RESTS ON THAT QUESTION. Asked
            # "Would you like our team to reach out to assist you with this?", the chip
            # agent wrote "Yes, please have them contact me" and "No, thanks", citing
            # that question. "them" and "contact" are not in the question or the KB,
            # so the yes was dropped and the guest saw only "No, thanks" (T24 UAT, 15
            # Oct). When the chip opens with a yes or a no and the evidence it cites is
            # a question the reply really asked, word for word, that is its evidence.
            _cited = str(chip.evidence_excerpt or "").strip()
            _opening = (re.findall(r"[a-z]+", str(chip.text or "").lower()) or [""])[0]
            if (
                _replies_to_question_kept
                and _cited.endswith("?")
                and self._chip_is_affirmation_or_decline(_opening)
                and self._normalize_chip_match_text(_cited)
                in self._normalize_chip_match_text(assistant_response)
            ):
                evidence_excerpt = _cited
            # Decision-help / comparison chips ("Which is better for families?",
            # "Help me choose one") are conversational selection aids the guest
            # would naturally tap when the bot just listed several properties.
            # They are comparative, so they have no single literal KB excerpt to
            # match — treat them as context-relative (like zero-token chips) so
            # the evidence gate doesn't wrongly drop them.
            elif not chip_tokens or self._chip_is_decision_help(chip.text):
                evidence_excerpt = (
                    str(chip.evidence_excerpt or "").strip()
                    or grounding_text[:220]
                )
            else:
                evidence_excerpt = str(chip.evidence_excerpt or "").strip()
                # Trust LLM-provided evidence: if the LLM cites an excerpt
                # that supports the chip text (all chip tokens are present in
                # the excerpt), we accept the chip. We do NOT additionally
                # require those tokens to appear in the broader
                # `grounding_text` — a *pivot* chip (e.g. "Tell me about
                # Hornbill Banquet" after a turn about Tipsy/Splash/Falcon
                # dining) by definition moves to a topic not in the current
                # reply/history, only in KB. Forcing tokens to appear in
                # grounding_text would kill every legitimate pivot.
                if evidence_excerpt and self._chip_text_supported_by_text(
                    text=chip.text,
                    supporting_text=evidence_excerpt,
                ):
                    pass  # LLM-cited evidence supports chip → accept
                else:
                    # No LLM evidence (or excerpt didn't match the chip
                    # tokens) — fall back to mining an excerpt from the
                    # broader grounding_text. This is the legacy non-pivot
                    # path: chip references something in the response.
                    evidence_excerpt = self._find_supporting_evidence_excerpt(
                        text=chip.text,
                        grounding_text=grounding_text,
                    )
                    if not evidence_excerpt:
                        continue
                    if not self._chip_text_supported_by_text(
                        text=chip.text,
                        supporting_text=evidence_excerpt,
                    ):
                        continue
                    if not self._chip_text_supported_by_text(
                        text=chip.text,
                        supporting_text=grounding_text,
                    ):
                        continue

            chip = SuggestionChip.model_validate(
                {
                    **chip.model_dump(mode="json"),
                    "evidence_excerpt": evidence_excerpt,
                    "evidence_fingerprint": self._build_chip_evidence_fingerprint(
                        text=chip.text,
                        turn_trace_id=chip.turn_trace_id,
                        origin_responder=chip.origin_responder,
                        origin_service_id=chip.origin_service_id,
                        target_service_id=chip.target_service_id,
                        active_property_ids=list(chip.active_property_ids or []),
                        selected_book_ids=list(chip.selected_book_ids or []),
                        evidence_excerpt=evidence_excerpt,
                    ),
                }
            )
            key = chip.text.lower()
            if key in seen:
                continue
            seen.add(key)
            validated.append(chip)
            if len(validated) >= self._chip_speculation_max_per_session:
                break
        return validated

    async def _run_next_action_suggestion_agent(
        self,
        *,
        assistant_turn_id: str = "",
        user_message: str,
        context: ConversationContext,
        decision: OrchestrationDecision,
        selected_phase_id: str,
        selected_phase_name: str,
        target_service: dict[str, Any] | None = None,
        prefetched_services: list[dict[str, Any]] | None = None,
        book_route_context: dict[str, Any] | None = None,
        main_router_book_ids: list[str] | None = None,
        recently_shown_chip_texts: list[str] | None = None,
        memory_summary: str = "",
    ) -> list[SuggestionChip]:
        if not bool(getattr(settings, "chat_llm_next_suggestions_enabled", False)):
            logger.info("[CHIP_AGENT] disabled by setting chat_llm_next_suggestions_enabled")
            return []
        if not str(settings.openai_api_key or "").strip():
            logger.warning("[CHIP_AGENT] openai_api_key is empty — chip LLM skipped")
            return []

        # Determine who answered this turn and extract their KB for grounding.
        _svc_answered = isinstance(target_service, dict)
        answering_llm = "service_agent" if _svc_answered else "main_orchestrator"

        service_payload: dict[str, Any] = {}
        answering_service_kb: dict[str, Any] = {}
        if _svc_answered:
            service_payload = {
                "id": self._normalize_identifier(target_service.get("id")),
                "name": str(target_service.get("name") or "").strip(),
                "profile": str(target_service.get("profile") or "").strip(),
            }
            # Pull KB content so the suggestion agent can ground its suggestions in facts.
            _svc_pack = target_service.get("service_prompt_pack") or {}
            if not isinstance(_svc_pack, dict):
                _svc_pack = {}
            _ek = str(
                target_service.get("extracted_knowledge")
                or _svc_pack.get("extracted_knowledge")
                or ""
            ).strip()
            if _ek:
                try:
                    _ek = self._scope_service_knowledge_to_runtime_property(
                        service_knowledge=_ek,
                        user_message=user_message,
                        context=context,
                    )
                except Exception:
                    pass
            _slots = _svc_pack.get("required_slots") or []
            answering_service_kb = {
                "extracted_knowledge": _ek,  # no truncation — per-file extraction keeps each property small
                "required_slots": _slots if isinstance(_slots, list) else [],
            }

        # Build bot delivery boundary from live config — fully dynamic per property
        pending_data = context.pending_data if isinstance(context.pending_data, dict) else {}
        public_pending = self._coerce_public_pending(pending_data)
        property_scope = {
            "active_property_ids": self._normalize_property_id_list(
                list(public_pending.get("active_property_ids") or [])
            ),
            "active_property_names": [
                str(name).strip()
                for name in list(public_pending.get("active_property_names") or [])
                if str(name).strip()
            ],
            "selected_property_scope_id": str(
                public_pending.get("selected_property_scope_id") or ""
            ).strip(),
            "selected_property_scope_ids": self._normalize_property_id_list(
                list(public_pending.get("selected_property_scope_ids") or [])
            ),
            "selected_property_scope_name": str(
                public_pending.get("selected_property_scope_name") or ""
            ).strip(),
        }

        book_ctx = book_route_context if isinstance(book_route_context, dict) else {}
        answering_book_kb: dict[str, Any] = {}
        _book_context_text = str(book_ctx.get("context_text") or "").strip()
        # Restrict the books we surface to the chip generator to the active
        # property scope. Without this, a multi-property tenant can have a
        # service-agent answer for property A but the chip generator sees
        # books from property B and proposes off-property suggestions.
        _active_scope_ids = {
            str(pid).strip().lower()
            for pid in property_scope.get("active_property_ids") or []
            if str(pid).strip()
        }
        _book_rows: list[dict[str, Any]] = []
        for row in list(book_ctx.get("books") or []):
            if not isinstance(row, dict):
                continue
            _book_id = str(row.get("id") or "").strip()
            _book_title = str(row.get("title") or "").strip()
            _property_id = str(row.get("property_id") or "").strip()
            _property_name = str(row.get("property_name") or "").strip()
            if not (_book_id or _book_title or _property_id or _property_name):
                continue
            # If we know the active property scope, drop rows from other
            # properties. Books with no property_id are shared/common KB —
            # keep them. This mirrors the same scoping rule used by the
            # service-agent KB scoper.
            if _active_scope_ids and _property_id:
                if _property_id.lower() not in _active_scope_ids:
                    continue
            _book_rows.append(
                {
                    "id": _book_id,
                    "title": _book_title,
                    "property_id": _property_id,
                    "property_name": _property_name,
                }
            )
            if len(_book_rows) >= 12:
                break
        if _book_context_text or _book_rows or list(main_router_book_ids or []):
            answering_book_kb = {
                "book_ids": [
                    str(book_id).strip()
                    for book_id in list(main_router_book_ids or [])
                    if str(book_id).strip()
                ]
                or [
                    str(row.get("id") or "").strip()
                    for row in _book_rows
                    if str(row.get("id") or "").strip()
                ],
                "book_count": int(book_ctx.get("book_count") or len(_book_rows)),
                "page_count": int(book_ctx.get("page_count") or 0),
                "books": _book_rows,
                "context_text": _book_context_text[:12000],
            }
        if not _svc_answered and not (answering_book_kb.get("context_text") if isinstance(answering_book_kb, dict) else ""):
            # Main-LLM path with no per-turn book_route_context (e.g. WS path
            # that schedules chips off a minimal decision).
            #
            # Without any KB grounding the chip LLM treats passing mentions in
            # the bot reply ("amenities include spa, gym, pool…") as evidence
            # and suggests deep-dive chips on items the KB doesn't actually
            # document — leading to "What spa services?" when the KB only lists
            # "spa" once with no detail.
            #
            # Fetch a SMALL slice of the active-property KB (~8k chars, 8
            # sources) — enough for the LLM to tell "documented in detail" vs
            # "mentioned in passing", but ~10× smaller and faster than the
            # previous 80KB blast (which made the chip LLM call 20s+ and timed
            # out the frontend).
            full_kb_property_ids = self._normalize_property_id_list(
                property_scope.get("active_property_ids")
                or list(decision.selected_property_ids or [])
                or property_scope.get("selected_property_scope_ids")
                or []
            )
            logger.warning(
                "[CHIP_AGENT] fallback KB plan: active_property_ids=%r decision.selected=%r property_scope.selected=%r resolved_ids=%r",
                property_scope.get("active_property_ids"),
                list(decision.selected_property_ids or []),
                property_scope.get("selected_property_scope_ids"),
                full_kb_property_ids,
            )
            if full_kb_property_ids:
                try:
                    scoped_result = await config_service.get_kb_text_for_properties(
                        property_ids=list(full_kb_property_ids),
                        include_manifest=False,  # manifest just lists property names, wastes char budget
                        include_common=False,  # common pages aren't what we need for follow-up chips
                        past_property_context=(
                            dict(public_pending.get("past_property_context") or {})
                            if isinstance(public_pending.get("past_property_context"), dict)
                            else None
                        ),
                        # max_sources gates the LIBRARY load (all docs across all
                        # properties), not just docs for the requested property.
                        # Set high enough that all pages for the requested
                        # property are reachable; max_scope_chars is the real cap.
                        max_sources=50,
                        max_scope_chars=10000,
                        allow_llm_generation=False,
                    )
                except Exception:
                    scoped_result = {}
                full_kb_text = str(
                    (scoped_result or {}).get("scoped_kb_text") or ""
                ).strip()
                if full_kb_text:
                    answering_book_kb = {
                        **(answering_book_kb if isinstance(answering_book_kb, dict) else {}),
                        "book_ids": list((answering_book_kb or {}).get("book_ids") or []),
                        "book_count": int((answering_book_kb or {}).get("book_count") or 0),
                        "page_count": int((answering_book_kb or {}).get("page_count") or 0),
                        "books": list((answering_book_kb or {}).get("books") or []),
                        "context_text": full_kb_text[:8000],
                    }
                    logger.warning(
                        "[CHIP_AGENT] fetched fallback KB scope: %d chars from %d properties session=%s",
                        len(full_kb_text), len(full_kb_property_ids),
                        str(getattr(context, "session_id", "")),
                    )

        hotel_code = str(getattr(context, "hotel_code", None) or "DEFAULT")
        cap_summary: dict[str, Any] = {}
        try:
            cap_summary = config_service.get_capability_summary(hotel_code)
            raw_caps = cap_summary.get("capabilities", {})
            try:
                from services.db_config_service import db_config_service

                db_caps = await db_config_service.get_capabilities()
                if isinstance(db_caps, dict):
                    raw_caps = db_caps
                db_knowledge = await db_config_service.get_knowledge_config()
                if isinstance(db_knowledge, dict):
                    db_nlu_policy = db_knowledge.get("nlu_policy", {})
                    if isinstance(db_nlu_policy, dict):
                        cap_summary["nlu_policy"] = db_nlu_policy
                if isinstance(prefetched_services, list) and prefetched_services:
                    cap_summary["service_catalog"] = [
                        dict(item) for item in prefetched_services if isinstance(item, dict)
                    ]
                else:
                    _svc_cache_enabled = bool(getattr(settings, "chat_conversation_cache_enabled", False))
                    _svc_cache_ttl = self._session_cache_ttl_seconds(
                        getattr(settings, "chat_conversation_cache_ttl_seconds", 1800)
                    )
                    _svc_cache_scope = (
                        str(getattr(context, "hotel_code", "") or "").strip()
                        or str(getattr(context, "session_id", "") or "").strip()
                    ) if _svc_cache_enabled else ""
                    db_services = (
                        conversation_cache_service.get(_svc_cache_scope, "services", _svc_cache_ttl)
                        if _svc_cache_scope else None
                    )
                    if not isinstance(db_services, list) or not db_services:
                        db_services = await db_config_service.get_services()
                        if isinstance(db_services, list) and _svc_cache_scope:
                            conversation_cache_service.set(_svc_cache_scope, "services", db_services)
                    if isinstance(db_services, list):
                        cap_summary["service_catalog"] = db_services
            except Exception:
                pass
            enabled_capabilities = [
                cap_id
                for cap_id, cap_data in raw_caps.items()
                if isinstance(cap_data, dict) and cap_data.get("enabled", False)
            ]
            nlu_policy = cap_summary.get("nlu_policy", {})
            property_constraints = [
                str(d).strip()
                for d in (nlu_policy.get("donts", []) if isinstance(nlu_policy, dict) else [])
                if str(d).strip()
            ]
        except Exception:
            cap_summary = {}
            enabled_capabilities = []
            property_constraints = []

        bot_delivery_boundary = {
            # The bot's limits as the chip model reads them. "Cannot contact anyone"
            # is here because it was missing: the reply gave the hotel's phone
            # number, the chip job read that as proof the topic was supported, and
            # offered "Can you call the hotel for me?" - which the bot then refused.
            # `enabled_capabilities` is empty for tenants with no capability rows,
            # so the prompt rule that checks against it had nothing to check.
            "medium": (
                "text only — cannot show images, photos, videos, or visual media; "
                "cannot provide real-time availability, live pricing, or data from "
                "external systems not listed as active services; cannot call, dial, "
                "message, email or otherwise contact the hotel, a person or any "
                "third party on the guest's behalf — a phone number in a reply is "
                "there for the GUEST to dial themselves, never something the bot "
                "can do for them, so never suggest asking it to"
            ),
            "enabled_capabilities": enabled_capabilities,
            "property_constraints": property_constraints,
        }

        # Build allowed and blocked service lists for the suggestion agent.
        # Include a kb_hint per allowed service so the agent can verify topics before suggesting.
        try:
            all_services = (
                [dict(item) for item in prefetched_services if isinstance(item, dict)]
                if isinstance(prefetched_services, list) and prefetched_services
                else cap_summary.get("service_catalog", [])
            )
            allowed_services = []
            for s in all_services:
                if not isinstance(s, dict):
                    continue
                if not bool(s.get("is_active", True)):
                    continue
                if self._normalize_identifier(s.get("phase_id")) != selected_phase_id:
                    continue
                svc_name = str(s.get("name") or s.get("id") or "").strip()
                if not svc_name:
                    continue
                _pack = s.get("service_prompt_pack") or {}
                if not isinstance(_pack, dict):
                    _pack = {}
                _kb_hint = str(
                    s.get("extracted_knowledge") or _pack.get("extracted_knowledge") or ""
                ).strip()
                if _kb_hint:
                    try:
                        _kb_hint = self._scope_service_knowledge_to_runtime_property(
                            service_knowledge=_kb_hint,
                            user_message=user_message,
                            context=context,
                        )
                    except Exception:
                        pass
                allowed_services.append({
                    "id": self._normalize_identifier(s.get("id")),
                    "name": svc_name,
                    "can_do": str(s.get("description") or s.get("profile") or "").strip()[:120],
                    "ticketing_enabled": bool(s.get("ticketing_enabled", True)),
                    "kb_hint": _kb_hint,  # factual KB snippet — use this to verify suggestions
                })
            blocked_service_names = [
                str(s.get("name") or s.get("id") or "").strip()
                for s in all_services
                if isinstance(s, dict)
                and bool(s.get("is_active", True))
                and self._normalize_identifier(s.get("phase_id")) != selected_phase_id
                and str(s.get("name") or s.get("id") or "").strip()
            ]
        except Exception:
            allowed_services = []
            blocked_service_names = []
        if _svc_answered:
            allowed_services = []

        _turn_response_language = self._response_language_preference(
            user_message=str(
                getattr(context, "_turn_guest_language_message", "") or user_message or ""
            ),
            history_rows=self._history_preview(context, max_messages=8, max_chars=3000),
            last_assistant_message=self._last_message_by_role(context, "assistant"),
            router_language=getattr(context, "_turn_response_language", None),
        )
        payload = {
            "user_message": str(user_message or "").strip(),
            "response_language": self._sanitize_json(_turn_response_language),
            "response_language_preference": self._sanitize_json(_turn_response_language),
            "assistant_response": str(decision.response_text or "").strip(),
            "answering_llm": answering_llm,
            "assistant_turn_id": str(assistant_turn_id or "").strip(),
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "current_state": context.state.value,
            "pending_action": str(context.pending_action or ""),
            "pending_data_public": self._sanitize_json(public_pending),
            "property_scope": self._sanitize_json(property_scope),
            "decision": {
                "action": str(decision.action or ""),
                "intent": str(decision.intent or ""),
                "target_service_id": str(decision.target_service_id or ""),
                "missing_fields": list(decision.missing_fields or []),
                "followup_question": str(decision.followup_question or ""),
            },
            "service": service_payload,
            "answering_service_kb": answering_service_kb,
            "answering_book_kb": self._sanitize_json(answering_book_kb),
            "history": self._history_preview(context, max_messages=8, max_chars=3000),
            # WHAT THIS CHAT HAS ALREADY COVERED. The chip agent was the only
            # agent not given the running summary - it saw 8 messages and nothing
            # before them, so a pool question asked ten turns ago was invisible
            # and came back as a "new" suggestion. It is not being asked to obey a
            # new rule; it was missing the facts the rule needs.
            "conversation_summary": str(memory_summary or "").strip(),
            "recently_shown_chips": [
                str(t).strip() for t in (recently_shown_chip_texts or []) if str(t).strip()
            ],
            "bot_delivery_boundary": bot_delivery_boundary,
            "allowed_services": allowed_services,
            "blocked_service_names": blocked_service_names,
            "response_contract": {
                "suggested_actions": [
                    {
                        "text": "what the guest would type next",
                        "target_service_id": "exact allowed service id when the chip should jump from main KB into that service; empty when it should stay in the current lane",
                        "evidence": "short supporting KB fact copied or closely paraphrased from the exact scoped knowledge used for this chip",
                    }
                ]
            },
        }
        # Multi-model: chip generation runs on 5.4 (+ 5.4's chip prompt).
        model = self._role_model("next_suggestions")
        try:
            system_prompt = await prompt_registry.get("orchestrator.next_suggestions", model=model)
        except PromptMissingError:
            logger.exception("orchestrator_next_suggestions_prompt_missing")
            return []
        _faq_block_sugg = build_faq_block(
            (context.pending_data or {}).get("_active_property_ids")
            if isinstance(context.pending_data, dict) else None
        )
        if _faq_block_sugg:
            system_prompt = _faq_block_sugg + "\n\n" + system_prompt
        _sugg_bk = self._build_booking_context_prompt_block(context)
        if _sugg_bk:
            system_prompt = system_prompt + "\n" + _sugg_bk
        # `model` was resolved above (self._role_model("next_suggestions")).
        _log_llm_call(
            label="NEXT SUGGESTION AGENT",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt=system_prompt,
            payload=payload,
        )
        logger.info(
            "[CHIP_AGENT] calling LLM model=%r session=%s grounding_kb_chars=%d allowed_services=%d",
            model, str(getattr(context, "session_id", "")),
            len(str(answering_book_kb.get("context_text") or "") if answering_book_kb else ""),
            len(allowed_services),
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    {"role": "system", "content": self._reply_language_directive(for_chips=True)},
                ],
                model=model,
                temperature=0.0,
                trace_context={
                    "responder_type": "service" if _svc_answered else "main",
                    "agent": "next_action_suggestion_agent",
                    "session_id": str(getattr(context, "session_id", "")),
                    "selected_phase_id": selected_phase_id,
                    "selected_phase_name": selected_phase_name,
                    "target_service_id": str(decision.target_service_id or ""),
                },
            )
        except Exception as _llm_exc:
            logger.error("[CHIP_AGENT] LLM call failed: %r", _llm_exc)
            return []
        _log_llm_call(
            label="NEXT SUGGESTION AGENT RESPONSE",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt="",
            payload={},
            response=raw if isinstance(raw, dict) else {},
        )

        if not isinstance(raw, dict):
            return []
        candidates_raw = raw.get("suggested_actions")
        if isinstance(candidates_raw, list) and candidates_raw and isinstance(candidates_raw[0], str):
            candidates_raw = [{"text": str(item or "").strip()} for item in candidates_raw]
        elif isinstance(candidates_raw, list):
            normalized_candidates: list[dict[str, Any]] = []
            for item in candidates_raw:
                if isinstance(item, dict):
                    normalized_candidates.append(
                        {
                            "text": str(item.get("text") or item.get("label") or "").strip(),
                            "target_service_id": str(item.get("target_service_id") or item.get("service_id") or "").strip(),
                            "evidence_excerpt": str(item.get("evidence") or item.get("evidence_excerpt") or "").strip(),
                        }
                    )
                elif str(item or "").strip():
                    normalized_candidates.append({"text": str(item or "").strip()})
            candidates_raw = normalized_candidates
        else:
            candidates_raw = []
        logger.warning(
            "[CHIP_AGENT] LLM returned %d raw candidates session=%s texts=%r",
            len(candidates_raw), str(getattr(context, "session_id", "")),
            [str(c.get("text") or "")[:50] for c in (candidates_raw or []) if isinstance(c, dict)],
        )
        if not candidates_raw:
            return []
        # Build the active KB text the LLM was supposed to ground in:
        # - service-agent answer: only that service's extracted_knowledge
        # - main-orchestrator answer: book context_text + every allowed
        #   service's kb_hint (since main-LLM chips can suggest services)
        target_service_kb_by_id = {
            self._normalize_identifier(svc.get("id")): str(svc.get("kb_hint") or "").strip()
            for svc in allowed_services
            if isinstance(svc, dict)
            and self._normalize_identifier(svc.get("id"))
            and str(svc.get("kb_hint") or "").strip()
        }
        # The bot's actual response text is itself authoritative grounding:
        # the LLM was given it as input and asked to propose follow-up chips
        # about its contents. Including it ensures chips that reference
        # property/topic names mentioned in the reply pass the evidence gate
        # even when the WS path runs with a minimal decision (no book route,
        # no prefetched services).
        assistant_response_text = str(decision.response_text or "").strip()
        # Last few bot replies in history. Named entities the bot mentioned a
        # turn or two ago (e.g. "Hornbill Banquet" when the guest just clicked
        # a chip about Hornbill) are still legitimate evidence for follow-up
        # chips on sibling entities ("Falcon Bar & Grills"). Without this the
        # evidence gate drops perfectly valid chips because the immediate
        # turn's response_text is too narrow.
        history_messages = context.get_recent_messages(8) if hasattr(context, "get_recent_messages") else []
        recent_assistant_text = "\n".join(
            str(getattr(m, "content", "") or "").strip()
            for m in (history_messages or [])
            if str(getattr(getattr(m, "role", None), "value", "") or "").strip().lower() == "assistant"
        )
        if _svc_answered:
            grounding_kb_text = "\n".join(
                p for p in [
                    str(answering_service_kb.get("extracted_knowledge") or ""),
                    assistant_response_text,
                    recent_assistant_text[:4000],
                ] if p
            )
        else:
            grounding_pieces = [
                str(answering_book_kb.get("context_text") or "") if answering_book_kb else "",
                assistant_response_text,
                recent_assistant_text[:4000],
            ]
            for svc in allowed_services:
                if isinstance(svc, dict):
                    grounding_pieces.append(str(svc.get("kb_hint") or ""))
                    grounding_pieces.append(str(svc.get("name") or ""))
                    grounding_pieces.append(str(svc.get("can_do") or ""))
            grounding_kb_text = "\n".join(p for p in grounding_pieces if p)
        logger.warning(
            "[CHIP_AGENT] grounding_kb_chars=%d response_text_chars=%d allowed_service_kb_count=%d session=%s",
            len(grounding_kb_text), len(assistant_response_text),
            len(target_service_kb_by_id),
            str(getattr(context, "session_id", "")),
        )
        validated = self._validate_generated_suggestion_chips(
            candidates=list(candidates_raw or []),
            turn_trace_id=str(assistant_turn_id or "").strip(),
            origin_responder="service" if _svc_answered else "main",
            origin_service_id=str(service_payload.get("id") or "").strip(),
            target_service_kb_by_id=target_service_kb_by_id,
            primary_grounding_text=grounding_kb_text,
            assistant_response=str(decision.response_text or "").strip(),
            active_property_ids=property_scope.get("active_property_ids") or [],
            selected_book_ids=list(answering_book_kb.get("book_ids") or []),
            allowed_service_ids=list(target_service_kb_by_id.keys()),
            followup_question=str(decision.followup_question or ""),
            hotel_code=str(getattr(context, "hotel_code", "") or ""),
        )
        logger.info(
            "[CHIP_AGENT] after evidence gate: %d/%d chips survived session=%s",
            len(validated), len(candidates_raw), str(getattr(context, "session_id", "")),
        )
        if validated:
            kept_texts = set(
                self._drop_unsupported_existence_chips(
                    self._chip_texts(validated),
                    grounding_kb_text,
                )
            )
            final_chips = [
                chip
                for chip in validated
                if str(chip.text or "").strip() in kept_texts
            ]
        else:
            logger.warning(
                "[CHIP_AGENT] all chips dropped by evidence gate — grounding_kb_chars=%d candidates=%r",
                len(grounding_kb_text),
                [str(c.get("text") or "")[:50] for c in (candidates_raw or []) if isinstance(c, dict)],
            )
            final_chips = []

        logger.info(
            "[CHIP_TURN] generated session=%s turn=%s answering_llm=%s origin_service=%s raw=%d validated=%d final=%d active_props=%d selected_books=%d allowed_services=%d",
            str(getattr(context, "session_id", "") or ""),
            str(assistant_turn_id or "")[:16],
            answering_llm,
            str(service_payload.get("id") or "").strip(),
            len(candidates_raw),
            len(validated),
            len(final_chips),
            len(property_scope.get("active_property_ids") or []),
            len(answering_book_kb.get("book_ids") or []) if isinstance(answering_book_kb, dict) else 0,
            len(allowed_services),
        )
        return final_chips

    async def _ensure_suggested_actions(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        decision: OrchestrationDecision,
        selected_phase_id: str,
        selected_phase_name: str,
        target_service: dict[str, Any] | None = None,
        prefetched_services: list[dict[str, Any]] | None = None,
    ) -> OrchestrationDecision:
        _chip_enabled = bool(getattr(settings, "chat_llm_next_suggestions_enabled", False))
        if decision.suggested_actions:
            return decision
        if not _chip_enabled:
            decision.metadata.setdefault("suggested_actions_source", "chat_service_fallback")
            return decision
        # Keep response path non-blocking: chip generation is done by the
        # post-response background job and served through /api/chat/suggestions.
        decision.metadata.setdefault("suggested_actions_source", "post_response_chip_job")
        return decision

    async def _run_quick_chips_llm(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        selected_phase_id: str,
        selected_phase_name: str,
        book_properties: list[dict[str, Any]],
        active_property_ids: list[str],
        in_phase_services: list[dict[str, Any]] | None = None,
        out_of_phase_services: list[dict[str, Any]] | None = None,
        book_context_text: str = "",
    ) -> list[str]:
        """Fast parallel suggestion-chip generator.

        Runs concurrently with the book_answerer so chips do not add to the
        user-perceived response wall-clock. Smaller prompt + smaller output
        than the answerer, so this normally finishes well before the answer.
        Caller bounds any extra wait — if this is slower than the answerer
        the response ships without chips and the chip-upgrade endpoint
        (`/api/chat/suggestions`) fills them later from the frontend.

        Receives the same book_context_text the answerer is reading so the
        chips are KB-grounded — they only propose questions whose answer is
        actually in the loaded books. Without this grounding, chips like
        "What are the pool hours at Bombay Swim Club" leak through even when
        the loaded books don't contain pool hours.
        """
        if not str(settings.openai_api_key or "").strip():
            return []
        # Cap KB excerpt at ~12k chars: enough for the chip LLM to verify
        # topic coverage, small enough to keep this fast (~1-2s).
        kb_excerpt = str(book_context_text or "").strip()
        chip_kb_limit = max(
            3000,
            int(getattr(settings, "chat_llm_quick_chips_kb_chars", 12000) or 12000),
        )
        if len(kb_excerpt) > chip_kb_limit:
            kb_excerpt = kb_excerpt[:chip_kb_limit]
        _turn_response_language = self._response_language_preference(
            user_message=str(
                getattr(context, "_turn_guest_language_message", "") or user_message or ""
            ),
            history_rows=self._history_preview(context, max_messages=5, max_chars=1400),
            last_assistant_message=self._last_message_by_role(context, "assistant"),
            router_language=getattr(context, "_turn_response_language", None),
        )
        payload = {
            "user_message": str(user_message or "").strip()[:600],
            "response_language": self._sanitize_json(_turn_response_language),
            "response_language_preference": self._sanitize_json(_turn_response_language),
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "active_property_ids": list(active_property_ids or []),
            "book_properties": self._sanitize_json(book_properties or []),
            "services_available_now": self._sanitize_json(in_phase_services or []),
            "services_unavailable_now": self._sanitize_json(out_of_phase_services or []),
            "history_last_5": self._history_preview(context, max_messages=5, max_chars=1400),
            "knowledge_base_excerpt": kb_excerpt,
        }
        system_prompt = (
            "You generate 3 short suggestion chips for a hotel concierge chat: what the guest is most likely to ask next.\n"
            "Chips MUST be informational only and MUST have a concrete, positive, KB-grounded answer.\n"
            "\n"
            "Rules:\n"
            "- Output STRICT JSON only: {\"chips\": [\"chip1\", \"chip2\", \"chip3\"]}\n"
            "- ALWAYS emit exactly 3 strong chips, each 2-9 words, first-person guest phrasing. Never return fewer than 3, never leave a slot weak/empty.\n"
            "- Do not repeat what the assistant just answered in `history_last_5`.\n"
            "- If the last assistant message listed items (rooms/outlets/facilities), include specific deep-dive chips for named items from that list.\n"
            "- BROADEN WHEN THE CURRENT TOPIC IS THIN (important): if the immediate topic has few good follow-ups, do NOT drop chips — pull strong chips from OTHER topics the property genuinely has in `knowledge_base_excerpt`: rooms/suites, dining/restaurants, spa/pool/gym/wellness, other amenities, events/banquet venues, location/nearby attractions. ANY KB-grounded factual question about this property is a valid chip. The goal is always 3 useful, varied chips — never a blank or filler slot.\n"
            "- Stay on the active property; never mention or compare with any default/other property.\n"
            "\n"
            "Avoid (these tend to produce negative/unhelpful answers):\n"
            "- Booking/reserving/request actions: book, reserve, table, request, transfer request.\n"
            "- Rates/price/cost, breakfast-included checks, photo/image requests, availability checks.\n"
            "- A chip whose answer would be 'not available' / 'not specified' / 'contact us'. If a topic would land there, switch to a DIFFERENT topic that IS covered — don't just drop to 2 chips.\n"
            "\n"
            "Grounding:\n"
            "- Each chip's answer MUST exist in `knowledge_base_excerpt` (no fabrication). But if one candidate is thin, REPLACE it with a solid grounded chip from another book — always land on 3 real ones.\n"
            "- No emojis, quotes, bullet markers, or explanations.\n"
        )
        # Multi-model: quick-chips is chip generation -> 5.4. Prompt is inline
        # (no DB variant), so only the model switches.
        model = self._role_model("next_suggestions")
        try:
            result = await llm_client.chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    {"role": "system", "content": self._reply_language_directive(for_chips=True)},
                ],
                model=model,
                temperature=0.2,
                max_tokens=140,
                trace_context={
                    "responder_type": "main",
                    "agent": "quick_chips_parallel",
                    "session_id": str(getattr(context, "session_id", "")),
                    "selected_phase_id": selected_phase_id,
                },
            )
        except Exception:
            return []
        if not isinstance(result, dict):
            return []
        return self._normalize_suggested_actions(
            result.get("chips") or result.get("suggested_actions")
        )

    @staticmethod
    def _split_answer_chips_and_focus(text: str) -> tuple[str, list[str], list[str]]:
        """Strip trailing `FOCUS: ...` and `SUGGESTIONS: ...` lines off an answer.

        Single-call LLM emits chips and the property focus inline so the
        response, suggestion chips, and the next-turn KB scope all arrive
        in the same wall-clock time without extra LLM round-trips. Each
        suffix is optional and parsed independently — order between FOCUS
        and SUGGESTIONS is not assumed; either may come last.

        Returns (cleaned_answer, chips, focus_property_ids). Empty lists
        signal "no signal from the LLM" and let callers fall back to
        deterministic defaults / preserve prior state.
        """
        if not text:
            return "", [], []

        chips: list[str] = []
        focus_ids: list[str] = []
        cleaned = text

        # Iterate: peel off SUGGESTIONS / FOCUS suffix lines from the end, in
        # any order, until none match. Two passes are enough in practice since
        # there are only two trailing markers, but we loop defensively in case
        # the LLM emits them in either order.
        for _ in range(3):
            stripped_this_pass = False

            sugg = re.search(
                r"\n+\s*SUGGESTIONS\s*:\s*(?P<chips>[^\n]*?)\s*$",
                cleaned,
                flags=re.IGNORECASE,
            )
            if sugg and not chips:
                chips_blob = sugg.group("chips").strip()
                chips_raw = [
                    re.sub(r"^[\s\-\*\d\.\)\(]*", "", part).strip().strip("\"'")
                    for part in chips_blob.split("|")
                ]
                chips = [c for c in chips_raw if c and 1 <= len(c.split()) <= 12][:3]
                cleaned = cleaned[: sugg.start()].rstrip()
                stripped_this_pass = True

            # Marker word is typo-tolerant: 5.4 sometimes jumbles the letters of
            # FOCUS (e.g. "FCOUS", "FOCS", "FOUCS"). `[FOCUS]{4,6}` matches any 4-6
            # char token built only from F/O/C/U/S, so a misspelled trailing marker
            # is still parsed out AND stripped from the visible reply.
            focus = re.search(
                r"\n+\s*[FOCUS]{4,6}\s*:\s*(?P<ids>[^\n]*?)\s*$",
                cleaned,
                flags=re.IGNORECASE,
            )
            if focus and not focus_ids:
                ids_blob = focus.group("ids").strip()
                # Treat 'none', 'all', '-', empty as "no focus change"
                if ids_blob.lower() not in {"none", "all", "-", ""}:
                    raw_ids = [
                        re.sub(r"^[\s\-\*\d\.\)\(]*", "", part).strip().strip("\"'")
                        for part in re.split(r"[|,]", ids_blob)
                    ]
                    # No count cap: the FOCUS line must be able to carry the FULL
                    # property scope forward (e.g. all 24 hotels after "list all").
                    # A prior [:5] cap silently truncated the scope to 5, which
                    # collapsed the next turn's fleet questions to a stale subset.
                    # The per-id length filter stays as a sanity guard.
                    focus_ids = [i for i in raw_ids if i and len(i) <= 80]
                cleaned = cleaned[: focus.start()].rstrip()
                stripped_this_pass = True

            if not stripped_this_pass:
                break

        # Defensive: the model occasionally emits stray HTML line-break/format
        # tags (<br>, <br/>, <b>, <p>, …) even though the prompt is plain-markdown
        # only. The KB never contains them, so any tag here is model noise — strip
        # it so raw HTML never reaches the widget.
        if cleaned and "<" in cleaned:
            cleaned = re.sub(r"</?(?:br|b|i|p|div|span|strong|em)\s*/?>", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"[ \t]+\n", "\n", cleaned).strip()

        return cleaned, chips, focus_ids

    # Topics the gallery is categorised by. The answerer may name exactly one.
    IMAGE_TOPICS = (
        "facade",
        "room",
        "dining",
        "pool",
        "spa",
        "fitness",
        "banquet",
        "lobby",
        "kids",
    )

    @staticmethod
    def _split_image_topic(text: str) -> tuple[str, str]:
        """Strip a trailing `IMAGES: <topic>` line and return (cleaned, topic).

        The answerer knows what it just talked about, so it names the topic and
        the code picks pictures for it. That replaces matching image TITLES
        against the reply text, which only fired when a title happened to appear
        word-for-word — so mid-conversation replies almost never showed a photo.

        Returns topic="" when the marker is absent or names something we have no
        category for; the caller then attaches nothing rather than guessing.
        """
        if not text:
            return "", ""
        match = re.search(
            r"\n+\s*IMAGES?\s*:\s*(?P<topic>[^\n]*?)\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return text, ""
        raw = re.sub(r"[^a-z]+", "", match.group("topic").strip().lower())
        cleaned = text[: match.start()].rstrip()
        topic = raw if raw in LLMOrchestrationService.IMAGE_TOPICS else ""
        # Tolerate close variants the model reaches for ("rooms", "restaurant").
        if not topic and raw:
            aliases = {
                "rooms": "room", "suite": "room", "suites": "room",
                "restaurant": "dining", "restaurants": "dining", "food": "dining",
                "bar": "dining", "swimmingpool": "pool", "gym": "fitness",
                "exterior": "facade", "building": "facade", "event": "banquet",
                "events": "banquet", "hall": "banquet", "reception": "lobby",
                "children": "kids", "kid": "kids", "wellness": "spa",
            }
            topic = aliases.get(raw, "")
        return cleaned, topic

    @staticmethod
    def _split_answer_and_chips(text: str) -> tuple[str, list[str]]:
        """Backwards-compatible wrapper preserved for any external callers."""
        cleaned, chips, _ = LLMOrchestrationService._split_answer_chips_and_focus(text)
        return cleaned, chips

    def _consume_chip_click_context(self, *, context: ConversationContext) -> dict[str, Any]:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        if not pending:
            return {}
        origin_responder = str(pending.pop("_chip_click_origin_responder", "") or "").strip().lower()
        if origin_responder not in {"main", "service"}:
            origin_responder = ""
        origin_service_id = self._normalize_identifier(pending.pop("_chip_click_origin_service_id", ""))
        target_service_id = self._normalize_identifier(pending.pop("_chip_click_target_service_id", ""))
        active_property_ids = self._normalize_property_id_list(
            pending.pop("_chip_click_active_property_ids", []) or []
        )
        selected_book_ids = [
            str(book_id).strip()
            for book_id in list(pending.pop("_chip_click_selected_book_ids", []) or [])
            if str(book_id).strip()
        ]
        turn_trace_id = str(pending.pop("_chip_click_turn_trace_id", "") or "").strip()
        evidence_fingerprint = str(
            pending.pop("_chip_click_evidence_fingerprint", "") or ""
        ).strip()
        chip_click_hint = bool(
            pending.get("_chip_click_hint")
            or origin_responder
            or origin_service_id
            or target_service_id
            or active_property_ids
            or selected_book_ids
            or turn_trace_id
            or evidence_fingerprint
        )
        return {
            "is_chip_click_followup": chip_click_hint,
            "origin_responder": origin_responder,
            "origin_service_id": origin_service_id,
            "target_service_id": target_service_id,
            "active_property_ids": active_property_ids,
            "selected_book_ids": selected_book_ids,
            "turn_trace_id": turn_trace_id,
            "evidence_fingerprint": evidence_fingerprint,
        }

    def _apply_chip_click_route_lock(
        self,
        *,
        context: ConversationContext,
        selected_phase_id: str,
        main_router_route: str,
        main_router_hint: dict[str, Any] | None,
        chip_click_context: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any]]:
        route = self._normalize_main_router_route(main_router_route)
        hint = dict(main_router_hint or {})
        chip_ctx = dict(chip_click_context or {})
        if not self._coerce_bool(chip_ctx.get("is_chip_click_followup"), default=False):
            return route, hint

        chip_property_ids = self._normalize_property_id_list(
            chip_ctx.get("active_property_ids") or []
        )
        chip_book_ids = [
            str(book_id).strip()
            for book_id in list(chip_ctx.get("selected_book_ids") or [])
            if str(book_id).strip()
        ]
        chip_origin_responder = str(chip_ctx.get("origin_responder") or "").strip().lower()
        chip_origin_service_id = self._normalize_identifier(chip_ctx.get("origin_service_id"))
        chip_target_service_id = self._normalize_identifier(chip_ctx.get("target_service_id"))

        if chip_property_ids:
            hint["selected_property_ids"] = list(chip_property_ids)
            self._persist_book_answerer_focus(
                context=context,
                focus_ids=list(chip_property_ids),
                selected_phase_id=selected_phase_id,
            )
        if chip_book_ids:
            hint["chip_click_seed_book_ids"] = list(chip_book_ids)

        hint["chip_click_route_lock"] = True
        hint["chip_click_origin_responder"] = chip_origin_responder
        if chip_origin_service_id:
            hint["chip_click_origin_service_id"] = chip_origin_service_id
        if chip_target_service_id:
            hint["chip_click_target_service_id"] = chip_target_service_id

        # A chip shown under a service reply was pinned to that service whatever
        # the router said. But the chip agent writes chips about anything the hotel
        # offers, so "What dining options are available?" under a room-booking
        # reply went to the room agent ("this hotel does not provide dining", at a
        # hotel with three restaurants), and a "Yes, please" accepting a staff
        # offer went back to the room agent instead of the team form. When the
        # router actually read the message - it returned a route WITH a confidence,
        # not its skipped-turn fallback (pending flow: confidence 0) - its answer
        # stands, as for info-lane chips below and for typed messages. The chip's
        # target is no exception: the chip agent tags plain questions with the
        # service that answered ("Do you have room service?" -> room agent), so on
        # T24 those chips reached the room agent, which said there is no room
        # service and offered to pass a local-transport request to the team, each
        # time with the booking buttons. At Mango (team-only booking, 8 Oct) the
        # "Yes, please connect me" chip under a room answer was pinned back to the
        # room agent while the router had chosen the team form, so no form opened,
        # no ticket was raised, and the guest was told "Our team has received your
        # request". Only a turn the router did not decide keeps the pin.
        # Admin-managed hotels (T24 onward) and team-only booking hotels (Mango);
        # Zone and Sarovar keep the pin they were tested with.
        from services import prebooking_settings_service
        from services.booking_assistance_state import team_booking_enabled

        _router_decided = (
            route in {"book", "service"}
            and float(hint.get("confidence") or 0.0) > 0.0
        )
        _hotel_lock = str(getattr(context, "hotel_code", "") or "").strip().lower()
        if _router_decided and (
            prebooking_settings_service.is_admin_managed(_hotel_lock)
            or team_booking_enabled(_hotel_lock)
        ):
            if chip_origin_responder == "service" and chip_origin_service_id:
                hint["chip_click_service_bias"] = True
            return route, hint

        if chip_origin_responder == "service" and chip_origin_service_id:
            hint["route"] = "service"
            hint["service_id"] = chip_origin_service_id
            hint["confidence"] = max(float(hint.get("confidence") or 0.0), 0.96)
            hint["service_continuity_blocked"] = True
            return "service", hint

        if chip_target_service_id:
            hint["route"] = "service"
            hint["service_id"] = chip_target_service_id
            hint["confidence"] = max(float(hint.get("confidence") or 0.0), 0.94)
            hint["service_continuity_blocked"] = True
            return "service", hint

        if chip_origin_responder == "main":
            # The chip came from the book_answerer (FAQ). We used to HARD-FORCE the
            # followup back to route=book so an info chip ("tell me more about X")
            # couldn't be mis-routed. But that override also stomped on a legitimate
            # handoff-ACCEPTANCE chip ("yes" / "yes please" after the bot offered a
            # callback): it pinned route=book and the book_answerer dead-ended with
            # "let me get someone..." and NO form opened. A TYPED affirmative never
            # hits this lock and routes correctly (the 5x20 stress test showed
            # typed acceptances pass 100% while chip acceptances failed ~1/3 here).
            # So make the chip path behave like a typed message: keep the
            # main_router's own context-aware route (it already knows this is a
            # chip click via is_chip_click_followup and leans to book for genuine
            # info chips on its own) instead of overriding it. Carry only a soft
            # book-lean hint for telemetry — never a hard route override that can
            # silently kill a handoff-acceptance form.
            hint["chip_click_origin_responder"] = chip_origin_responder
            hint["chip_click_book_bias"] = True
            return route, hint

        return route, hint

    async def _maybe_serve_chip_click_fast_path(
        self,
        *,
        user_message: str,
        context: ConversationContext,
    ) -> "OrchestrationDecision | None":
        """Chip-click fast path (with cascading speculation).

        If the guest clicked one of the chips we shipped on the previous turn,
        the chip text comes back as ``user_message`` verbatim. We already
        speculatively ran the answerer for each chip in the background, so we
        serve the cached decision with zero LLM wait. ``_consume_chip_speculation``
        also clears the session's cache, so stale speculations from the previous
        turn never leak into this one. Pending actions (mid-flow form fills, etc.)
        skip this path so the form/service routing stays authoritative.

        CASCADE: before returning the cached decision, kick off background
        speculation for THIS decision's chips too, so the next click is instant.

        Returns the cached decision to serve immediately, or ``None`` to fall
        through to normal orchestration.
        """
        if not (not bool(str(context.pending_action or "").strip()) and self._legacy_chip_speculation_enabled()):
            return None

        _spec_session_id = str(getattr(context, "session_id", "") or "")
        _cached_entry = self._consume_chip_speculation(
            session_id=_spec_session_id,
            user_message=user_message,
        )
        if _cached_entry is None:
            return None

        _cached_decision = _cached_entry.get("decision")
        if _cached_decision is None:
            _cached_task = _cached_entry.get("task")
            if _cached_task is not None:
                try:
                    _cached_decision = await asyncio.wait_for(
                        asyncio.shield(_cached_task),
                        timeout=max(
                            0.2,
                            float(
                                getattr(
                                    settings,
                                    "chat_chip_speculation_click_wait_ms",
                                    8000,
                                ) or 8000
                            ) / 1000.0,
                        ),
                    )
                except Exception:
                    _cached_decision = None
        if _cached_decision is None:
            return None

        try:
            _cascade_args = _cached_entry.get("spec_args") or {}
            _next_chips = [
                str(c).strip()
                for c in (
                    getattr(_cached_decision, "suggested_actions", None) or []
                )
                if str(c or "").strip()
            ][: self._chip_speculation_max_per_session]
            if (
                _spec_session_id
                and _next_chips
                and _cascade_args
                and self._legacy_chip_speculation_enabled()
            ):
                for _next_chip in _next_chips:
                    self._schedule_chip_speculation(
                        session_id=_spec_session_id,
                        chip_text=_next_chip,
                        user_message_just_asked=str(
                            _cached_entry.get("user_message_just_asked") or ""
                        ),
                        answer_just_given=str(
                            _cached_entry.get("answer_just_given") or ""
                        ),
                        **_cascade_args,
                    )
        except Exception:
            pass
        return _cached_decision
