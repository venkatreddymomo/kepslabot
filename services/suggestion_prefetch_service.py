"""
Suggestion Prefetch Service

Speculatively generates answer candidates for suggestion chips and serves them
through semantic matching on the next user turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional
from uuid import uuid4

from config.settings import settings
from core.context_manager import context_manager
from llm.client import llm_client
from schemas.chat import ChatRequest, ChatResponse, ConversationContext, ConversationState, MessageRole
from schemas.orchestration import OrchestrationDecision, SuggestionChip
from services import conversation_cache_service
from services.chat_service import chat_service
from services.config_service import config_service
from services.llm_orchestration_service import llm_orchestration_service
from services.observability_service import observability_service
from services.ticketing_service import ticketing_service


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _coerce_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed):
        return default
    return parsed


@dataclass
class PrefetchEntry:
    entry_id: str
    chip_text: str
    chip_data: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"  # queued|running|ready|failed
    embedding: list[float] = field(default_factory=list)
    answer_payload: dict[str, Any] = field(default_factory=dict)
    similarity_hint: float = 0.0
    consumed: bool = False
    error: str = ""
    task_key: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)


@dataclass
class PrefetchBatch:
    batch_id: str
    session_id: str
    conversation_id: str
    hotel_code: str
    assistant_turn_id: str
    phase_id: str
    conversation_state: str
    last_bot_message: str
    triggering_user_message: str
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    active_service: dict[str, Any] = field(default_factory=dict)
    services_allowed_in_phase: list[dict[str, Any]] = field(default_factory=list)
    knowledge_base_excerpt: dict[str, str] = field(default_factory=dict)
    capabilities_summary: dict[str, Any] = field(default_factory=dict)
    memory_snapshot: dict[str, Any] = field(default_factory=dict)
    selected_phase_context: dict[str, Any] = field(default_factory=dict)
    pending_action: str = ""
    pending_data_public: dict[str, Any] = field(default_factory=dict)
    # THE BOOKING REQUESTS THE GUEST HAS ALREADY SENT (`_rb_bookings`).
    # Carried beside `pending_data_public` rather than inside it: that dict is
    # built by `_coerce_public_pending`, which drops every underscore key, and
    # widening its mirror list would change what the live answerers and
    # `_suspend_active_service_task` see. This field keeps the fix inside the
    # prefetch feature. Without it the pre-cooked answer is written as if the
    # guest never submitted the booking form, so it still says "Tap Share your
    # booking details below" while the serve-side CTA attach - which reads the
    # REAL context - correctly withholds the spent button.
    booking_ledger: list[dict[str, Any]] = field(default_factory=list)
    turn_revision: str = ""
    response_language: dict[str, Any] = field(default_factory=dict)
    known_context: dict[str, str] = field(default_factory=dict)
    config_fingerprint: str = ""
    context_fingerprint: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    expires_at: datetime = field(default_factory=_utc_now)
    entries: dict[str, PrefetchEntry] = field(default_factory=dict)


class SuggestionPrefetchService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._batches: dict[str, PrefetchBatch] = {}
        self._session_batches: dict[str, list[str]] = {}
        self._entry_tasks: dict[str, asyncio.Task[Any]] = {}
        parallelism = max(1, int(getattr(settings, "chat_suggestion_prefetch_parallelism", 3) or 3))
        self._semaphore = asyncio.Semaphore(parallelism)

    @staticmethod
    def _entry_task_key(*, batch_id: str, entry_id: str) -> str:
        return f"{str(batch_id or '').strip()}::{str(entry_id or '').strip()}"

    def _consume_ready_entry_payload(
        self,
        *,
        batch: PrefetchBatch,
        entry: PrefetchEntry,
        similarity: float,
        threshold: float,
        match_mode: str,
    ) -> dict[str, Any]:
        entry.consumed = True
        entry.similarity_hint = similarity
        entry.updated_at = _utc_now()
        payload = dict(entry.answer_payload or {})
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            payload["metadata"] = metadata
        metadata["prefetch_batch_id"] = batch.batch_id
        metadata["prefetch_entry_id"] = entry.entry_id
        metadata["prefetch_chip_text"] = entry.chip_text
        metadata["prefetch_chip_data"] = dict(entry.chip_data or {})
        metadata["prefetch_similarity"] = similarity
        metadata["prefetch_match_mode"] = match_mode
        metadata["prefetch_threshold"] = threshold
        metadata["prefetch_context_fingerprint"] = str(batch.context_fingerprint or "").strip()
        metadata["prefetch_config_fingerprint"] = str(batch.config_fingerprint or "").strip()
        metadata["prefetch_turn_revision"] = batch.turn_revision
        return payload

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        a_sq = 0.0
        b_sq = 0.0
        for va, vb in zip(a, b):
            dot += va * vb
            a_sq += va * va
            b_sq += vb * vb
        if a_sq <= 0.0 or b_sq <= 0.0:
            return 0.0
        return max(-1.0, min(1.0, dot / (math.sqrt(a_sq) * math.sqrt(b_sq))))

    @staticmethod
    def _dedupe_suggestions(suggestions: list[Any], limit: int) -> list[SuggestionChip]:
        cleaned: list[SuggestionChip] = []
        seen: set[str] = set()
        for item in suggestions:
            raw: dict[str, Any]
            if isinstance(item, SuggestionChip):
                raw = item.model_dump(mode="json")
            elif isinstance(item, dict):
                raw = dict(item)
            else:
                text = " ".join(str(item or "").strip().split())
                if not text:
                    continue
                raw = {"text": text}
            if not str(raw.get("text") or "").strip() and str(raw.get("label") or "").strip():
                raw["text"] = str(raw.get("label") or "").strip()
            if not str(raw.get("text") or "").strip():
                continue
            try:
                chip = SuggestionChip.model_validate(raw)
            except Exception:
                continue
            key = chip.text.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(chip)
            if len(cleaned) >= limit:
                break
        return cleaned

    @staticmethod
    def _coerce_state(value: str) -> ConversationState:
        raw = str(value or "").strip().lower()
        for state in ConversationState:
            if state.value == raw:
                return state
        return ConversationState.IDLE

    @staticmethod
    def _coerce_role(value: str) -> MessageRole:
        raw = str(value or "").strip().lower()
        if raw == MessageRole.USER.value:
            return MessageRole.USER
        if raw == MessageRole.ASSISTANT.value:
            return MessageRole.ASSISTANT
        return MessageRole.SYSTEM

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _safe_json_value(value: Any, *, depth: int = 0) -> Any:
        if depth > 6:
            return str(value)[:800]
        if value is None:
            return None
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return str(value)[:1200]
        if isinstance(value, list):
            return [SuggestionPrefetchService._safe_json_value(item, depth=depth + 1) for item in value[:80]]
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in list(value.items())[:80]:
                result[str(key)[:120]] = SuggestionPrefetchService._safe_json_value(item, depth=depth + 1)
            return result
        return str(value)[:1200]

    @staticmethod
    def _fingerprint(payload: Any) -> str:
        serialized = json.dumps(
            SuggestionPrefetchService._safe_json_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha1(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_prefetchable_answer(text: str) -> bool:
        normalized = _normalize_text(text)
        if not normalized:
            return False
        blocked_markers = (
            "not specified",
            "not provided",
            "not listed",
            "not available",
            "don't have that information",
            "do not have that information",
            "i don't have",
            "i do not have",
            "i can't confirm",
            "i cannot confirm",
            "please contact",
            "you may want to contact",
            "if you want i can check",
            "if you want, i can check",
            "would you like me to check",
            "could you share one more detail",
        )
        return not any(marker in normalized for marker in blocked_markers)

    def _build_config_fingerprint(
        self,
        *,
        hotel_code: str,
        capabilities_summary: dict[str, Any] | None,
    ) -> str:
        cap = dict(capabilities_summary or {})
        faq_signature: list[dict[str, Any]] = []
        for faq in cap.get("faq_bank", []) if isinstance(cap.get("faq_bank", []), list) else []:
            if not isinstance(faq, dict):
                continue
            if not bool(faq.get("enabled", True)):
                continue
            faq_signature.append(
                {
                    "id": self._normalize_identifier(faq.get("id")),
                    "question": str(faq.get("question") or "").strip(),
                    "answer": str(faq.get("answer") or "").strip(),
                    "tags": [str(tag).strip().lower() for tag in (faq.get("tags", []) or []) if str(tag).strip()][:12],
                }
            )
            if len(faq_signature) >= 200:
                break

        service_signature: list[dict[str, Any]] = []
        catalog = cap.get("service_catalog", [])
        if isinstance(catalog, list):
            for row in catalog:
                if not isinstance(row, dict):
                    continue
                service_signature.append(
                    {
                        "id": self._normalize_identifier(row.get("id")),
                        "name": str(row.get("name") or "").strip(),
                        "type": str(row.get("type") or "").strip().lower(),
                        "phase_id": self._normalize_identifier(row.get("phase_id")),
                        "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                        "ticketing_mode": str(row.get("ticketing_mode") or "").strip().lower(),
                        "is_active": bool(row.get("is_active", True)),
                    }
                )
                if len(service_signature) >= 240:
                    break

        payload = {
            "hotel_code": self._normalize_identifier(hotel_code),
            "business_id": self._normalize_identifier(cap.get("business_id")),
            "hotel_name": str(cap.get("hotel_name") or "").strip(),
            "expected_property_count": int(cap.get("expected_property_count") or 0),
            "knowledge_sources": [
                str(src).strip()
                for src in (cap.get("knowledge_sources", []) or [])
                if str(src).strip()
            ][:120],
            "faq_signature": faq_signature,
            "service_signature": service_signature,
        }
        return self._fingerprint(payload)

    def _build_context_fingerprint(
        self,
        *,
        batch: PrefetchBatch,
    ) -> str:
        active_service = batch.active_service if isinstance(batch.active_service, dict) else {}
        active_service_id = self._normalize_identifier(active_service.get("id") or active_service.get("service_id"))
        allowed_ids: list[str] = []
        for row in batch.services_allowed_in_phase if isinstance(batch.services_allowed_in_phase, list) else []:
            if not isinstance(row, dict):
                continue
            sid = self._normalize_identifier(row.get("id") or row.get("service_id"))
            if sid:
                allowed_ids.append(sid)
        kb_excerpt = batch.knowledge_base_excerpt if isinstance(batch.knowledge_base_excerpt, dict) else {}
        payload = {
            "session_id": str(batch.session_id or "").strip(),
            "hotel_code": self._normalize_identifier(batch.hotel_code),
            "assistant_turn_id": str(batch.assistant_turn_id or "").strip(),
            "phase_id": self._normalize_identifier(batch.phase_id),
            "conversation_state": self._normalize_identifier(batch.conversation_state),
            "pending_action": self._normalize_identifier(batch.pending_action),
            "active_service_id": active_service_id,
            "allowed_service_ids": sorted(set(allowed_ids)),
            "known_context": batch.known_context if isinstance(batch.known_context, dict) else {},
            "pending_data_public": batch.pending_data_public if isinstance(batch.pending_data_public, dict) else {},
            # Submitting the booking form changes what an answer is allowed to
            # say, so a batch cooked before it must never be matched against a
            # turn that comes after it.
            "booking_ledger": batch.booking_ledger if isinstance(batch.booking_ledger, list) else [],
            "turn_revision": batch.turn_revision,
            "last_bot_message": _normalize_text(batch.last_bot_message),
            "triggering_user_message": _normalize_text(batch.triggering_user_message),
            "history_tail": batch.conversation_history[-6:] if isinstance(batch.conversation_history, list) else [],
            "kb_excerpt_hash": self._fingerprint(
                {
                    "full_kb_text": str(kb_excerpt.get("full_kb_text") or "").strip(),
                    "service_knowledge_corpus": str(kb_excerpt.get("service_knowledge_corpus") or "").strip(),
                }
            ),
            "config_fingerprint": str(batch.config_fingerprint or "").strip(),
        }
        return self._fingerprint(payload)

    def _rehydrate_pending_data_public(self, pending_data_public: Any) -> dict[str, Any]:
        pending_public = (
            llm_orchestration_service._coerce_public_pending(pending_data_public)
            if isinstance(pending_data_public, dict)
            else {}
        )
        restored = dict(pending_public)
        scope_fields = llm_orchestration_service._canonical_property_scope_fields(
            active_property_ids=pending_public.get("active_property_ids"),
            active_property_names=pending_public.get("active_property_names"),
            selected_property_scope_id=pending_public.get("selected_property_scope_id"),
            selected_property_scope_ids=pending_public.get("selected_property_scope_ids"),
            selected_property_scope_name=pending_public.get("selected_property_scope_name"),
        )
        if scope_fields:
            llm_orchestration_service._sync_property_scope_pending(
                pending_data=restored,
                property_ids=scope_fields.get("active_property_ids"),
                active_property_names=scope_fields.get("active_property_names"),
                selected_property_name=scope_fields.get("selected_property_scope_name"),
            )

        past_property_context = pending_public.get("past_property_context")
        if isinstance(past_property_context, dict) and past_property_context:
            restored["_past_property_context"] = dict(past_property_context)

        stay_property_id = str(pending_public.get("stay_property_id") or "").strip()
        if stay_property_id:
            restored["_stay_property_id"] = stay_property_id
        stay_property_name = str(pending_public.get("stay_property_name") or "").strip()
        if stay_property_name:
            restored["_stay_property_name"] = stay_property_name

        last_service_id = str(
            pending_public.get("last_service_id")
            or pending_public.get("service_id")
            or ""
        ).strip()
        if last_service_id:
            restored["_last_service_id"] = last_service_id
        last_service_name = str(
            pending_public.get("last_service_name")
            or pending_public.get("service")
            or ""
        ).strip()
        if last_service_name:
            restored["_last_service_name"] = last_service_name

        selected_phase_id = str(pending_public.get("selected_phase_id") or "").strip()
        if selected_phase_id:
            restored["_selected_phase_id"] = selected_phase_id

        return restored

    def _build_runtime_context(self, batch: PrefetchBatch) -> ConversationContext:
        known_context = batch.known_context if isinstance(batch.known_context, dict) else {}
        context = ConversationContext(
            session_id=str(batch.session_id or "").strip(),
            hotel_code=str(batch.hotel_code or "").strip() or "default",
            guest_name=str(known_context.get("guest_name") or "").strip() or None,
            room_number=str(known_context.get("room_number") or "").strip() or None,
            state=self._coerce_state(batch.conversation_state),
            pending_action=str(batch.pending_action or "").strip() or None,
            pending_data=self._rehydrate_pending_data_public(batch.pending_data_public),
            channel="web",
        )

        # Put the booking ledger back under its private key, the one the
        # answerer's own grounding block reads. This is what lets a pre-cooked
        # chip answer know the guest has ALREADY sent their details, so it stops
        # telling them to tap a button that the serve path has since withheld.
        if batch.booking_ledger:
            if not isinstance(context.pending_data, dict):
                context.pending_data = {}
            context.pending_data["_rb_bookings"] = [
                dict(row) for row in batch.booking_ledger if isinstance(row, dict)
            ]

        history_rows = batch.conversation_history if isinstance(batch.conversation_history, list) else []
        for row in history_rows[-20:]:
            if not isinstance(row, dict):
                continue
            role = self._coerce_role(str(row.get("role") or ""))
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            context.add_message(role, content)

        if batch.response_language:
            for message in reversed(context.messages):
                if str(getattr(message.role, "value", message.role)) == "assistant":
                    message.metadata["response_language"] = dict(batch.response_language)
                    break

        return context

    async def _generate_with_orchestration_flow(
        self,
        *,
        batch: PrefetchBatch,
        entry: PrefetchEntry,
    ) -> tuple[ChatResponse | None, ConversationContext]:
        context = self._build_runtime_context(batch)
        chip_text = str(entry.chip_text or "").strip()
        chip_data = dict(entry.chip_data or {})
        try:
            chip = SuggestionChip.model_validate(chip_data or {"text": chip_text})
        except Exception:
            chip = SuggestionChip(text=chip_text)
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        if chip.active_property_ids:
            llm_orchestration_service._sync_property_scope_pending(
                pending_data=context.pending_data,
                property_ids=list(chip.active_property_ids or []),
                active_property_names=list(context.pending_data.get("_active_property_names") or []),
            )
        if chip.origin_service_id:
            service_row = config_service.get_service(chip.origin_service_id) or {}
            context.pending_data["_last_service_id"] = str(chip.origin_service_id or "").strip()
            context.pending_data["_last_service_name"] = str(
                service_row.get("name") or chip.origin_service_id or ""
            ).strip()
        context.pending_data["_chip_click_hint"] = True
        context.pending_data["_chip_click_origin_responder"] = str(chip.origin_responder or "").strip()
        context.pending_data["_chip_click_origin_service_id"] = str(chip.origin_service_id or "").strip()
        context.pending_data["_chip_click_target_service_id"] = str(chip.target_service_id or "").strip()
        context.pending_data["_chip_click_active_property_ids"] = list(chip.active_property_ids or [])
        context.pending_data["_chip_click_selected_book_ids"] = list(chip.selected_book_ids or [])
        context.pending_data["_chip_click_turn_trace_id"] = str(chip.turn_trace_id or "").strip()
        context.pending_data["_chip_click_evidence_fingerprint"] = str(chip.evidence_fingerprint or "").strip()
        temp_session_id = f"prefetch::{batch.batch_id}::{uuid4().hex}"
        context.session_id = temp_session_id
        context.hotel_code = str(batch.hotel_code or "").strip() or context.hotel_code or "default"
        context.channel = "web"
        await context_manager.save_context(context, db_session=None)
        prefetched_services = [
            dict(row)
            for row in (batch.services_allowed_in_phase or [])
            if isinstance(row, dict)
        ]
        request = ChatRequest(
            session_id=temp_session_id,
            hotel_code=context.hotel_code,
            message=str(chip_text or "").strip(),
            channel="web",
            metadata={
                "channel": "web",
                "phase": str(batch.phase_id or "").strip(),
                "source_type": "suggested_action",
                "ui_source_type": "suggested_action",
                "prefetch_generation": True,
                "prefetch_parent_session_id": str(batch.session_id or "").strip(),
                "chip_origin_responder": str(chip.origin_responder or "").strip(),
                "chip_origin_service_id": str(chip.origin_service_id or "").strip(),
                "chip_target_service_id": str(chip.target_service_id or "").strip(),
                "chip_active_property_ids": list(chip.active_property_ids or []),
                "chip_selected_book_ids": list(chip.selected_book_ids or []),
                "chip_turn_trace_id": str(chip.turn_trace_id or "").strip(),
                "chip_evidence_fingerprint": str(chip.evidence_fingerprint or "").strip(),
            },
        )
        try:
            response = await chat_service.process_message(
                request,
                db_session=None,
                prefetched_services=prefetched_services,
                defer_summary_refresh=True,
            )
            updated_context = await context_manager.get_context(temp_session_id, db_session=None)
            # Did this pre-cook want to raise a ticket? create_ticket suppressed
            # it (no guest asked for it), and tells us here. Flag it so the entry
            # is thrown away instead of cached: a cache hit replays text only and
            # never re-runs ticketing, so caching this answer would mean the
            # guest clicks the chip, is told their request is with the team, and
            # NO ticket exists anywhere. Dropped => the click runs live.
            if ticketing_service.consume_prefetch_suppression(temp_session_id):
                if isinstance(response, ChatResponse) and isinstance(response.metadata, dict):
                    response.metadata["prefetch_ticket_suppressed"] = True
            # The same for an answer that collects details ("Please provide your
            # contact details..."): its form is opened by the LIVE turn, and a
            # cache hit replays text only - the guest tapped "Yes, connect me to
            # the team" and was asked for details with no form to fill. Dropped =>
            # the click runs live and the form opens. Admin-managed hotels only
            # (T24 onward); Zone, Mango and Sarovar keep their cache as it was.
            # chat_service puts the action under metadata["entities"], the same
            # place the live form trigger reads it from.
            from services import prebooking_settings_service

            _pf_entities = (
                response.metadata.get("entities")
                if isinstance(response, ChatResponse) and isinstance(response.metadata, dict)
                else None
            )
            if (
                isinstance(response, ChatResponse)
                and isinstance(response.metadata, dict)
                and str(
                    (_pf_entities.get("orchestration_action") if isinstance(_pf_entities, dict) else "")
                    or response.metadata.get("orchestration_action")
                    or ""
                ).strip().lower() == "collect_info"
                and prebooking_settings_service.is_admin_managed(str(batch.hotel_code or "").strip().lower())
            ):
                response.metadata["prefetch_ticket_suppressed"] = True
            return response, updated_context or context
        finally:
            llm_orchestration_service._cancel_session_background_work(temp_session_id)
            try:
                await context_manager.delete_context(temp_session_id, db_session=None)
            except Exception:
                pass

    def _collect_ready_candidates(
        self,
        *,
        session_batch_ids: list[str],
        preferred_batch_id: str,
    ) -> list[tuple[PrefetchBatch, PrefetchEntry]]:
        if preferred_batch_id:
            ordered_ids = [preferred_batch_id] + [bid for bid in session_batch_ids if bid != preferred_batch_id]
        else:
            ordered_ids = list(reversed(session_batch_ids))

        candidates: list[tuple[PrefetchBatch, PrefetchEntry]] = []
        for candidate_batch_id in ordered_ids:
            batch = self._batches.get(candidate_batch_id)
            if batch is None:
                continue
            for entry in batch.entries.values():
                if entry.status == "ready" and not entry.consumed and entry.answer_payload:
                    candidates.append((batch, entry))
        return candidates

    async def _embed_text(self, text: str) -> list[float]:
        if not str(settings.openai_api_key or "").strip():
            return []
        content = str(text or "").strip()
        if not content:
            return []
        try:
            response = await llm_client.raw_embeddings_create(
                model=str(getattr(settings, "openai_embedding_model", "") or "text-embedding-3-small"),
                input=[content],
                trace_context={
                    "component": "suggestion_prefetch_embedding",
                },
                purpose="Create embedding for suggestion-chip semantic match",
            )
            if not getattr(response, "data", None):
                return []
            vector = getattr(response.data[0], "embedding", None)
            if isinstance(vector, list):
                return [float(v) for v in vector]
            return []
        except Exception as exc:
            observability_service.log_event(
                "suggestion_prefetch_embedding_failed",
                {"error": str(exc)},
            )
            return []

    async def _generate_answer_for_entry(self, batch_id: str, entry_id: str) -> None:
        async with self._semaphore:
            async with self._lock:
                batch = self._batches.get(batch_id)
                if batch is None or _utc_now() >= batch.expires_at:
                    return
                entry = batch.entries.get(entry_id)
                if entry is None:
                    return
                if entry.status not in {"queued", "failed"}:
                    return
                entry.status = "running"
                entry.updated_at = _utc_now()

                phase_service_names = [
                    str(row.get("name") or "").strip()
                    for row in (batch.services_allowed_in_phase or [])
                    if isinstance(row, dict) and str(row.get("name") or "").strip()
                ][:12]
                generation_payload = {
                    "chip_text": entry.chip_text,
                    "last_bot_message": batch.last_bot_message,
                    "triggering_user_message": batch.triggering_user_message,
                    "conversation_state": batch.conversation_state,
                    "phase_id": batch.phase_id,
                    "pending_data_public": (
                        llm_orchestration_service._coerce_public_pending(batch.pending_data_public)
                        if isinstance(batch.pending_data_public, dict)
                        else {}
                    ),
                    "known_context": batch.known_context if isinstance(batch.known_context, dict) else {},
                    "active_service": batch.active_service,
                    "phase_services": phase_service_names,
                    "history": batch.conversation_history[-8:],
                    "knowledge_excerpt": batch.knowledge_base_excerpt,
                }

            chip_embedding = await self._embed_text(entry.chip_text)
            if not chip_embedding:
                async with self._lock:
                    current_batch = self._batches.get(batch_id)
                    if current_batch is None:
                        return
                    current_entry = current_batch.entries.get(entry_id)
                    if current_entry is None:
                        return
                    current_entry.status = "failed"
                    current_entry.error = "embedding_unavailable"
                    current_entry.updated_at = _utc_now()
                return

            answer_text = ""
            decision: OrchestrationDecision | None = None
            response_obj: ChatResponse | None = None
            runtime_context: ConversationContext | None = None
            try:
                response_obj, runtime_context = await self._generate_with_orchestration_flow(
                    batch=batch,
                    entry=entry,
                )
                if isinstance(response_obj, ChatResponse):
                    answer_text = str(response_obj.message or "").strip()
                    response_metadata = (
                        response_obj.metadata
                        if isinstance(response_obj.metadata, dict)
                        else {}
                    )
                    decision_raw = (
                        response_metadata.get("orchestration_decision")
                        or response_metadata.get("decision")
                    )
                    if isinstance(decision_raw, dict):
                        try:
                            decision = OrchestrationDecision.model_validate(decision_raw)
                        except Exception:
                            decision = None
            except Exception as exc:
                observability_service.log_event(
                    "suggestion_prefetch_orchestration_failed",
                    {"batch_id": batch_id, "entry_id": entry_id, "error": str(exc)},
                )
                answer_text = ""
            # This chip's answer only holds if a ticket is raised, and the
            # pre-cook is not allowed to raise one. Caching it would confirm a
            # request to the guest that nothing was ever filed for, so drop it:
            # "failed" is not servable, and the click falls through to the live
            # pipeline, which tickets properly on the guest's own session.
            if isinstance(response_obj, ChatResponse) and isinstance(response_obj.metadata, dict) \
                    and response_obj.metadata.get("prefetch_ticket_suppressed"):
                async with self._lock:
                    current_batch = self._batches.get(batch_id)
                    if current_batch is None:
                        return
                    current_entry = current_batch.entries.get(entry_id)
                    if current_entry is None:
                        return
                    current_entry.status = "failed"
                    current_entry.error = "ticket_side_effect_not_cacheable"
                    current_entry.updated_at = _utc_now()
                observability_service.log_event(
                    "suggestion_prefetch_dropped_ticket_side_effect",
                    {
                        "batch_id": batch_id,
                        "entry_id": entry_id,
                        "chip_text": str(entry.chip_text or "")[:120],
                    },
                )
                return
            cleaned_answer = str(answer_text or "").strip()
            if not cleaned_answer:
                async with self._lock:
                    current_batch = self._batches.get(batch_id)
                    if current_batch is None:
                        return
                    current_entry = current_batch.entries.get(entry_id)
                    if current_entry is None:
                        return
                    current_entry.status = "failed"
                    current_entry.error = "orchestration_empty_response"
                    current_entry.updated_at = _utc_now()
                return
            if not self._is_prefetchable_answer(cleaned_answer):
                async with self._lock:
                    current_batch = self._batches.get(batch_id)
                    if current_batch is None:
                        return
                    current_entry = current_batch.entries.get(entry_id)
                    if current_entry is None:
                        return
                    current_entry.status = "failed"
                    current_entry.error = "answer_not_positive"
                    current_entry.updated_at = _utc_now()
                return

            public_pending_snapshot = (
                llm_orchestration_service._coerce_public_pending(runtime_context.pending_data)
                if isinstance(runtime_context, ConversationContext)
                and isinstance(runtime_context.pending_data, dict)
                else (
                    llm_orchestration_service._coerce_public_pending(batch.pending_data_public)
                    if isinstance(batch.pending_data_public, dict)
                    else {}
                )
            )
            selected_phase_id = str(
                (batch.selected_phase_context or {}).get("selected_phase_id")
                or batch.phase_id
                or ""
            ).strip()
            selected_phase_name = str(
                (batch.selected_phase_context or {}).get("selected_phase_name")
                or selected_phase_id.replace("_", " ").title()
                or ""
            ).strip()
            decision_payload = decision.model_copy(deep=True) if isinstance(decision, OrchestrationDecision) else None
            if decision_payload is not None and not str(decision_payload.response_text or "").strip():
                decision_payload.response_text = cleaned_answer
            response_metadata = (
                response_obj.metadata
                if isinstance(response_obj, ChatResponse) and isinstance(response_obj.metadata, dict)
                else {}
            )
            cached_suggested_actions = (
                llm_orchestration_service._normalize_suggested_actions(decision_payload.suggested_actions)
                if decision_payload is not None
                else llm_orchestration_service._normalize_suggested_actions(
                    list(response_obj.suggested_actions or [])
                    if isinstance(response_obj, ChatResponse)
                    else []
                )
            )
            selected_property_ids = (
                llm_orchestration_service._normalize_property_id_list(decision_payload.selected_property_ids)
                if decision_payload is not None
                else llm_orchestration_service._normalize_property_id_list(
                    list(response_metadata.get("selected_property_ids") or [])
                )
            )
            if not selected_property_ids:
                selected_property_ids = llm_orchestration_service._normalize_property_id_list(
                    list(
                        public_pending_snapshot.get("selected_property_scope_ids")
                        or public_pending_snapshot.get("active_property_ids")
                        or []
                    )
                )
            selected_property_name = str(
                (decision_payload.selected_property_name if decision_payload is not None else "")
                or response_metadata.get("selected_property_name")
                or public_pending_snapshot.get("selected_property_scope_name")
                or ""
            ).strip()
            target_service_id = str(
                (decision_payload.target_service_id if decision_payload is not None else "")
                or response_metadata.get("target_service_id")
                or response_metadata.get("orchestration_target_service_id")
                or public_pending_snapshot.get("last_service_id")
                or public_pending_snapshot.get("service_id")
                or ""
            ).strip()
            target_service_name = str(
                response_metadata.get("target_service_name")
                or response_metadata.get("orchestration_target_service_name")
                or public_pending_snapshot.get("last_service_name")
                or public_pending_snapshot.get("service")
                or ""
            ).strip()
            if target_service_id:
                service_row = config_service.get_service(target_service_id) or {}
                if not target_service_name:
                    target_service_name = str(service_row.get("name") or target_service_id).strip()

            payload = {
                "message": cleaned_answer,
                "display_message": (
                    str(getattr(response_obj, "display_message", "") or "").strip()
                    if isinstance(response_obj, ChatResponse)
                    else ""
                )
                or cleaned_answer,
                "state": (
                    str(getattr(response_obj.state, "value", response_obj.state) or "").strip()
                    if isinstance(response_obj, ChatResponse)
                    else str(getattr(runtime_context.state, "value", "") or "").strip()
                )
                or (
                    str(getattr(runtime_context.state, "value", "") or "").strip()
                    if isinstance(runtime_context, ConversationContext)
                    else batch.conversation_state or "idle"
                ),
                "suggested_actions": cached_suggested_actions,
                "metadata": {
                    "response_source": "suggestion_prefetch_cache",
                    "response_language": dict(response_metadata.get("response_language") or {}),
                    "language_contract_verified": bool(response_metadata.get("language_contract_verified")),
                    # The books the router answered from - an admin hotel's
                    # menu topics are read from these when the chip is served.
                    "router_book_ids": list(response_metadata.get("router_book_ids") or []),
                    "prefetch_turn_revision": batch.turn_revision,
                    "prefetch_batch_id": batch.batch_id,
                    "prefetch_entry_id": entry.entry_id,
                    "prefetch_chip_text": entry.chip_text,
                    "prefetch_chip_data": dict(entry.chip_data or {}),
                    "prefetch_speculative": True,
                    "prefetch_config_fingerprint": str(batch.config_fingerprint or "").strip(),
                    "prefetch_context_fingerprint": str(batch.context_fingerprint or "").strip(),
                    "cached_decision": (
                        decision_payload.model_dump(mode="json")
                        if decision_payload is not None
                        else None
                    ),
                    "pending_data_public": public_pending_snapshot,
                    "selected_phase_id": selected_phase_id,
                    "selected_phase_name": selected_phase_name,
                    "selected_property_ids": selected_property_ids,
                    "selected_property_name": selected_property_name,
                    "target_service_id": target_service_id,
                    "target_service_name": target_service_name,
                },
            }

            async with self._lock:
                current_batch = self._batches.get(batch_id)
                if current_batch is None:
                    return
                current_entry = current_batch.entries.get(entry_id)
                if current_entry is None:
                    return
                current_entry.embedding = chip_embedding
                current_entry.answer_payload = payload
                current_entry.status = "ready"
                current_entry.error = ""
                current_entry.updated_at = _utc_now()

            observability_service.log_event(
                "suggestion_prefetch_ready",
                {
                    "batch_id": batch_id,
                    "entry_id": entry_id,
                    "session_id": batch.session_id,
                },
            )

    async def _cleanup_expired(self) -> None:
        now = _utc_now()
        expired_batch_ids = [
            batch_id
            for batch_id, batch in self._batches.items()
            if now >= batch.expires_at
        ]
        if not expired_batch_ids:
            return
        tasks_to_cancel: list[asyncio.Task[Any]] = []
        for batch_id in expired_batch_ids:
            batch = self._batches.pop(batch_id, None)
            if batch is None:
                continue
            for entry_id in list(batch.entries.keys()):
                task_key = self._entry_task_key(batch_id=batch_id, entry_id=entry_id)
                task = self._entry_tasks.pop(task_key, None)
                if task is not None and not task.done():
                    tasks_to_cancel.append(task)
            bucket = self._session_batches.get(batch.session_id, [])
            if batch_id in bucket:
                bucket.remove(batch_id)
            if not bucket:
                self._session_batches.pop(batch.session_id, None)
        for task in tasks_to_cancel:
            try:
                task.cancel()
            except Exception:
                pass

    def get_recently_shown_chip_texts(self, session_id: str, n_batches: int = 3) -> list[str]:
        """Return chip texts served in the most recent N batches for this session.

        Used by the chip-generation runner to avoid suggesting chips the guest
        already saw, even when the LLM would otherwise repeat them due to KB
        constraints. Read-only; safe to call without the async lock.
        """
        try:
            session_key = str(session_id or "").strip()
            if not session_key:
                return []
            batch_ids = list(self._session_batches.get(session_key, []))
            recent_ids = batch_ids[-n_batches:] if batch_ids else []
            texts: list[str] = []
            seen: set[str] = set()
            for bid in reversed(recent_ids):
                batch = self._batches.get(bid)
                if batch is None:
                    continue
                for entry in batch.entries.values():
                    t = str(entry.chip_text or "").strip()
                    if t and t.lower() not in seen:
                        seen.add(t.lower())
                        texts.append(t)
            return texts
        except Exception:
            return []

    async def schedule_batch(
        self,
        *,
        session_id: str,
        conversation_id: str = "",
        hotel_code: str,
        assistant_turn_id: str,
        phase_id: str,
        conversation_state: str,
        last_bot_message: str,
        triggering_user_message: str,
        suggestions: list[Any],
        conversation_history: list[dict[str, str]] | None = None,
        active_service: dict[str, Any] | None = None,
        services_allowed_in_phase: list[dict[str, Any]] | None = None,
        knowledge_base_excerpt: dict[str, str] | None = None,
        capabilities_summary: dict[str, Any] | None = None,
        memory_snapshot: dict[str, Any] | None = None,
        selected_phase_context: dict[str, Any] | None = None,
        pending_action: str = "",
        pending_data_public: dict[str, Any] | None = None,
        booking_ledger: list[dict[str, Any]] | None = None,
        turn_revision: str = "",
        response_language: dict[str, Any] | None = None,
        known_context: dict[str, str] | None = None,
    ) -> Optional[str]:
        if not bool(getattr(settings, "chat_suggestion_prefetch_enabled", True)):
            return None
        if not str(settings.openai_api_key or "").strip():
            return None

        cleaned_suggestions = self._dedupe_suggestions(
            list(suggestions or []),
            limit=max(1, int(getattr(settings, "chat_suggestion_prefetch_count", 3) or 3)),
        )
        if not cleaned_suggestions:
            return None

        ttl_seconds = max(
            30,
            int(
                conversation_cache_service.session_ttl_seconds(
                    getattr(settings, "chat_suggestion_prefetch_ttl_seconds", 300)
                )
            ),
        )
        max_batches = max(1, int(getattr(settings, "chat_suggestion_prefetch_max_batches_per_session", 6) or 6))
        kb_chars = max(1200, int(getattr(settings, "chat_suggestion_prefetch_max_kb_chars", 12000) or 12000))
        full_kb = str((knowledge_base_excerpt or {}).get("full_kb_text") or "").strip()
        service_kb = str((knowledge_base_excerpt or {}).get("service_knowledge_corpus") or "").strip()
        history_rows = conversation_history if isinstance(conversation_history, list) else []
        known_context_map = {
            str(k): str(v)
            for k, v in dict(known_context or {}).items()
            if str(k).strip() and str(v).strip()
        }
        effective_cap_summary = (
            dict(capabilities_summary)
            if isinstance(capabilities_summary, dict)
            else {}
        )
        if not effective_cap_summary:
            try:
                effective_cap_summary = config_service.get_capability_summary(hotel_code)
            except Exception:
                effective_cap_summary = {}

        batch_id = f"pf_{uuid4().hex}"
        entries = {
            f"e_{idx+1}": PrefetchEntry(
                entry_id=f"e_{idx+1}",
                chip_text=str(chip.text or "").strip(),
                chip_data=chip.model_dump(mode="json"),
            )
            for idx, chip in enumerate(cleaned_suggestions)
        }
        batch = PrefetchBatch(
            batch_id=batch_id,
            session_id=str(session_id or "").strip(),
            conversation_id=str(conversation_id or "").strip(),
            hotel_code=str(hotel_code or "").strip(),
            assistant_turn_id=str(assistant_turn_id or "").strip(),
            phase_id=str(phase_id or "").strip(),
            conversation_state=str(conversation_state or "").strip().lower() or "idle",
            last_bot_message=str(last_bot_message or "").strip(),
            triggering_user_message=str(triggering_user_message or "").strip(),
            conversation_history=[row for row in history_rows if isinstance(row, dict)][-10:],
            active_service=dict(active_service or {}),
            services_allowed_in_phase=[
                row
                for row in (services_allowed_in_phase or [])
                if isinstance(row, dict)
            ][:18],
            knowledge_base_excerpt={
                "full_kb_text": full_kb[:kb_chars],
                "service_knowledge_corpus": service_kb[:kb_chars],
            },
            capabilities_summary=effective_cap_summary,
            memory_snapshot=(
                dict(memory_snapshot)
                if isinstance(memory_snapshot, dict)
                else {}
            ),
            selected_phase_context=(
                dict(selected_phase_context)
                if isinstance(selected_phase_context, dict)
                else {}
            ),
            pending_action=str(pending_action or "").strip(),
            pending_data_public=(
                llm_orchestration_service._coerce_public_pending(pending_data_public)
                if isinstance(pending_data_public, dict)
                else {}
            ),
            booking_ledger=[
                dict(row)
                for row in (booking_ledger or [])
                if isinstance(row, dict)
            ],
            turn_revision=turn_revision,
            response_language=dict(response_language or {}),
            known_context=known_context_map,
            expires_at=_utc_now() + timedelta(seconds=ttl_seconds),
            entries=entries,
        )
        batch.config_fingerprint = self._build_config_fingerprint(
            hotel_code=batch.hotel_code,
            capabilities_summary=effective_cap_summary,
        )
        batch.context_fingerprint = self._build_context_fingerprint(batch=batch)

        async with self._lock:
            await self._cleanup_expired()
            self._batches[batch_id] = batch
            session_key = batch.session_id
            bucket = self._session_batches.setdefault(session_key, [])
            bucket.append(batch_id)
            if len(bucket) > max_batches:
                old_ids = list(bucket[:-max_batches])
                for old_id in old_ids:
                    old_batch = self._batches.pop(old_id, None)
                    if old_batch is None:
                        continue
                    if old_id in bucket:
                        bucket.remove(old_id)
            if not bucket:
                self._session_batches.pop(session_key, None)

        observability_service.log_event(
            "suggestion_prefetch_scheduled",
            {
                "session_id": batch.session_id,
                "batch_id": batch_id,
                "count": len(entries),
                "assistant_turn_id": batch.assistant_turn_id,
            },
        )

        for entry_id in entries.keys():
            task_key = self._entry_task_key(batch_id=batch_id, entry_id=entry_id)
            task = asyncio.create_task(self._generate_answer_for_entry(batch_id, entry_id))
            task.add_done_callback(lambda _t, _key=task_key: self._entry_tasks.pop(_key, None))
            async with self._lock:
                current_batch = self._batches.get(batch_id)
                if current_batch is not None:
                    current_entry = current_batch.entries.get(entry_id)
                    if current_entry is not None:
                        current_entry.task_key = task_key
                self._entry_tasks[task_key] = task

        return batch_id

    async def cancel_session_prefetch(
        self,
        *,
        session_id: str,
        reason: str = "",
    ) -> int:
        session_key = str(session_id or "").strip()
        if not session_key:
            return 0
        tasks_to_cancel: list[asyncio.Task[Any]] = []
        cancelled_count = 0
        async with self._lock:
            await self._cleanup_expired()
            batch_ids = list(self._session_batches.pop(session_key, []))
            for batch_id in batch_ids:
                batch = self._batches.pop(batch_id, None)
                if batch is None:
                    continue
                for entry_id in list(batch.entries.keys()):
                    task_key = self._entry_task_key(batch_id=batch_id, entry_id=entry_id)
                    task = self._entry_tasks.pop(task_key, None)
                    if task is not None and not task.done():
                        tasks_to_cancel.append(task)
                    entry = batch.entries.get(entry_id)
                    if entry is not None and entry.status in {"queued", "running"}:
                        entry.status = "failed"
                        entry.error = "cancelled_session_reset"
                        entry.updated_at = _utc_now()
        for task in tasks_to_cancel:
            try:
                task.cancel()
                cancelled_count += 1
            except Exception:
                pass
        if cancelled_count > 0:
            observability_service.log_event(
                "suggestion_prefetch_cancelled",
                {
                    "session_id": session_key,
                    "cancelled_tasks": cancelled_count,
                    "reason": str(reason or "").strip(),
                },
            )
        return cancelled_count

    async def find_existing_batch_for_turn(
        self,
        *,
        session_id: str,
        assistant_turn_id: str,
    ) -> str:
        session_key = str(session_id or "").strip()
        turn_id = str(assistant_turn_id or "").strip()
        if not session_key or not turn_id:
            return ""
        async with self._lock:
            await self._cleanup_expired()
            batch_ids = list(self._session_batches.get(session_key, []))
            for batch_id in reversed(batch_ids):
                batch = self._batches.get(batch_id)
                if batch is None:
                    continue
                if str(batch.assistant_turn_id or "").strip() == turn_id:
                    return str(batch.batch_id or "").strip()
        return ""

    async def _try_resolve_exact_chip_priority(
        self,
        *,
        session_id: str,
        user_message: str,
        batch_id: str,
        conversation_id: str = "",
    ) -> Optional[dict[str, Any]]:
        session_key = str(session_id or "").strip()
        preferred_batch_id = str(batch_id or "").strip()
        normalized_query = _normalize_text(user_message)
        if not session_key or not preferred_batch_id or not normalized_query:
            return None

        target_batch_id = ""
        target_entry_id = ""
        target_task: Optional[asyncio.Task[Any]] = None
        tasks_to_cancel: list[asyncio.Task[Any]] = []
        threshold = max(
            0.5,
            min(
                0.99,
                _coerce_float(
                    getattr(settings, "chat_suggestion_prefetch_similarity_threshold", 0.90),
                    0.90,
                ),
            ),
        )

        async with self._lock:
            await self._cleanup_expired()
            session_batch_ids = self._batch_ids_for_conversation(
                list(self._session_batches.get(session_key, [])), conversation_id
            )
            if not session_batch_ids:
                return None
            ordered_ids = [preferred_batch_id] + [bid for bid in session_batch_ids if bid != preferred_batch_id]

            target_batch: Optional[PrefetchBatch] = None
            target_entry: Optional[PrefetchEntry] = None
            for candidate_batch_id in ordered_ids:
                batch = self._batches.get(candidate_batch_id)
                if batch is None:
                    continue
                for entry in batch.entries.values():
                    if entry.consumed:
                        continue
                    if _normalize_text(entry.chip_text) == normalized_query:
                        target_batch = batch
                        target_entry = entry
                        break
                if target_entry is not None:
                    break

            if target_batch is None or target_entry is None:
                return None

            if target_entry.status == "ready" and target_entry.answer_payload:
                payload = self._consume_ready_entry_payload(
                    batch=target_batch,
                    entry=target_entry,
                    similarity=1.0,
                    threshold=threshold,
                    match_mode="exact",
                )
                return payload

            target_batch_id = target_batch.batch_id
            target_entry_id = target_entry.entry_id
            target_task_key = (
                target_entry.task_key
                or self._entry_task_key(batch_id=target_batch_id, entry_id=target_entry_id)
            )
            target_task = self._entry_tasks.get(target_task_key)

            for candidate_batch_id in session_batch_ids:
                batch = self._batches.get(candidate_batch_id)
                if batch is None:
                    continue
                for entry in batch.entries.values():
                    entry_task_key = entry.task_key or self._entry_task_key(
                        batch_id=batch.batch_id,
                        entry_id=entry.entry_id,
                    )
                    task = self._entry_tasks.get(entry_task_key)
                    if batch.batch_id == target_batch_id and entry.entry_id == target_entry_id:
                        continue
                    if task is not None and not task.done():
                        tasks_to_cancel.append(task)
                    if entry.status in {"queued", "running"} and not entry.consumed:
                        entry.status = "failed"
                        entry.error = "cancelled_due_to_chip_click_priority"
                        entry.updated_at = _utc_now()

        for task in tasks_to_cancel:
            try:
                task.cancel()
            except Exception:
                pass

        if target_task is not None and not target_task.done():
            configured_click_wait_ms = int(
                getattr(settings, "chat_chip_speculation_click_wait_ms", 8000) or 8000
            )
            # The clicked chip's answer is already being cooked. Waiting for that
            # in-flight task to FINISH is faster than cancelling it and re-running
            # the full pipeline from scratch (a fresh run has no head start). So
            # we wait up to the configured cap (default 8s) instead of a tiny
            # 900ms window, and only fall back to the live chat flow if the cook
            # genuinely overruns the cap. Hard ceiling guards against a stuck task.
            click_wait_ms = max(120, min(20000, configured_click_wait_ms))
            try:
                await asyncio.wait_for(
                    asyncio.shield(target_task),
                    timeout=float(click_wait_ms) / 1000.0,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Task didn't finish in time — fall through and check entry status
                # one final time; it may have completed during the wait window.
                pass
            except Exception:
                return None

        async with self._lock:
            batch = self._batches.get(target_batch_id)
            if batch is None:
                return None
            entry = batch.entries.get(target_entry_id)
            if entry is None or entry.consumed:
                return None
            if entry.status != "ready" or not entry.answer_payload:
                return None
            payload = self._consume_ready_entry_payload(
                batch=batch,
                entry=entry,
                similarity=1.0,
                threshold=threshold,
                match_mode="exact",
            )
            return payload

    def _batch_ids_for_conversation(
        self, session_batch_ids: list[str], conversation_id: str
    ) -> list[str]:
        """Restrict a session's batch ids to the CURRENT conversation.

        Chip batches are stored per session, but a session outlives a
        conversation (dustbin 'new chat', a fresh location pick, etc.). Without
        this filter, a click in a NEW conversation can match a LEFTOVER batch
        from an earlier conversation/location in the same browser and re-pin the
        wrong property. When a conversation_id is known we keep ONLY batches
        tagged with it; an untagged/foreign batch is dropped so the click falls
        back to the live pipeline with the correct in-context scope — never a
        cross-conversation hit. Must be called while holding _lock.
        """
        conv = str(conversation_id or "").strip()
        if not conv:
            return list(session_batch_ids)
        kept: list[str] = []
        for bid in session_batch_ids:
            batch = self._batches.get(bid)
            if batch is None:
                continue
            if str(getattr(batch, "conversation_id", "") or "").strip() == conv:
                kept.append(bid)
        return kept

    async def resolve_chip_click_context(
        self,
        *,
        session_id: str,
        user_message: str,
        batch_id: str,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        session_key = str(session_id or "").strip()
        preferred_batch_id = str(batch_id or "").strip()
        normalized_query = _normalize_text(user_message)
        if not session_key or not preferred_batch_id or not normalized_query:
            return {}
        async with self._lock:
            await self._cleanup_expired()
            session_batch_ids = self._batch_ids_for_conversation(
                list(self._session_batches.get(session_key, [])), conversation_id
            )
            if not session_batch_ids:
                return {}
            ordered_ids = [preferred_batch_id] + [bid for bid in session_batch_ids if bid != preferred_batch_id]
            for candidate_batch_id in ordered_ids:
                batch = self._batches.get(candidate_batch_id)
                if batch is None:
                    continue
                for entry in batch.entries.values():
                    if _normalize_text(entry.chip_text) != normalized_query:
                        continue
                    return dict(entry.chip_data or {})
        return {}

    async def resolve_cached_answer(
        self,
        *,
        session_id: str,
        user_message: str,
        source_type: str = "",
        batch_id: str = "",
        conversation_id: str = "",
        similarity_threshold: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        if not bool(getattr(settings, "chat_suggestion_prefetch_enabled", True)):
            return None
        if not str(settings.openai_api_key or "").strip():
            return None

        normalized_source = str(source_type or "").strip().lower()
        normalized_query = _normalize_text(user_message)
        if not normalized_query:
            return None
        preferred_batch_id = str(batch_id or "").strip()

        # Click-priority path: if the user clicked a suggestion chip and its
        # exact prefetch answer is still running, cancel sibling chip tasks and
        # wait for that exact chip to finish first.
        if normalized_source == "suggested_action" and preferred_batch_id:
            prioritized_payload = await self._try_resolve_exact_chip_priority(
                session_id=session_id,
                user_message=user_message,
                batch_id=preferred_batch_id,
                conversation_id=conversation_id,
            )
            if isinstance(prioritized_payload, dict) and prioritized_payload:
                observability_service.log_event(
                    "suggestion_prefetch_priority_hit",
                    {
                        "session_id": str(session_id or "").strip(),
                        "batch_id": preferred_batch_id,
                    },
                )
                return prioritized_payload

        threshold = (
            _coerce_float(similarity_threshold, 0.9)
            if similarity_threshold is not None
            else _coerce_float(
                getattr(settings, "chat_suggestion_prefetch_similarity_threshold", 0.90),
                0.90,
            )
        )
        threshold = max(0.5, min(0.99, threshold))
        latest_context_fingerprint = ""

        async with self._lock:
            await self._cleanup_expired()
            session_key = str(session_id or "").strip()
            if not session_key:
                return None
            session_batch_ids = self._batch_ids_for_conversation(
                list(self._session_batches.get(session_key, [])), conversation_id
            )
            if not session_batch_ids:
                return None
            latest_batch = self._batches.get(session_batch_ids[-1])
            latest_context_fingerprint = (
                str(latest_batch.context_fingerprint or "").strip()
                if latest_batch is not None
                else ""
            )
            candidates = self._collect_ready_candidates(
                session_batch_ids=session_batch_ids,
                preferred_batch_id=preferred_batch_id,
            )

        if not candidates:
            wait_ms = max(0, int(getattr(settings, "chat_suggestion_prefetch_wait_ms", 220) or 220))
            if wait_ms <= 0 or not str(batch_id or "").strip():
                return None
            await asyncio.sleep(wait_ms / 1000.0)
            async with self._lock:
                await self._cleanup_expired()
                session_key = str(session_id or "").strip()
                session_batch_ids = self._batch_ids_for_conversation(
                list(self._session_batches.get(session_key, [])), conversation_id
            )
                if not session_batch_ids:
                    return None
                latest_batch = self._batches.get(session_batch_ids[-1])
                latest_context_fingerprint = (
                    str(latest_batch.context_fingerprint or "").strip()
                    if latest_batch is not None
                    else ""
                )
                candidates = self._collect_ready_candidates(
                    session_batch_ids=session_batch_ids,
                    preferred_batch_id=str(batch_id or "").strip(),
                )
            if not candidates:
                return None

        enforce_latest_context = not (
            normalized_source == "suggested_action" and bool(preferred_batch_id)
        )
        if latest_context_fingerprint and enforce_latest_context:
            context_filtered = [
                (candidate_batch, candidate_entry)
                for candidate_batch, candidate_entry in candidates
                if str(candidate_batch.context_fingerprint or "").strip() == latest_context_fingerprint
            ]
            if context_filtered:
                candidates = context_filtered
            else:
                observability_service.log_event(
                    "suggestion_prefetch_context_stale",
                    {
                        "session_id": str(session_id or "").strip(),
                        "source_type": normalized_source,
                        "preferred_batch_id": str(batch_id or "").strip(),
                    },
                )
                return None

        exact_best: Optional[tuple[PrefetchBatch, PrefetchEntry]] = None
        for batch, entry in candidates:
            if _normalize_text(entry.chip_text) == normalized_query:
                exact_best = (batch, entry)
                break

        best_batch: Optional[PrefetchBatch] = None
        best_entry: Optional[PrefetchEntry] = None
        best_similarity = 0.0

        if exact_best is not None:
            best_batch, best_entry = exact_best
            best_similarity = 1.0
        else:
            query_embedding = await self._embed_text(user_message)
            if not query_embedding:
                return None
            for batch, entry in candidates:
                if not entry.embedding:
                    continue
                score = self._cosine_similarity(query_embedding, entry.embedding)
                if score > best_similarity:
                    best_similarity = score
                    best_batch = batch
                    best_entry = entry

        if best_batch is None or best_entry is None:
            return None

        if best_similarity < threshold:
            observability_service.log_event(
                "suggestion_prefetch_miss",
                {
                    "session_id": best_batch.session_id,
                    "batch_id": best_batch.batch_id,
                    "entry_id": best_entry.entry_id,
                    "similarity": round(best_similarity, 4),
                    "threshold": threshold,
                    "source_type": normalized_source,
                },
            )
            return None

        expected_config_fingerprint = str(best_batch.config_fingerprint or "").strip()
        current_config_fingerprint = ""
        if expected_config_fingerprint:
            try:
                current_cap_summary = config_service.get_capability_summary(best_batch.hotel_code)
                current_config_fingerprint = self._build_config_fingerprint(
                    hotel_code=best_batch.hotel_code,
                    capabilities_summary=current_cap_summary,
                )
            except Exception:
                current_config_fingerprint = ""
            if current_config_fingerprint and current_config_fingerprint != expected_config_fingerprint:
                observability_service.log_event(
                    "suggestion_prefetch_config_stale",
                    {
                        "session_id": best_batch.session_id,
                        "batch_id": best_batch.batch_id,
                        "entry_id": best_entry.entry_id,
                        "source_type": normalized_source,
                    },
                )
                return None

        async with self._lock:
            latest_batch = self._batches.get(best_batch.batch_id)
            if latest_batch is None:
                return None
            if latest_context_fingerprint and str(latest_batch.context_fingerprint or "").strip() != latest_context_fingerprint:
                return None
            if (
                expected_config_fingerprint
                and current_config_fingerprint
                and str(latest_batch.config_fingerprint or "").strip() != current_config_fingerprint
            ):
                return None
            latest_entry = latest_batch.entries.get(best_entry.entry_id)
            if latest_entry is None or latest_entry.consumed:
                return None
            payload = self._consume_ready_entry_payload(
                batch=latest_batch,
                entry=latest_entry,
                similarity=best_similarity,
                threshold=threshold,
                match_mode="exact" if best_similarity >= 0.999 else "semantic",
            )

        observability_service.log_event(
            "suggestion_prefetch_hit",
            {
                "session_id": best_batch.session_id,
                "batch_id": best_batch.batch_id,
                "entry_id": best_entry.entry_id,
                "similarity": round(best_similarity, 4),
                "source_type": normalized_source,
            },
        )
        return payload


suggestion_prefetch_service = SuggestionPrefetchService()
