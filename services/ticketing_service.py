"""
Ticketing Service

Lumira-compatible ticketing integration:
- Create ticket: POST  {TICKETING_BASE_URL}/insert/ticket.htm
- Update ticket: PATCH {TICKETING_BASE_URL}/insert/ticket/{id}.htm
- Agent handoff: POST  {AGENT_HANDOFF_API_URL}
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Iterable

from core.text_utils import normalize_identifier

import httpx

from config.settings import settings
from schemas.chat import ConversationContext
from services.config_service import config_service

logger = logging.getLogger(__name__)

# Indian Standard Time (UTC+05:30). The product serves India-only hotels and the
# downstream Kepsla/KePSLA ticketing dashboard renders ticket timestamps as
# plain wall-clock strings (no timezone marker), so the ticket's `created_at`
# must be stamped in IST — not UTC — or the dashboard shows the time 5h30m
# behind (e.g. a ticket raised at 10:15 IST appears as 04:45). India observes no
# DST, so a fixed offset is exact.
IST = timezone(timedelta(hours=5, minutes=30))


def _normalize_key(value: Any) -> str:
    """Lowercase alphanumeric form of a label, for tolerant name matching.

    Property names reach us with varying punctuation and spacing ("Zone by The
    Park, Kolkata (New Town)" vs "Zone by the Park Kolkata New Town"), so
    comparisons collapse everything but letters and digits.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


@dataclass
class TicketingResult:
    """Normalized result returned by all ticketing operations."""

    success: bool
    ticket_id: str = ""
    assigned_id: str = ""
    status_code: int | None = None
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    # Did the request reach STAFF, even though the external API refused it?
    # The KePSLA row is written whether or not Lumira answers, so an external
    # outage still leaves the team a real ticket on their dashboard - and a guest
    # was being told "nothing has been sent" about a request staff could already
    # see. `success` stays exactly as it was (it means the EXTERNAL write); this
    # is the separate question of whether the hotel knows.
    saved_to_dashboard: bool = False


class TicketingService:
    """Async client for external ticketing + handoff APIs."""

    _DUPLICATE_CREATE_WINDOW_SECONDS = 120

    _LOCAL_TICKET_CSV_HEADERS: tuple[str, ...] = (
        "id",
        "ticket_id",
        "session_id",
        "guest_id",
        "entity_id",
        "fms_entity_id",
        "issue",
        "message_id",
        "sentiment_score",
        "department_alloc",
        "priority",
        "category",
        "phase",
        "status",
        "sla_due",
        "escalation_stage",
        "created_at",
        "updated_at",
        "room_number",
        "message",
        "customer_feedback",
        "manager_notes",
        "assignee_notes",
        "assigned_to",
        "assigned_id",
        "customer_rating",
        "department_id",
        "sla_due_at",
        "sla_duration_minutes",
        "ticket_auto_assign",
        "closed_at",
        "outlet_id",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost",
        "sub_category",
        "guest_name",
        "property_name",
        "room_type",
        "compensation_type",
        "compensation_currency",
        "compensation_amount",
        "group_id",
        "ticket_source",
        "cancelled_notes",
        "mode",
        "local_created_at_utc",
        "local_updated_at_utc",
        "payload_json",
        "response_json",
    )

    def __init__(self) -> None:
        self._recent_create_cache: dict[str, dict[str, Any]] = {}
        # Sessions known to have >=1 persisted ticket. Lets the per-turn
        # cancel/edit interceptor skip a prod-DB `list_session_tickets` round-trip
        # for the ~all sessions that never ticket (that query, over VPN + under
        # pool contention, was costing ~3-5s on EVERY turn — see the edit-checker
        # in llm_orchestration_service). Populated at the single DB persistence
        # point below. Fail-open: a miss just means the interceptor no-ops, and
        # the turn falls through to the normal flow.
        self._sessions_with_tickets: set[str] = set()
        # Refs the guest has already CANCELLED this session, keyed by session.
        # A cancelled ticket is terminal — it must never be offered to the
        # cancel/edit interceptor again, otherwise the classifier can keep
        # re-matching later unrelated turns ("what's the weather", "thanks") to
        # the same cancel and re-short-circuit them. Cleared on restart (safe).
        self._session_cancelled_refs: dict[str, set[str]] = {}
        # Synthetic `prefetch::` sessions whose pre-cooked turn tried to raise a
        # ticket and was suppressed below. The prefetch service reads this to
        # throw that cache entry away, so the guest's real click runs the live
        # pipeline and files ONE real ticket on their own session. Without it,
        # suppressing the pre-cook would silently lose the ticket entirely,
        # because a cache hit replays text only and never re-runs ticketing.
        self._prefetch_suppressed_sessions: set[str] = set()

    @staticmethod
    def prefetch_ticket_guard_enabled(hotel_code: Any) -> bool:
        """Is the speculative-ticket guard opted IN for this hotel?

        Fails CLOSED (returns False) on an empty or unparseable list and on a
        missing hotel_code: not knowing the tenant must leave that tenant's
        behaviour exactly as it is today, never silently change it.
        """
        from services import prebooking_settings_service

        if prebooking_settings_service.is_admin_managed(hotel_code):
            # Pre-cooked chip answers never file tickets for admin-managed hotels.
            return True
        try:
            hotels = json.loads(settings.chat_prefetch_ticket_guard_hotels_json or "[]")
        except (TypeError, ValueError):
            return False
        if not isinstance(hotels, list):
            return False
        code = str(hotel_code or "").strip().casefold()
        if not code:
            return False
        return code in {str(value).strip().casefold() for value in hotels}

    def consume_prefetch_suppression(self, session_id: Any) -> bool:
        """True if this pre-cook session had a ticket suppressed (pops the mark)."""
        key = str(session_id or "").strip()
        if not key:
            return False
        try:
            self._prefetch_suppressed_sessions.remove(key)
        except KeyError:
            return False
        return True

    def session_has_known_tickets(self, session_id: Any) -> bool:
        """True if this session has persisted >=1 ticket THIS server run.

        Cheap in-memory check used to gate the per-turn cancel/edit DB query.
        A restart clears the set, so this can false-negative for a session that
        ticketed before the restart — which is safe (the interceptor no-ops and
        the normal flow handles the message)."""
        return str(session_id or "").strip() in self._sessions_with_tickets

    def mark_ticket_cancelled(self, session_id: Any, ticket_ref: Any) -> None:
        """Record that `ticket_ref` was cancelled this session — a terminal state.
        The cancel/edit interceptor filters these out so an already-cancelled
        ticket is never re-cancelled by a later, unrelated message."""
        sid = str(session_id or "").strip()
        ref = str(ticket_ref or "").strip()
        if sid and ref:
            self._session_cancelled_refs.setdefault(sid, set()).add(ref)

    def session_cancelled_refs(self, session_id: Any) -> set[str]:
        """Refs already cancelled this session (empty set when none)."""
        return set(self._session_cancelled_refs.get(str(session_id or "").strip(), set()))

    @staticmethod
    def _ticket_debug_enabled() -> bool:
        return bool(getattr(settings, "ticketing_debug_log_enabled", True))

    @staticmethod
    def _ticket_debug_path() -> Path:
        raw = str(getattr(settings, "ticketing_debug_log_file", "") or "").strip()
        if not raw:
            raw = "./logs/ticketing_debug.jsonl"
        return Path(raw)

    @classmethod
    def _sanitize_debug_value(cls, value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return str(value)[:400]
        if value is None:
            return None
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:1800]
        if isinstance(value, list):
            return [cls._sanitize_debug_value(item, depth + 1) for item in value[:60]]
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for raw_key, raw_value in list(value.items())[:100]:
                key = str(raw_key)[:100]
                normalized[key] = cls._sanitize_debug_value(raw_value, depth + 1)
            return normalized
        return str(value)[:1800]

    @classmethod
    def _write_ticket_debug_event(cls, event: str, **data: Any) -> None:
        if not cls._ticket_debug_enabled():
            return
        path = cls._ticket_debug_path()
        payload_obj = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        result_response = (
            data.get("result_response")
            if isinstance(data.get("result_response"), dict)
            else data.get("response")
            if isinstance(data.get("response"), dict)
            else {}
        )
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": str(event or "").strip() or "unknown_event",
            "stage": str(data.get("stage") or event or "").strip() or None,
        }
        for key, value in data.items():
            payload[str(key)] = cls._sanitize_debug_value(value)
        normalized_phase = cls._normalize_phase_identifier(payload_obj.get("phase"))
        top_level = {
            "session_id": cls._safe_str(data.get("session_id") or payload_obj.get("session_id")),
            "hotel_code": cls._safe_str(
                data.get("hotel_code")
                or payload_obj.get("hotel_code")
                or payload_obj.get("tenant_id")
            ),
            "service_id": cls._safe_str(data.get("service_id") or payload_obj.get("service_id")),
            "phase": normalized_phase,
            "ticket_id": cls._safe_str(
                data.get("ticket_id")
                or data.get("ticket_ref")
                or result_response.get("ticket_id")
                or result_response.get("id")
            ),
            "status": cls._safe_str(
                data.get("status")
                or payload_obj.get("ticket_status")
                or result_response.get("status")
            ),
        }
        for key, value in top_level.items():
            if value:
                payload[key] = value
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")
        except Exception:
            logger.exception("Failed writing ticket debug log event=%s path=%s", event, path)

        try:
            from services.db_log_sink import db_log_sink
            db_log_sink.emit("ticketing", payload)
        except Exception:
            pass

    @staticmethod
    def _ticket_duplicate_window_seconds() -> int:
        raw = getattr(settings, "ticketing_duplicate_window_seconds", 120)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = TicketingService._DUPLICATE_CREATE_WINDOW_SECONDS
        return max(0, value)

    def _cleanup_recent_create_cache(self) -> None:
        if not self._recent_create_cache:
            return
        window_seconds = self._ticket_duplicate_window_seconds()
        if window_seconds <= 0:
            self._recent_create_cache.clear()
            return
        now_ts = datetime.now(UTC).timestamp()
        stale_keys = [
            key
            for key, record in self._recent_create_cache.items()
            if (now_ts - float(record.get("created_ts") or 0.0)) > window_seconds
        ]
        for key in stale_keys:
            self._recent_create_cache.pop(key, None)

    @classmethod
    def _build_create_dedupe_fingerprint(cls, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""
        normalized = {
            "session_id": cls._safe_str(payload.get("session_id")).lower(),
            "guest_id": cls._safe_str(payload.get("guest_id")).lower(),
            "entity_id": cls._safe_str(
                payload.get("entity_id")
                or payload.get("organisation_id")
                or payload.get("organization_id")
                or payload.get("org_id")
            ).lower(),
            "room_number": cls._safe_str(payload.get("room_number")).lower(),
            "issue": cls._safe_str(payload.get("issue")).lower(),
            "phase": cls._safe_str(payload.get("phase")).lower(),
            "category": cls._safe_str(payload.get("categorization") or payload.get("category")).lower(),
            "sub_category": cls._safe_str(
                payload.get("sub_categorization") or payload.get("sub_category")
            ).lower(),
            "service_id": cls._safe_str(payload.get("service_id") or payload.get("service")).lower(),
        }
        if not normalized["issue"]:
            return ""
        serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(serialized.encode("utf-8")).hexdigest()

    def _find_recent_duplicate_result(self, payload: dict[str, Any]) -> TicketingResult | None:
        self._cleanup_recent_create_cache()
        fingerprint = self._build_create_dedupe_fingerprint(payload)
        if not fingerprint:
            return None
        cached = self._recent_create_cache.get(fingerprint)
        if not isinstance(cached, dict):
            return None
        response = dict(cached.get("response") or {})
        response["duplicate_suppressed"] = True
        return TicketingResult(
            success=True,
            ticket_id=self._safe_str(cached.get("ticket_id")),
            assigned_id=self._safe_str(cached.get("assigned_id")),
            status_code=int(cached.get("status_code") or 200),
            payload=dict(payload or {}),
            response=response,
        )

    def _remember_recent_create_result(self, payload: dict[str, Any], result: TicketingResult) -> None:
        if not isinstance(payload, dict):
            return
        if not bool(result and result.success):
            return
        ticket_id = self._safe_str(result.ticket_id)
        if not ticket_id:
            return
        fingerprint = self._build_create_dedupe_fingerprint(payload)
        if not fingerprint:
            return
        self._recent_create_cache[fingerprint] = {
            "created_ts": datetime.now(UTC).timestamp(),
            "ticket_id": ticket_id,
            "assigned_id": self._safe_str(result.assigned_id),
            "status_code": int(result.status_code or 200),
            "response": dict(result.response or {}),
        }

    def is_ticketing_enabled(self, capabilities: dict[str, Any] | None = None) -> bool:
        """Ticketing is enabled only when endpoint exists and tool toggle allows it."""
        if not bool(getattr(settings, "ticketing_plugin_enabled", True)):
            return False
        if self._use_local_mode():
            return True
        if not self._ticketing_base_url():
            return False

        tools: Iterable[dict[str, Any]] = []
        if isinstance(capabilities, dict):
            tools = capabilities.get("tools", []) or []
        if not tools:
            tools = config_service.get_tools()

        ticket_tool: dict[str, Any] | None = None
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = self._normalize_identifier(tool.get("id"))
            if tool_id in {"ticketing", "ticket_create"}:
                ticket_tool = tool
                break

        # If no explicit tool row exists, keep endpoint-driven enablement.
        if ticket_tool is None:
            return True
        return bool(ticket_tool.get("enabled", False))

    def is_handoff_enabled(self, capabilities: dict[str, Any] | None = None) -> bool:
        """Handoff requires AGENT_HANDOFF_API_URL and enabled handoff tool/capability."""
        has_handoff_url = bool(
            str(settings.agent_handoff_api_url or "").strip()
            or str(getattr(settings, "agent_handoff_api_url_gj", "") or "").strip()
        )
        if not has_handoff_url:
            return False

        if not config_service.is_capability_enabled("human_escalation"):
            return False

        tools: Iterable[dict[str, Any]] = []
        if isinstance(capabilities, dict):
            tools = capabilities.get("tools", []) or []
        if not tools:
            tools = config_service.get_tools()

        handoff_tool_enabled_values: list[bool] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = self._normalize_identifier(tool.get("id"))
            if tool_id in {"human_handoff", "live_chat", "callback", "email_followup"}:
                handoff_tool_enabled_values.append(bool(tool.get("enabled", False)))

        # If no explicit tool row exists, allow handoff by capability + endpoint.
        if not handoff_tool_enabled_values:
            return True
        return any(handoff_tool_enabled_values)

    async def create_ticket(self, payload: dict[str, Any]) -> TicketingResult:
        """Create a ticket using Lumira-compatible payload contract."""
        # WHICH code path raised this? Tickets are created from a dozen call
        # sites, and a guard wired into the wrong one looks identical to a guard
        # that does not work - two rounds were lost to exactly that. One line
        # here answers it for every future change.
        try:
            import traceback as _tb
            _callers = [
                "%s:%s" % (f.filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                           f.lineno)
                for f in _tb.extract_stack()[:-1]
                if "site-packages" not in f.filename
            ][-4:]
            logger.info("[TICKET] create_ticket called from %s", " <- ".join(reversed(_callers)))
        except Exception:
            pass
        debug_context = {
            "operation": "create_ticket",
            "mode": "local" if self._use_local_mode() else "api",
            "ticketing_base_url": self._ticketing_base_url(),
            "ticketing_create_path": str(settings.ticketing_create_path or "/insert/ticket.htm").strip(),
            "local_store_path": str(self._local_store_path()),
            "local_csv_path": str(self._local_csv_path()),
        }
        self._write_ticket_debug_event(
            "ticket_create_requested",
            payload=payload,
            context=debug_context,
        )
        payload_effective = dict(payload or {})
        # Ticket priority set per service in the admin (Bot Training ->
        # Pre-booking -> Service buttons). Never downgrades a CRITICAL ticket.
        try:
            from services import prebooking_settings_service as _pbs

            _admin_priority = _pbs.service_priority_for(
                payload_effective.get("_hotel_code") or payload_effective.get("hotel_code"),
                payload_effective.get("service_id"),
                payload_effective.get("service_name"),
            )
            if _admin_priority and str(payload_effective.get("priority") or "").upper() != "CRITICAL":
                payload_effective["priority"] = _admin_priority
        except Exception:
            logger.debug("admin ticket priority lookup skipped", exc_info=True)
        # SPECULATIVE PRE-COOK — never file a real ticket.
        # The suggestion-chip prefetch service replays every chip the bot wrote
        # through this same pipeline in the background, on a synthetic
        # `prefetch::<batch>::<uuid>` session, so that a click can be served from
        # cache. That replay reached here and filed REAL external tickets for
        # chips no guest ever clicked ("Help me pick one" -> "Guest wants to book
        # a room"): 11 on Mango in four days. It already set
        # metadata["prefetch_generation"]=True but nothing ever read it, so the
        # guard keys off the session id, which only a synthetic session can
        # carry, and it lives in create_ticket because every one of the dozen
        # call sites funnels through here. The caller is told via the error so it
        # can drop the cache entry and let the real click raise the real ticket.
        # Opted-in tenants only (CHAT_PREFETCH_TICKET_GUARD_HOTELS_JSON): a hotel
        # that is not listed keeps its current behaviour untouched, and so does
        # the prefetch cache entry, which is the other half of this gate.
        _speculative_session = str(payload_effective.get("session_id") or "").strip()
        if _speculative_session.startswith("prefetch::") and self.prefetch_ticket_guard_enabled(
            payload_effective.get("hotel_code")
        ):
            if len(self._prefetch_suppressed_sessions) > 512:
                self._prefetch_suppressed_sessions.clear()
            self._prefetch_suppressed_sessions.add(_speculative_session)
            self._write_ticket_debug_event(
                "ticket_create_skipped_prefetch_speculative",
                session_id=_speculative_session,
                payload=payload_effective,
                context=debug_context,
            )
            logger.info(
                "Ticket SUPPRESSED: speculative suggestion-chip pre-cook, no guest "
                "asked for this; session_id=%s issue=%r",
                _speculative_session,
                str(payload_effective.get("issue") or "")[:120],
            )
            return TicketingResult(
                success=False,
                status_code=409,
                error="prefetch_speculative_ticket_suppressed",
                payload=dict(payload_effective or {}),
                response={"skip_reason": "prefetch_speculative_ticket_suppressed"},
            )
        # Unmapped-location guard (flag set by build_lumira_ticket_payload):
        # the resolved entity isn't a configured property/FMS entity for this
        # tenant (e.g. a Zone location whose FMS id we don't have yet). Pushing
        # it to the external API would mis-route to the wrong company, so we
        # create the ticket LOCAL-ONLY (visible on the KePSLA page) and skip the
        # external API entirely — it can be synced once the FMS id is provided.
        # This replaces the old hard "block" so the guest always gets a ticket.
        # Legacy _block_wrong_tenant payloads are routed the same way.
        force_local = bool(payload_effective.pop("_force_local_only", False)) or bool(
            payload_effective.pop("_block_wrong_tenant", False)
        )
        if force_local:
            self._write_ticket_debug_event(
                "ticket_create_local_only_unmapped_entity",
                organisation_id=payload_effective.get("organisation_id"),
                session_id=payload_effective.get("session_id"),
                context=debug_context,
            )
            logger.info(
                "Ticket routed LOCAL-ONLY: entity %s is not a configured FMS entity for "
                "this tenant — creating KePSLA ticket, skipping external API; session_id=%s",
                payload_effective.get("organisation_id"),
                payload_effective.get("session_id"),
            )
        if not self._use_local_mode() and not force_local:
            payload_effective = await self._apply_department_mapping(payload_effective)
        self._write_ticket_debug_event(
            "ticket_create_payload_resolved",
            payload=payload_effective,
            context=debug_context,
        )

        gate = self.detect_service_ticketing_disabled(payload_effective)
        if gate is not None:
            response_payload = {
                "skip_reason": "phase_service_ticketing_disabled",
                **gate,
            }
            self._write_ticket_debug_event(
                "ticket_create_blocked",
                reason="phase_service_ticketing_disabled",
                gate=gate,
                payload=payload_effective,
                context=debug_context,
            )
            return TicketingResult(
                success=False,
                status_code=409,
                error="phase_service_ticketing_disabled",
                payload=dict(payload_effective or {}),
                response=response_payload,
            )

        duplicate_result = self._find_recent_duplicate_result(payload_effective)
        if duplicate_result is not None:
            self._write_ticket_debug_event(
                "ticket_create_deduplicated",
                ticket_id=duplicate_result.ticket_id,
                assigned_id=duplicate_result.assigned_id,
                payload=payload_effective,
                context=debug_context,
            )
            return duplicate_result

        # Generate the KePSLA one-line description up-front and use it as the
        # issue we push, so the external ticketing dashboard shows the SAME
        # description as the KePSLA page (not generic boilerplate). Runs after
        # dedup/gate so we never spend the LLM call on a suppressed ticket.
        payload_effective = await self._apply_kepsla_description(payload_effective)

        # Dashboard identity: during_stay tickets capture the room number but not
        # the guest's name. Fill an empty guest_name from the PMS guest list
        # (GHN_FF_GUEST_INFO) by entity + room so the external dashboard shows a
        # name against the ticket. Kebo hotels only; best-effort, never raises.
        payload_effective = await self._enrich_guest_name_from_pms(payload_effective)

        if self._use_local_mode() or force_local:
            result = await self._create_ticket_local(payload_effective)
            logger.info(
                "Ticket create completed in local mode: success=%s ticket_id=%s error=%s",
                result.success,
                result.ticket_id,
                self._safe_str(result.error),
            )
            self._write_ticket_debug_event(
                "ticket_create_local_completed",
                success=result.success,
                ticket_id=result.ticket_id,
                status_code=result.status_code,
                error=result.error,
                payload=payload_effective,
                result_response=result.response,
                context=debug_context,
            )
            self._remember_recent_create_result(payload_effective, result)
            await self._save_ticket_to_db(payload_effective, result)
            return result

        base_url = self._ticketing_base_url()
        if not base_url:
            self._write_ticket_debug_event(
                "ticket_create_failed_missing_base_url",
                error="TICKETING_BASE_URL is not configured",
                payload=payload_effective,
                context=debug_context,
            )
            return TicketingResult(success=False, error="TICKETING_BASE_URL is not configured")

        # Final safety net: never POST a category the external API rejects with
        # id:0 (e.g. a department name like "Reception Front Office"). The
        # department-mapping step that normally coerces this can be skipped (no
        # entity id, or the live department lookup failed), so guard here too.
        _orig_cat = self._safe_str(payload_effective.get("categorization"))
        _safe_cat = self._coerce_ticket_category(_orig_cat)
        if _safe_cat != _orig_cat:
            payload_effective["categorization"] = _safe_cat
            payload_effective["category"] = _safe_cat
            if not self._safe_str(payload_effective.get("sub_categorization")) and _orig_cat:
                payload_effective["sub_categorization"] = _orig_cat
                payload_effective["sub_category"] = _orig_cat
            self._write_ticket_debug_event(
                "ticket_category_coerced_for_api",
                original=_orig_cat,
                coerced=_safe_cat,
                context=debug_context,
            )

        path = str(settings.ticketing_create_path or "/insert/ticket.htm").strip() or "/insert/ticket.htm"
        url = f"{base_url}{path if path.startswith('/') else '/' + path}"
        # Strip internal-only keys (e.g. _kepsla_description) so they never
        # reach the external API; they are still read from payload_effective by
        # _save_ticket_to_db below. The KePSLA summary, when present, becomes
        # the `issue` we push so the external dashboard shows the same one-line
        # description as the KePSLA page (the stored issue stays the original).
        # ── ANCHOR THE TICKET TO THE TURN THAT RAISED IT ────────────────────
        # GHN_OPERATION_TICKETS.message_id is the only column in the whole
        # database that ties a ticket to a chat message, and it is what the
        # dashboard's chat-history page uses to draw the ticket INLINE, in the
        # middle of the conversation. Without it the ticket still reaches the
        # tickets list, but renders nowhere in the thread.
        #
        # What used to travel in this field was the widget's client-side message
        # UUID out of metadata.message_id — a string, into an int(11) column, so
        # it stored as 0/NULL. Resolve the real KEBO_ENGAGE_SESSIONS row id here
        # instead (the guest row is written before the turn is processed, so by
        # the time a ticket is being built it has long since committed).
        await self._resolve_engage_message_id(payload_effective)

        outbound_payload = {
            key: value
            for key, value in payload_effective.items()
            if not str(key).startswith("_")
        }
        kepsla_description = str(payload_effective.get("_kepsla_description") or "").strip()
        if kepsla_description:
            outbound_payload["issue"] = kepsla_description
        result = await self._request_json(
            "POST",
            url,
            outbound_payload,
            debug_context=debug_context,
        )
        # Retry transient gateway errors (their dev API intermittently 502/503/504)
        # a couple of times before giving up to the local fallback, so a brief
        # blip doesn't strand an otherwise-valid ticket as KePSLA-only.
        _retries = 0
        while (
            not result.success
            and result.status_code in (502, 503, 504)
            and _retries < 2
        ):
            _retries += 1
            self._write_ticket_debug_event(
                "ticket_create_api_retry",
                attempt=_retries,
                api_status_code=result.status_code,
                context=debug_context,
            )
            await asyncio.sleep(0.6 * _retries)
            result = await self._request_json(
                "POST",
                url,
                outbound_payload,
                debug_context=debug_context,
            )
        self._write_ticket_debug_event(
            "ticket_create_api_completed",
            success=result.success,
            ticket_id=result.ticket_id,
            assigned_id=result.assigned_id,
            status_code=result.status_code,
            error=result.error,
            payload=payload_effective,
            result_response=result.response,
            context={**debug_context, "request_url": url},
        )

        # If the external API failed (5xx or any non-success), fall back to local mode
        if not result.success:
            fallback_reason = (
                f"HTTP {result.status_code}" if result.status_code is not None
                else "request_failed"
            )
            if not self._allow_api_failure_local_fallback():
                logger.warning(
                    "External ticketing API failed (%s) — returning explicit failure (local fallback disabled)",
                    fallback_reason,
                )
                self._write_ticket_debug_event(
                    "ticket_create_api_fallback_skipped",
                    api_status_code=result.status_code,
                    fallback_reason=fallback_reason,
                    payload=payload_effective,
                    context=debug_context,
                )
                self._remember_recent_create_result(payload_effective, result)
                await self._save_ticket_to_db(payload_effective, result)
                return result
            logger.warning(
                "External ticketing API failed (%s) — falling back to local mode",
                fallback_reason,
            )
            self._write_ticket_debug_event(
                "ticket_create_api_fallback_to_local",
                api_status_code=result.status_code,
                fallback_reason=fallback_reason,
                payload=payload_effective,
                context=debug_context,
            )
            try:
                result = await self._create_ticket_local(payload_effective)
            except Exception as local_exc:
                logger.exception("Local ticket fallback also failed")
                result = TicketingResult(
                    success=False,
                    error=f"Ticket creation failed (API and local fallback): {local_exc}",
                    payload=dict(payload_effective or {}),
                )
            self._write_ticket_debug_event(
                "ticket_create_local_fallback_completed",
                success=result.success,
                ticket_id=result.ticket_id,
                error=result.error,
                payload=payload_effective,
                context=debug_context,
            )

        self._remember_recent_create_result(payload_effective, result)
        await self._save_ticket_to_db(payload_effective, result)
        return result

    async def update_ticket(
        self,
        ticket_id: str,
        manager_notes: str,
        extra_fields: dict[str, Any] | None = None,
    ) -> TicketingResult:
        """Update an existing ticket (manager notes and optional additional fields)."""
        debug_context = {
            "operation": "update_ticket",
            "mode": "local" if self._use_local_mode() else "api",
            "ticketing_base_url": self._ticketing_base_url(),
            "ticketing_update_path_template": str(
                settings.ticketing_update_path_template or "/insert/ticket/{ticket_id}.htm"
            ).strip(),
            "local_store_path": str(self._local_store_path()),
            "local_csv_path": str(self._local_csv_path()),
        }
        tid = str(ticket_id or "").strip()
        notes = str(manager_notes or "").strip()
        if not tid:
            self._write_ticket_debug_event(
                "ticket_update_rejected",
                reason="ticket_id is required",
                ticket_id=ticket_id,
                context=debug_context,
            )
            return TicketingResult(success=False, error="ticket_id is required")
        if not notes:
            self._write_ticket_debug_event(
                "ticket_update_rejected",
                reason="manager_notes is required",
                ticket_id=ticket_id,
                context=debug_context,
            )
            return TicketingResult(success=False, error="manager_notes is required")
        self._write_ticket_debug_event(
            "ticket_update_requested",
            ticket_id=tid,
            manager_notes=notes,
            extra_fields=extra_fields or {},
            context=debug_context,
        )
        if self._use_local_mode():
            result = await self._update_ticket_local(tid, notes, extra_fields=extra_fields)
            logger.info(
                "Ticket update completed in local mode: success=%s ticket_id=%s error=%s",
                result.success,
                result.ticket_id or tid,
                self._safe_str(result.error),
            )
            self._write_ticket_debug_event(
                "ticket_update_local_completed",
                success=result.success,
                ticket_id=result.ticket_id or tid,
                status_code=result.status_code,
                error=result.error,
                result_response=result.response,
                context=debug_context,
            )
            return result
        base_url = self._ticketing_base_url()
        if not base_url:
            self._write_ticket_debug_event(
                "ticket_update_failed_missing_base_url",
                ticket_id=tid,
                error="TICKETING_BASE_URL is not configured",
                context=debug_context,
            )
            return TicketingResult(success=False, error="TICKETING_BASE_URL is not configured")

        path_template = (
            str(settings.ticketing_update_path_template or "/insert/ticket/{ticket_id}.htm").strip()
            or "/insert/ticket/{ticket_id}.htm"
        )
        try:
            path = path_template.format(ticket_id=tid, id=tid)
        except Exception:
            path = f"/insert/ticket/{tid}.htm"
        url = f"{base_url}{path if path.startswith('/') else '/' + path}"

        body: dict[str, Any] = {"manager_notes": notes}
        if isinstance(extra_fields, dict):
            for key, value in extra_fields.items():
                if key not in body and value is not None:
                    body[key] = value

        result = await self._request_json(
            "PATCH",
            url,
            body,
            debug_context={**debug_context, "ticket_id": tid},
        )
        if not result.ticket_id:
            result.ticket_id = tid
        self._write_ticket_debug_event(
            "ticket_update_api_completed",
            success=result.success,
            ticket_id=result.ticket_id,
            assigned_id=result.assigned_id,
            status_code=result.status_code,
            error=result.error,
            result_response=result.response,
            context={**debug_context, "request_url": url, "ticket_id": tid},
        )
        return result

    async def cancel_room_booking_ticket(
        self,
        *,
        session_id: str,
        property_name: str = "",
        conversation_uid: str = "",
        reason: str = "",
    ) -> "TicketingResult":
        """Mark a silent room-booking ticket Cancelled in BOTH dashboards.

        Sets new_bot_tickets.status -> 'cancelled' (KePSLA) for the most recent
        open room-booking ticket of this session (optionally scoped to a property),
        and best-effort PATCHes the external API when the row carries a real ref.
        Never raises.
        """
        return await self._mutate_room_booking_ticket(
            session_id=session_id,
            property_name=property_name,
            conversation_uid=conversation_uid,
            mode="cancel",
            note=str(reason or "").strip() or "Guest cancelled this booking request.",
        )

    async def update_room_booking_ticket(
        self,
        *,
        session_id: str,
        property_name: str = "",
        conversation_uid: str = "",
        new_issue: str = "",
        note: str = "",
    ) -> "TicketingResult":
        """Update a silent room-booking ticket (e.g. changed dates) in BOTH dashboards.

        Rewrites the issue/description text on the most recent open room-booking
        ticket of this session (optionally scoped to a property) and best-effort
        PATCHes the external API. Never raises.
        """
        return await self._mutate_room_booking_ticket(
            session_id=session_id,
            property_name=property_name,
            conversation_uid=conversation_uid,
            mode="update",
            new_issue=str(new_issue or "").strip(),
            note=str(note or "").strip() or "Guest updated this booking request.",
        )

    async def _mutate_room_booking_ticket(
        self,
        *,
        session_id: str,
        property_name: str = "",
        conversation_uid: str = "",
        mode: str = "cancel",
        new_issue: str = "",
        note: str = "",
    ) -> "TicketingResult":
        sid = str(session_id or "").strip()
        if not sid:
            return TicketingResult(success=False, error="session_id is required")
        prop = str(property_name or "").strip()
        cuid = str(conversation_uid or "").strip()
        ext_ref = ""
        row_id = None
        row_guest_name = ""
        row_phase = ""
        try:
            from models.database import AsyncSessionLocal, Ticket
            from sqlalchemy import select as _select, or_ as _or_
            from datetime import datetime as _dt, timezone as _tz

            async with AsyncSessionLocal() as db:
                # Identify the room-booking row by its CATEGORY — the `source` column
                # is a constrained enum (_resolve_ticket_source coerces everything to
                # "manual"), so the room_booking category/sub_category is the reliable
                # marker. Most recent open one for this session (+ property) wins.
                # sub_category only - support/event tickets also carry the category
                # "room booking request", so a room-booking cancel could hit them.
                q = _select(Ticket).where(
                    Ticket.session_id == sid,
                    Ticket.sub_category.in_(["room_booking", "room booking request", "room booking"]),
                    Ticket.status != "cancelled",
                )
                if prop:
                    q = q.where(Ticket.property_name == prop)
                if cuid:
                    q = q.where(Ticket.conversation_uid == cuid)
                q = q.order_by(Ticket.created_at.desc())
                row = (await db.execute(q)).scalars().first()
                if row is None:
                    self._write_ticket_debug_event(
                        "room_booking_ticket_mutate_not_found",
                        mode=mode, session_id=sid, property_name=prop,
                    )
                    # Already cancelled earlier in the same turn (the ticket-edit
                    # check cancels "cancel that" itself, with its reason) is a
                    # cancel that landed, not a failure. Pre-booking-tab hotels only.
                    if mode == "cancel":
                        try:
                            from services import prebooking_settings_service as _pbs_nf
                            from services.db_config_service import db_config_service as _dbc_nf

                            if _pbs_nf.is_admin_managed(str(_dbc_nf.get_current_hotel_code() or "").strip().lower()):
                                q_done = _select(Ticket).where(
                                    Ticket.session_id == sid,
                                    Ticket.sub_category.in_(["room_booking", "room booking request", "room booking"]),
                                    Ticket.status == "cancelled",
                                )
                                if prop:
                                    q_done = q_done.where(Ticket.property_name == prop)
                                if cuid:
                                    q_done = q_done.where(Ticket.conversation_uid == cuid)
                                done = (await db.execute(q_done.order_by(Ticket.updated_at.desc()))).scalars().first()
                                if done is not None:
                                    return TicketingResult(
                                        success=True,
                                        ticket_id=str(getattr(done, "ticket_ref", "") or getattr(done, "id", "") or sid),
                                    )
                        except Exception:
                            pass
                    return TicketingResult(success=False, status_code=404, error="no matching room-booking ticket")
                if mode == "cancel":
                    row.status = "cancelled"
                elif mode == "update" and new_issue:
                    row.issue = new_issue
                    row.description = new_issue
                    row.message = new_issue
                _prev = str(getattr(row, "manager_notes", "") or "").strip()
                row.manager_notes = (f"{_prev}\n{note}" if _prev else note).strip()
                try:
                    row.updated_at = _dt.now(_tz.utc)
                except Exception:
                    pass
                ext_ref = str(getattr(row, "ticket_ref", "") or "").strip()
                row_id = getattr(row, "id", None)
                row_guest_name = str(getattr(row, "guest_name", "") or "").strip()
                row_phase = str(getattr(row, "phase", "") or "").strip()
                await db.commit()
            logger.info(
                "Room-booking ticket %s in KePSLA DB: row_id=%s ext_ref=%s session=%s property=%r",
                mode, row_id, ext_ref or "-", sid, prop,
            )
        except Exception as exc:
            logger.warning("Room-booking ticket %s (DB) failed: %s", mode, exc)
            return TicketingResult(success=False, error=str(exc))

        # Best-effort external API PATCH — only when we have a real external ref
        # (create returns none while the Lumira API is down, which is the norm).
        # Hotels set up in the Pre-booking tab (T24 onward) send the cancellation
        # reason the external API requires, and report a refused cancel instead of
        # claiming it: on T24 a property switch sent the cancel without one, got
        # 400 "Cancellation Reason is required when Ticket Status is Closed or
        # Cancelled", and the guest was told "Done - I've cancelled" while the
        # ticket stayed open on the dashboard. Zone, Mango and Sarovar unchanged.
        _admin_hotel = False
        try:
            from services import prebooking_settings_service as _pbs_rb
            from services.db_config_service import db_config_service as _dbc_rb

            _admin_hotel = _pbs_rb.is_admin_managed(
                str(_dbc_rb.get_current_hotel_code() or "").strip().lower()
            )
        except Exception:
            _admin_hotel = False
        _external_ok = True
        if ext_ref:
            try:
                if mode == "cancel":
                    extra = {"ticket_status": "cancelled", "status": "cancelled"}
                    if _admin_hotel:
                        # The dashboard's own value is "cancel" (GHN_OPERATION_TICKETS:
                        # 552 rows up to today; "cancelled" last used on 8234).
                        extra = {"ticket_status": "cancel", "status": "cancel"}
                        # Same field and the same clean-up as append_ticket_comment:
                        # the API keeps it in `cancelled_notes` and refuses anything
                        # beyond letters, numbers, spaces and basic punctuation.
                        _why = re.sub(r"[^A-Za-z0-9 .,\-]+", " ", str(note or ""))
                        _why = re.sub(r"\s+", " ", _why).strip(" .,-").strip() or "Cancelled by the guest"
                        extra["cancelled_notes"] = _why[:240]
                        extra["cancelledNotes"] = _why[:240]
                else:
                    extra = {"issue": new_issue} if new_issue else {}
                if row_guest_name:
                    # The external PATCH endpoint can null guestName when the
                    # field is omitted. Preserve the displayed identity on every
                    # room-booking mutation.
                    extra["guest_name"] = row_guest_name
                    extra["guestName"] = row_guest_name
                if row_phase.lower() == "pre booking":
                    # Keep lead tickets detached from any prior in-stay guest
                    # record on update/cancel too; otherwise the API dashboard
                    # name lookup can regress back to blank.
                    extra["guest_id"] = ""
                _patch = await self.update_ticket(ext_ref, note, extra_fields=extra)
                _external_ok = bool(getattr(_patch, "success", False))
            except Exception as exc:
                _external_ok = False
                logger.warning("External room-booking %s PATCH failed ref=%s: %s", mode, ext_ref, exc)
        if _admin_hotel and not _external_ok:
            return TicketingResult(
                success=False, ticket_id=ext_ref or str(row_id or sid),
                error=f"external room-booking {mode} was not accepted",
            )
        return TicketingResult(success=True, ticket_id=ext_ref or str(row_id or sid))

    async def list_session_tickets(
        self, session_id: str, *, limit: int = 15
    ) -> list[dict[str, Any]]:
        """Return this session's non-cancelled tickets (newest first) as light
        dicts for matching a cancel/edit request to the right existing ticket.

        Never raises — returns [] on any error.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return []
        try:
            from models.database import AsyncSessionLocal, Ticket
            from sqlalchemy import select as _select

            async with AsyncSessionLocal() as db:
                q = (
                    _select(Ticket)
                    .where(Ticket.session_id == sid, Ticket.status != "cancelled")
                    .order_by(Ticket.created_at.desc())
                    .limit(max(1, int(limit)))
                )
                rows = (await db.execute(q)).scalars().all()
                if rows:
                    # Self-heal the in-memory index. It is wiped by a restart, so
                    # a session that ticketed before a deploy looks ticket-less
                    # and every later edit takes the slow "unknown session" path.
                    # Finding real rows here proves it has tickets — remember it,
                    # and the rest of the conversation uses the fast gate again.
                    self._sessions_with_tickets.add(sid)
                out: list[dict[str, Any]] = []
                for row in rows:
                    out.append({
                        "ticket_ref": str(getattr(row, "ticket_ref", "") or "").strip(),
                        "issue": str(getattr(row, "issue", "") or "").strip(),
                        "category": str(getattr(row, "category", "") or "").strip(),
                        # WHAT KIND OF REQUEST THIS IS.
                        # `category` is the DEPARTMENT that will action it, which is
                        # often nothing like what the guest would call the request: a
                        # spa booking sits under "Front Office". Whoever is matching a
                        # cancel to a ticket was therefore shown a department name and
                        # a one-line issue, and had to guess. `sub_category` is the
                        # service that raised it ("spa & wellness booking") and
                        # `description` is the staff-facing summary — both already on
                        # the row, both free to read, and between them they say what
                        # the request actually IS.
                        "sub_category": str(getattr(row, "sub_category", "") or "").strip(),
                        "description": str(getattr(row, "description", "") or "").strip(),
                        "room_number": str(getattr(row, "room_number", "") or "").strip(),
                        # Whether staff still have it. A request already DONE must
                        # not silence the guest asking for it again; only work that
                        # is still open can make a new ask a duplicate.
                        "status": str(getattr(row, "status", "") or "").strip().lower(),
                        "created_at": str(getattr(row, "created_at", "") or ""),
                        # Which chat it was raised in, so a caller can keep to the
                        # chat the guest is in now (Mango's edit router does).
                        "conversation_uid": str(getattr(row, "conversation_uid", "") or "").strip(),
                    })
                return out
        except Exception as exc:
            logger.warning("list_session_tickets failed session=%s: %s", sid, exc)
            return []

    async def find_session_booking_ticket_ref(
        self, *, session_id: str, property_name: str = ""
    ) -> str:
        """Return the newest open room-booking ticket_ref for this session, or "".

        The room-booking flow already raises a silent ticket and stamps
        `property_name` on it precisely so it can be found again later. The
        booking-assistance form uses this to UPDATE that ticket instead of
        raising a second one for the same stay.

        When `property_name` is given, a ticket for that property wins; if none
        matches (e.g. the guest never narrowed), the newest booking ticket for
        the session is returned. Never raises — returns "" on any error.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        wanted = _normalize_key(property_name)
        try:
            from models.database import AsyncSessionLocal, Ticket
            from sqlalchemy import select as _select

            async with AsyncSessionLocal() as db:
                # Match on sub_category, NOT category. The create path remaps
                # category to the dashboard's accepted label ("room booking
                # request"), so filtering on category == "room_booking" matched
                # nothing and every assistance submit silently created a second
                # ticket instead of enriching the first. sub_category keeps the
                # raw "room_booking" value, so it is the stable key.
                q = (
                    _select(Ticket)
                    .where(
                        Ticket.session_id == sid,
                        Ticket.status != "cancelled",
                    )
                    .order_by(Ticket.created_at.desc())
                    .limit(30)
                )
                # sub_category ONLY: support, event and discount tickets also carry
                # the category "room booking request" (their department's label), so
                # matching on category picked a support ticket as the booking and the
                # guest's room details overwrote their callback request (ticket 14292).
                rows = [
                    row
                    for row in (await db.execute(q)).scalars().all()
                    if "roombooking" in _normalize_key(getattr(row, "sub_category", ""))
                ]
        except Exception as exc:
            logger.warning("find_session_booking_ticket_ref failed session=%s: %s", sid, exc)
            return ""

        newest_ref = ""
        for row in rows:
            ref = str(getattr(row, "ticket_ref", "") or "").strip()
            if not ref:
                continue
            if not newest_ref:
                newest_ref = ref
            if wanted and _normalize_key(getattr(row, "property_name", "")) == wanted:
                return ref
        return newest_ref

    async def is_ticket_still_editable(
        self,
        *,
        session_id: str,
        ticket_ref: str,
        within_minutes: int,
    ) -> bool:
        """Can this ticket still be changed — still `open`, and raised recently?

        Status is the gate, not just the clock: once staff move a ticket to
        in-progress, closed or cancelled, the work has started and neither the
        ticket nor the order behind it may be revised. Returns False whenever it
        cannot confirm, so nothing is edited on a guess.
        """
        sid = str(session_id or "").strip()
        ref = str(ticket_ref or "").strip()
        if not sid or not ref:
            return False
        try:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz

            from models.database import AsyncSessionLocal, Ticket
            from sqlalchemy import select as _select

            async with AsyncSessionLocal() as db:
                q = _select(Ticket).where(
                    Ticket.session_id == sid, Ticket.ticket_ref == ref
                ).order_by(Ticket.created_at.desc())
                row = (await db.execute(q)).scalars().first()
                if row is None:
                    return False
                if str(getattr(row, "status", "") or "").strip().lower() != "open":
                    return False
                window = int(within_minutes or 0)
                if window <= 0:
                    return True
                created = getattr(row, "created_at", None)
                if created is None:
                    return False
                if created.tzinfo is None:
                    created = created.replace(tzinfo=_tz.utc)
                return created >= _dt.now(_tz.utc) - _td(minutes=window)
        except Exception as exc:
            logger.warning("is_ticket_still_editable failed ref=%s: %s", ref, exc)
            return False

    async def _staff_have_picked_up(self, ticket_ref: str) -> bool | None:
        """Has the team started on this ticket, according to the staff dashboard?

        Our own `new_bot_tickets` row is written once and never hears anything
        back — there is no status API and no webhook — so it reads "open" forever,
        and "the order is already being prepared" could only ever be guessed from
        the clock. The dashboard's own table lives in the same database we are
        already connected to, so we can just ask it.

        The signal is the STATUS moving off `open`. Being assigned is deliberately
        NOT treated as pickup: the dashboard auto-assigns on creation, in the same
        second, by SYSTEM(LLM) — every one of the thousands of assignment records
        was written that way, and never once by a person. Reading that as "staff
        have started" makes every ticket untouchable the instant it exists, which
        collapses the edit window to zero and sends the kitchen a second ticket for
        food it is already making. An assignment by an actual human still counts,
        which is what the auto-assign flag distinguishes.

        Returns True (picked up), False (still untouched), or None when we cannot
        tell — a different database, a renamed table, anything. None means "no
        opinion": the caller keeps using the time window on its own, exactly as
        before this check existed.
        """
        ref = str(ticket_ref or "").strip()
        if not ref:
            return None
        try:
            from models.database import AsyncSessionLocal
            from sqlalchemy import text as _text

            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(
                        _text(
                            "SELECT status, assigned_id, assigned_to, ticket_auto_assign "
                            "FROM GHN_OPERATION_TICKETS WHERE id = :tid LIMIT 1"
                        ),
                        {"tid": ref},
                    )
                ).mappings().first()
            if not row:
                return None
            status = str(row.get("status") or "").strip().lower()
            try:
                assigned_id = int(row.get("assigned_id") or 0)
            except (TypeError, ValueError):
                assigned_id = 0
            auto_assigned = bool(row.get("ticket_auto_assign"))
            worked = bool(status) and status != "open"
            claimed_by_a_person = assigned_id > 0 and not auto_assigned
            picked_up = worked or claimed_by_a_person
            if picked_up:
                logger.info(
                    "[IRD] ticket %s is already with the team (status=%r "
                    "assigned_id=%s auto_assigned=%s) — no longer editable",
                    ref, status, assigned_id, auto_assigned,
                )
            return picked_up
        except Exception as exc:
            logger.info(
                "[IRD] could not read live ticket state for %s (%s) — "
                "falling back to the edit window alone", ref, exc,
            )
            return None

    async def find_recent_open_ticket(
        self,
        *,
        session_id: str,
        category_markers: tuple[str, ...],
        within_minutes: int,
        exclude_refs: "tuple[str, ...] | list[str] | None" = None,
    ) -> dict[str, Any] | None:
        """Most recent still-open ticket of a kind, raised within a time window.

        Used so a guest adding to an order already placed UPDATES that order
        instead of raising a second ticket for the whole cart — which had the
        kitchen receiving the first items twice. Never raises; returns None when
        there is nothing suitable.

        Being inside the window is necessary but not sufficient: a ticket the team
        has already picked up is off limits however recent it is, because the food
        is being made. See `_staff_have_picked_up`.

        `exclude_refs` are orders the guest has explicitly finished with by asking
        for a separate one. They can still be perfectly open and well inside the
        window, so nothing else here would skip them — and the next order would be
        merged into the very order the guest just said they were done with.
        """
        sid = str(session_id or "").strip()
        if not sid or int(within_minutes or 0) <= 0:
            return None
        _skip = {str(r).strip() for r in (exclude_refs or []) if str(r or "").strip()}
        try:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz

            from models.database import AsyncSessionLocal, Ticket
            from sqlalchemy import select as _select

            cutoff = _dt.now(_tz.utc) - _td(minutes=int(within_minutes))
            async with AsyncSessionLocal() as db:
                q = (
                    _select(Ticket)
                    .where(Ticket.session_id == sid, Ticket.status == "open")
                    .order_by(Ticket.created_at.desc())
                    .limit(10)
                )
                for row in (await db.execute(q)).scalars().all():
                    cat = f"{getattr(row, 'category', '')} {getattr(row, 'sub_category', '')}".lower()
                    if not any(m in cat for m in category_markers):
                        continue
                    created = getattr(row, "created_at", None)
                    if created is None:
                        continue
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=_tz.utc)
                    if created < cutoff:
                        continue
                    _ref = str(getattr(row, "ticket_ref", "") or "").strip()
                    if _ref and _ref in _skip:
                        continue
                    # Recent, but has the team already started on it?
                    if await self._staff_have_picked_up(_ref) is True:
                        continue
                    return {
                        "ticket_ref": _ref,
                        "issue": str(getattr(row, "issue", "") or "").strip(),
                        "created_at": created.isoformat(),
                    }
        except Exception as exc:
            logger.warning("find_recent_open_ticket failed session=%s: %s", sid, exc)
        return None

    async def append_ticket_comment(
        self,
        *,
        session_id: str,
        ticket_ref: str,
        note: str,
        set_status: str = "",
        set_issue: str = "",
        set_description: str = "",
    ) -> "TicketingResult":
        """Append a comment to an EXISTING ticket (by session + ref) instead of
        raising a new one — used when a guest cancels/edits something that was
        already ticketed. Updates new_bot_tickets.manager_notes (what the
        dashboard shows) and best-effort PATCHes the external API. Never raises.
        """
        sid = str(session_id or "").strip()
        ref = str(ticket_ref or "").strip()
        text = str(note or "").strip()
        if not sid or not ref or not text:
            return TicketingResult(success=False, error="session_id, ticket_ref and note are required")
        row_id = None
        row_guest_name = ""
        try:
            from models.database import AsyncSessionLocal, Ticket
            from sqlalchemy import select as _select
            from datetime import datetime as _dt, timezone as _tz

            async with AsyncSessionLocal() as db:
                q = _select(Ticket).where(
                    Ticket.session_id == sid,
                    Ticket.ticket_ref == ref,
                ).order_by(Ticket.created_at.desc())
                row = (await db.execute(q)).scalars().first()
                if row is None:
                    self._write_ticket_debug_event(
                        "ticket_comment_not_found", session_id=sid, ticket_ref=ref,
                    )
                    return TicketingResult(success=False, status_code=404, error="no matching ticket for session+ref")
                _prev = str(getattr(row, "manager_notes", "") or "").strip()
                row.manager_notes = (f"{_prev}\n{text}" if _prev else text).strip()
                if set_status:
                    # ONE SPELLING FOR "CANCELLED" IN OUR OWN COLUMN.
                    # This wrote whatever the caller passed, and the cancel path
                    # passes "cancel" — the word the EXTERNAL API wants. But every
                    # local query that asks "is this ticket still live?" tests
                    # `status != "cancelled"`, so a ticket cancelled through here
                    # kept showing up as open: 32 rows in the table say "cancel"
                    # and are invisible to that filter. The practical effect is
                    # that a guest could cancel a request and have it offered back
                    # to them as still open once the in-memory cancelled-set was
                    # lost (any restart). The external PATCH below still sends the
                    # caller's word; only our own column is normalised.
                    row.status = (
                        "cancelled"
                        if str(set_status).strip().lower().startswith("cancel")
                        else set_status
                    )
                # Revise what the ticket SAYS, not just its notes — a guest who
                # adds a dish needs the kitchen's copy of the order to list it,
                # not just a note underneath.
                if set_issue:
                    row.issue = set_issue
                if set_description:
                    row.description = set_description
                try:
                    row.updated_at = _dt.now(_tz.utc)
                except Exception:
                    pass
                row_id = getattr(row, "id", None)
                row_guest_name = str(getattr(row, "guest_name", "") or "").strip()
                await db.commit()
            self._write_ticket_debug_event(
                "ticket_comment_appended",
                session_id=sid, ticket_ref=ref, row_id=row_id, note=text, set_status=set_status,
            )
        except Exception as exc:
            logger.warning("append_ticket_comment (DB) failed session=%s ref=%s: %s", sid, ref, exc)
            return TicketingResult(success=False, error=str(exc))

        # Best-effort external API PATCH (only when a real external ref exists).
        try:
            extra: dict[str, Any] = {}
            if set_status:
                extra["ticket_status"] = set_status
                extra["status"] = set_status
                # ── A CANCEL WITHOUT A REASON IS REFUSED, AND WE NEVER NOTICED ──
                # The external API answers a cancel with
                #   400 "Cancellation Reason is required when Ticket Status is
                #        Closed or Cancelled"
                # so 11321 and 11327 stayed OPEN on the hotel's dashboard while
                # our own row said cancelled - the bot then told the guest their
                # order was "definitely cancelled, no food will be prepared" and
                # the kitchen cooked it anyway. Sent under several spellings for
                # the same reason `guest_name`/`guestName` are: this API's field
                # naming is not consistent and a rejected cancel is silent.
                # The field is `cancelled_notes` - the column the dashboard stores
                # it in (GHN_OPERATION_TICKETS). Guessing at the obvious names
                # first cost a round: cancellation_reason / cancellationReason /
                # cancel_reason / cancelReason were all still refused with the
                # same 400. The schema had the answer.
                if str(set_status).strip().lower().startswith(("cancel", "clos")):
                    # ── AND THE REASON HAS TO SURVIVE THEIR VALIDATOR ────────
                    # Sending the note verbatim earned a second 400:
                    #   "Cancellation Reason can only contain letters, numbers,
                    #    spaces, and basic punctuation"
                    # for "CANCELLED: Guest requested cancellation of the entire
                    # in-room dining order." - our own notes carry colons, and
                    # order text carries markdown, the multiplication sign and
                    # rupee symbols. A rejected cancel is SILENT: the book says
                    # cancelled, the dashboard stays open, and the kitchen cooks
                    # food the guest was told was stopped. So the reason is
                    # reduced to what their validator accepts rather than hoped
                    # over.
                    _why = str(text or "").strip() or "Cancelled by the guest"
                    _why = re.sub(r"[^A-Za-z0-9 .,\-]+", " ", _why)
                    _why = re.sub(r"\s+", " ", _why).strip(" .,-").strip()
                    _why = _why or "Cancelled by the guest"
                    extra["cancelled_notes"] = _why[:240]
                    extra["cancelledNotes"] = _why[:240]
            if set_issue:
                extra["issue"] = set_issue
            if set_description:
                extra["description"] = set_description
            if row_guest_name:
                extra["guest_name"] = row_guest_name
                extra["guestName"] = row_guest_name
            await self.update_ticket(ref, text, extra_fields=extra or None)
        except Exception as exc:
            logger.warning("External ticket-comment PATCH failed ref=%s: %s", ref, exc)
        return TicketingResult(success=True, ticket_id=ref or str(row_id or sid))

    async def update_ticket_details(
        self,
        *,
        session_id: str,
        ticket_ref: str,
        description: str = "",
        message: str = "",
        note: str = "",
    ) -> "TicketingResult":
        """Overwrite an EXISTING ticket's `description` and/or `message` in place.

        Sibling of `append_ticket_comment`, with one deliberate difference: it
        does NOT write `manager_notes`. Used by the booking-assistance form,
        where the guest is filling in the details of a request we already
        raised (dates / party size / their note) — staff should read the
        updated request itself, not a running commentary appended under it.

        `description` is what the KePSLA ticket UI shows; `message` is the
        fuller context field. Both are optional; whatever is passed is what
        gets replaced. Never raises.
        """
        sid = str(session_id or "").strip()
        ref = str(ticket_ref or "").strip()
        new_description = str(description or "").strip()
        new_message = str(message or "").strip()
        if not sid or not ref:
            return TicketingResult(success=False, error="session_id and ticket_ref are required")
        if not new_description and not new_message:
            return TicketingResult(success=False, error="nothing to update")

        row_id = None
        row_guest_name = ""
        try:
            from models.database import AsyncSessionLocal, Ticket
            from sqlalchemy import select as _select
            from datetime import datetime as _dt, timezone as _tz

            async with AsyncSessionLocal() as db:
                q = _select(Ticket).where(
                    Ticket.session_id == sid,
                    Ticket.ticket_ref == ref,
                ).order_by(Ticket.created_at.desc())
                row = (await db.execute(q)).scalars().first()
                if row is None:
                    self._write_ticket_debug_event(
                        "ticket_details_not_found", session_id=sid, ticket_ref=ref,
                    )
                    return TicketingResult(
                        success=False, status_code=404, error="no matching ticket for session+ref"
                    )
                if new_description:
                    row.description = new_description
                if new_message:
                    row.message = new_message
                try:
                    row.updated_at = _dt.now(_tz.utc)
                except Exception:
                    pass
                row_id = getattr(row, "id", None)
                row_guest_name = str(getattr(row, "guest_name", "") or "").strip()
                await db.commit()
            self._write_ticket_debug_event(
                "ticket_details_updated",
                session_id=sid, ticket_ref=ref, row_id=row_id,
                description=new_description, message=new_message,
            )
        except Exception as exc:
            logger.warning("update_ticket_details (DB) failed session=%s ref=%s: %s", sid, ref, exc)
            return TicketingResult(success=False, error=str(exc))

        # Best-effort external API PATCH so the staff dashboard matches the DB.
        try:
            extra: dict[str, Any] = {}
            if new_description:
                # The external dashboard's "Description" column IS the `issue`
                # field (create sets issue = the KePSLA description). Sending
                # only `description` — a field that API does not have — left the
                # dashboard showing the ORIGINAL one-line request while every
                # detail the guest typed showed up under Manager Notes instead.
                extra["issue"] = new_description
                extra["description"] = new_description
            if new_message:
                extra["message"] = new_message
            if row_guest_name:
                extra["guest_name"] = row_guest_name
                extra["guestName"] = row_guest_name
            # update_ticket() requires a non-empty note. Keep it a short marker:
            # the detail belongs in the request text, and passing the whole
            # description here duplicated the entire block into Manager Notes.
            #
            # ── EXCEPT WHEN THE REQUEST TEXT NEVER ARRIVES ──────────────────
            # The external API accepts this PATCH with 200 and then IGNORES
            # `issue`: a food order edited from 3 parathas to 4 kept showing
            # "3 Lachha Parathas" on the hotel's dashboard while our own row
            # read 4, and this generic marker was the ONLY thing staff saw
            # change. Verified against GHN_OPERATION_TICKETS after a live edit.
            # Manager Notes is the field that does land, so a caller that has
            # something specific to say may say it there.
            await self.update_ticket(
                ref,
                str(note or "").strip() or "Guest updated the details of this request.",
                extra_fields=extra or None,
            )
        except Exception as exc:
            logger.warning("External ticket-details PATCH failed ref=%s: %s", ref, exc)
        return TicketingResult(success=True, ticket_id=ref or str(row_id or sid))

    async def handoff_to_agent(
        self,
        conversation_id: str,
        session_id: str,
        reason: str,
        agent_id: str,
        widget_id: str = "",
        *,
        channel: str = "",
        flow: str = "",
        wa_number: str = "",
        entity_id: str = "",
    ) -> TicketingResult:
        """Trigger human-agent handoff API."""
        flow_norm = str(flow or "").strip().lower()
        channel_norm = str(channel or "").strip().lower()
        wa_number_norm = str(wa_number or "").strip()
        entity_id_norm = str(entity_id or "").strip()
        use_guest_journey_handoff = (
            flow_norm == "guest_journey"
            or channel_norm == "whatsapp"
            or (bool(wa_number_norm) and bool(entity_id_norm))
        )

        gj_url = str(getattr(settings, "agent_handoff_api_url_gj", "") or "").strip()
        default_url = str(settings.agent_handoff_api_url or "").strip()
        url = gj_url if use_guest_journey_handoff and gj_url else default_url
        if not url:
            self._write_ticket_debug_event(
                "handoff_rejected_missing_url",
                error="No handoff URL is configured",
                conversation_id=conversation_id,
                session_id=session_id,
                channel=channel_norm,
                flow=flow_norm,
            )
            return TicketingResult(success=False, error="No handoff URL is configured")

        if use_guest_journey_handoff and gj_url:
            payload = {
                "wa_number": wa_number_norm,
                "from_responder": "BOT",
                "to_responder": "AGENT",
                "to_agent_id": str(agent_id or "").strip(),
                "reason": str(reason or "").strip(),
                "entity_id": entity_id_norm,
            }
            handoff_mode = "guest_journey"
        else:
            payload = {
                "from_responder": "BOT",
                "to_responder": "AGENT",
                "conversation_id": str(conversation_id or "").strip(),
                "session_id": str(session_id or "").strip(),
                "to_agent_id": str(agent_id or "").strip(),
                "widget_id": str(widget_id or "").strip(),
                "reason": str(reason or "").strip(),
            }
            handoff_mode = "engage"
        self._write_ticket_debug_event(
            "handoff_requested",
            payload=payload,
            request_url=url,
            handoff_mode=handoff_mode,
        )
        result = await self._request_json(
            "POST",
            url,
            payload,
            debug_context={
                "operation": "handoff_to_agent",
                "request_url": url,
                "conversation_id": str(conversation_id or "").strip(),
                "session_id": str(session_id or "").strip(),
                "handoff_mode": handoff_mode,
            },
            expect_ticket_id=False,
        )
        self._write_ticket_debug_event(
            "handoff_completed",
            success=result.success,
            status_code=result.status_code,
            error=result.error,
            result_response=result.response,
            request_url=url,
            conversation_id=str(conversation_id or "").strip(),
            session_id=str(session_id or "").strip(),
            handoff_mode=handoff_mode,
        )
        return result

    @staticmethod
    def _load_property_entity_map() -> dict[str, dict[str, str]]:
        """Parse settings.ticketing_property_entity_map_json.

        Shape: { hotel_code: { property_match_substring: fms_entity_id } }.
        All keys are lowercased; values stringified. Returns {} on any problem.
        Hotels whose ticket routing is set in the admin (Bot Training ->
        Pre-booking) use those entity ids instead of the env.
        """
        from services import prebooking_settings_service

        normalized: dict[str, dict[str, str]] = {}
        raw = str(getattr(settings, "ticketing_property_entity_map_json", "") or "").strip()
        parsed: Any = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning("ticketing_property_entity_map_json is not valid JSON; ignoring")
                parsed = {}
        for hotel_code, prop_map in (parsed.items() if isinstance(parsed, dict) else []):
            if not isinstance(prop_map, dict):
                continue
            inner: dict[str, str] = {}
            for match, entity_id in prop_map.items():
                match_key = str(match or "").strip().lower()
                entity_val = str(entity_id or "").strip()
                if match_key and entity_val:
                    inner[match_key] = entity_val
            code_key = str(hotel_code or "").strip().lower()
            if code_key and inner:
                normalized[code_key] = inner
        normalized.update(prebooking_settings_service.entity_map_overlay())
        return normalized

    @classmethod
    def _load_allowed_departments_map(cls) -> dict[str, set[str]]:
        """Parse settings.ticketing_allowed_departments_json.

        Shape: { hotel_code: [department_name, ...] }. Returns
        { hotel_code_lower: {normalized_name, ...} }. A hotel present here has a
        CLOSED SET — tickets may only be assigned to these departments. Absent
        hotel => empty => no restriction. Returns {} on any problem. Admin-set
        departments (Bot Training -> Pre-booking) win over the env.
        """
        out: dict[str, set[str]] = {}
        for hotel_code, names in cls._load_allowed_departments_raw().items():
            norm = {cls._normalize_category_key(n) for n in names if str(n or "").strip()}
            norm.discard("")
            if norm:
                out[hotel_code] = norm
        return out

    @classmethod
    def _load_allowed_departments_raw(cls) -> dict[str, list[str]]:
        """Same env value as `_load_allowed_departments_map`, names UNCHANGED.

        That map normalizes for comparison ("Food and Beverages" -> "food and
        beverages"), which is right for matching but wrong for looking a
        department up by name — DEPARTMENT_MASTER stores the original spelling.
        Admin-set departments (Bot Training -> Pre-booking) win over the env.
        """
        from services import prebooking_settings_service

        out: dict[str, list[str]] = {}
        raw = str(getattr(settings, "ticketing_allowed_departments_json", "") or "").strip()
        parsed: Any = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning("ticketing_allowed_departments_json is not valid JSON; ignoring")
                parsed = {}
        for hotel_code, names in (parsed.items() if isinstance(parsed, dict) else []):
            code_key = str(hotel_code or "").strip().lower()
            if not code_key or not isinstance(names, list):
                continue
            kept = [str(n).strip() for n in names if str(n or "").strip()]
            if kept:
                out[code_key] = kept
        out.update(prebooking_settings_service.allowed_departments_overlay())
        return out

    def _allowed_department_names_for(self, payload: dict[str, Any]) -> list[str]:
        """This hotel's configured department names, as written in the env.

        Returns [] when the hotel has no allowlist, so an unrestricted tenant
        never gets a department invented for it.
        """
        allow_raw = self._load_allowed_departments_raw()
        if not allow_raw:
            return []
        hotel_code = self._safe_str(payload.get("hotel_code")).lower()
        if not hotel_code:
            try:
                from services.db_config_service import db_config_service
                hotel_code = str(db_config_service.get_current_hotel_code() or "").strip().lower()
            except Exception:
                hotel_code = ""
        return [str(n).strip() for n in (allow_raw.get(hotel_code) or []) if str(n).strip()]

    def _filter_departments_to_allowlist(
        self,
        payload: dict[str, Any],
        departments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep only departments whose name is in the per-hotel closed set.

        No allowlist configured for the hotel => the list is returned unchanged
        (other tenants are unaffected). Matching is punctuation/case-insensitive.
        If the allowlist would remove everything, we still return the filtered
        (possibly empty) list so a disallowed department is NEVER assigned — an
        empty list makes the caller fall through to "no department" rather than
        route to something outside the set.
        """
        if not isinstance(departments, list) or not departments:
            return departments
        allow_map = self._load_allowed_departments_map()
        if not allow_map:
            return departments
        hotel_code = self._safe_str(payload.get("hotel_code")).lower()
        if not hotel_code:
            try:
                from services.db_config_service import db_config_service
                hotel_code = str(db_config_service.get_current_hotel_code() or "").strip().lower()
            except Exception:
                hotel_code = ""
        allowed = allow_map.get(hotel_code)
        if not allowed:
            return departments  # this hotel isn't restricted
        filtered = [
            d for d in departments
            if isinstance(d, dict)
            and self._normalize_category_key(d.get("department_name")) in allowed
        ]
        if len(filtered) != len(departments):
            kept = [self._safe_str(d.get("department_name")) for d in filtered]
            dropped = [
                self._safe_str(d.get("department_name"))
                for d in departments
                if self._normalize_category_key(d.get("department_name")) not in allowed
            ]
            self._write_ticket_debug_event(
                "ticket_department_allowlist_filtered",
                hotel_code=hotel_code,
                kept=kept,
                dropped=dropped,
            )
            logger.info(
                "Ticket dept allowlist (%s): kept=%s dropped=%s",
                hotel_code, kept, dropped,
            )
        return filtered

    def _resolve_property_entity_id(
        self,
        *,
        context: ConversationContext,
        pending: dict[str, Any],
        integration: dict[str, Any],
    ) -> str:
        """Resolve organisation_id (FMS entity id) from the selected property.

        Multi-property tenants route each property to a different FMS entity id
        (e.g. Zone: Kolkata -> 6290, Jaipur -> 6295). Matching is a lowercase
        substring test of the configured key against the selected property name.
        Returns "" when no mapping is configured or nothing matches, so the
        caller keeps its existing hotel-wide fallback.
        """
        entity_map = self._load_property_entity_map()
        if not entity_map:
            return ""
        hotel_code = str(getattr(context, "hotel_code", "") or "").strip().lower()
        prop_map = entity_map.get(hotel_code)
        if not prop_map:
            return ""

        pending = pending if isinstance(pending, dict) else {}
        integration = integration if isinstance(integration, dict) else {}
        candidates: list[Any] = [
            pending.get("_selected_property_scope_name"),
            pending.get("selected_property_scope_name"),
            pending.get("_stay_property_name"),
            pending.get("stay_property_name"),
            getattr(context, "booking_property_name", None),
            integration.get("booking_property_name"),
            integration.get("property_name"),
            pending.get("property_name"),
            pending.get("property"),
            pending.get("hotel_name"),
        ]
        active_names = pending.get("_active_property_names")
        if isinstance(active_names, list) and len(active_names) == 1:
            candidates.append(active_names[0])

        # Session location (from the contact/returning-guest dropdown) is the
        # authoritative routing key under the session-centric model: it is
        # captured every visit, so it resolves the property entity even when the
        # conversational property scope was never set or got cleared. The map
        # keys are city/property substrings (e.g. "kolkata"), which a location
        # value like "Kolkata" matches directly. Checked LAST so an explicitly
        # selected property still wins over the broader location.
        candidates.extend([
            pending.get("location"),
            integration.get("location"),
            pending.get("guest_location"),
            pending.get("city"),
        ])

        for candidate in candidates:
            name = str(candidate or "").strip().lower()
            if not name:
                continue
            # Longest matching key wins so a specific property beats the bare
            # city when one contains the other (e.g. "new market kolkata" -> the
            # New Market entity, not plain Kolkata; "connect park coimbatore" ->
            # the Connect entity, not the by-the-Park one). A single property
            # name is unambiguous, so picking the most specific key is correct.
            best_entity = ""
            best_len = 0
            for match_key, entity_id in prop_map.items():
                if match_key and match_key in name and len(match_key) > best_len:
                    best_len = len(match_key)
                    best_entity = entity_id
            if best_entity:
                return best_entity
        # Single-property tenant: nothing above matched because there is no
        # property picker, so the candidate names are simply absent. Its map
        # points at exactly one entity, which makes the answer unambiguous —
        # use it rather than letting the wrong-tenant guard strand the ticket
        # as local-only. Opt-in per hotel_code so multi-property tenants (Zone)
        # keep strict name matching and never silently collapse to one entity.
        if hotel_code in self._load_single_entity_hotels():
            distinct = {str(v or "").strip() for v in prop_map.values() if str(v or "").strip()}
            if len(distinct) == 1:
                return next(iter(distinct))
        return ""

    @classmethod
    def _load_single_entity_hotels(cls) -> set[str]:
        """Lowercased hotel_codes from settings.ticketing_single_entity_hotels_json,
        plus admin-managed hotels whose properties all share one FMS entity."""
        from services import prebooking_settings_service

        overlay = prebooking_settings_service.single_entity_overlay()
        raw = str(getattr(settings, "ticketing_single_entity_hotels_json", "") or "").strip()
        if not raw:
            return set(overlay)
        try:
            parsed = json.loads(raw)
        except Exception:
            logger.warning("ticketing_single_entity_hotels_json is not valid JSON; ignoring")
            return set(overlay)
        if not isinstance(parsed, list):
            return set(overlay)
        return {str(x or "").strip().lower() for x in parsed if str(x or "").strip()} | overlay

    def _resolve_property_entity_id_from_text(
        self,
        *,
        context: ConversationContext,
        text: str,
    ) -> str:
        """Last-resort entity resolution from free text (the ticket's issue /
        message).

        The structured property scope (``_selected_property_scope_name`` etc.)
        can be missing at form-submit time when the lead context was rebuilt —
        which would let ``organisation_id`` fall back to a non-property value
        ("zone" / the 5703 group default) and get blocked as a wrong tenant. The
        ticket's own description almost always still names the property
        ("...room cleaning at Zone Park Kolkata"), so match the configured
        property keys against that text. Returns "" when nothing matches so the
        caller keeps its existing fallback + wrong-tenant guard.
        """
        blob = str(text or "").strip().lower()
        if not blob:
            return ""
        entity_map = self._load_property_entity_map()
        hotel_code = str(getattr(context, "hotel_code", "") or "").strip().lower()
        prop_map = entity_map.get(hotel_code)
        if not prop_map:
            return ""
        # Leak-safe resolution from free text.
        matched = [
            (match_key, entity_id)
            for match_key, entity_id in prop_map.items()
            if match_key and match_key in blob
        ]
        if not matched:
            return ""
        # Drop keys that are a substring of another matched key, so a nested
        # pair like ("coimbatore" ⊂ "connect park coimbatore") collapses to the
        # most specific one instead of looking like two different properties.
        maximal = [
            (key, entity_id)
            for (key, entity_id) in matched
            if not any(key != other and key in other for (other, _e) in matched)
        ]
        distinct_entities = {entity_id for _key, entity_id in maximal}
        # Only resolve when the maximal keys agree on ONE entity. If the text
        # genuinely names two different mapped properties (e.g. "Kolkata and
        # Jaipur"), it is ambiguous — return "" so the caller keeps its
        # wrong-tenant guard instead of guessing and mis-routing the ticket.
        if len(distinct_entities) == 1:
            return next(iter(distinct_entities))
        return ""

    def build_lumira_ticket_payload(
        self,
        context: ConversationContext,
        issue: str,
        message: str,
        *,
        category: str = "complaint",
        sub_category: str = "",
        priority: str = "medium",
        department_id: str = "",
        department_manager: str = "",
        manager_notes: str = "",
        phase: str = "",
        sla_due_time: str = "",
        outlet_id: str = "",
        assigned_to: str = "",
        sentiment_score: str = "",
        source: str = "",
        group_id: str | int | None = None,
        message_id: str | int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cost: float | None = None,
        ticket_status: str = "open",
    ) -> dict[str, Any]:
        """
        Build a Lumira-style ticket payload from context + normalized fields.
        """
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = self.get_integration_context(context)
        latest_ticket = self.get_latest_ticket(context)
        phase_value = self._normalize_phase(
            phase
            or integration.get("phase")
            or self._default_phase_for_context(integration)
        )
        # Pre-booking leads have no real guest record. Lumira's engage path sends
        # guest_id="" (create_engage_ticket_from_payload); a synthetic id would
        # point at no GHN_FF_GUEST_INFO row, so the dashboard's name lookup
        # (guest_id -> guest record) returns blank. Leave it empty for leads.
        _is_prebooking_phase = phase_value == "Pre Booking"

        guest_id = self._first_non_empty(
            integration.get("guest_id"),
            pending.get("guest_id"),
            latest_ticket.get("guest_id"),
        )
        if _is_prebooking_phase:
            # Pre-booking tickets must not carry a concrete guest record id from
            # an earlier stay lookup or prior in-stay context. The external
            # dashboard resolves the displayed name via guest_id first; when a
            # lead ticket points at an unrelated / missing guest row, the name
            # column renders blank even if guest_name is present on the ticket.
            guest_id = ""
        channel_user_id = self._first_non_empty(
            integration.get("user_id"),
            pending.get("user_id"),
            integration.get("wa_number"),
            pending.get("wa_number"),
        )
        if guest_id and channel_user_id and guest_id != channel_user_id:
            logger.warning(
                "Ticket identity mismatch: guest_id and user_id differ (session_id=%s guest_id=%s user_id=%s)",
                str(context.session_id or "").strip(),
                guest_id,
                self._mask_identifier(channel_user_id),
            )
        if not guest_id and not _is_prebooking_phase:
            logger.warning(
                "Missing real guest_id; attempting synthetic fallback (session_id=%s entity_id=%s user_id=%s phone=%s)",
                str(context.session_id or "").strip(),
                str(integration.get("entity_id") or integration.get("organisation_id") or "").strip(),
                self._mask_identifier(channel_user_id),
                self._mask_identifier(
                    pending.get("guest_phone")
                    or integration.get("guest_phone")
                    or context.guest_phone
                ),
            )
            guest_id = self._derive_generated_guest_id(
                context=context,
                integration=integration,
                pending=pending,
            )
            if guest_id and isinstance(context.pending_data, dict):
                integration_snapshot = context.pending_data.get("_integration", {})
                integration_update = (
                    dict(integration_snapshot) if isinstance(integration_snapshot, dict) else {}
                )
                integration_update["guest_id"] = guest_id
                context.pending_data["_integration"] = integration_update
                context.pending_data.setdefault("guest_id", guest_id)
                integration = integration_update
                logger.warning(
                    "Using synthetic guest_id fallback (session_id=%s guest_id=%s)",
                    str(context.session_id or "").strip(),
                    guest_id,
                )
        organisation_id = self._first_non_empty(
            integration.get("organisation_id"),
            integration.get("organization_id"),
            integration.get("org_id"),
            integration.get("entity_id"),
            pending.get("organisation_id"),
            pending.get("organization_id"),
            pending.get("org_id"),
            pending.get("entity_id"),
            latest_ticket.get("organisation_id"),
            latest_ticket.get("organization_id"),
            latest_ticket.get("org_id"),
            latest_ticket.get("entity_id"),
            context.hotel_code,
        )
        # Per-property entity override for multi-property tenants (e.g. Zone).
        # The Kepsla ticket API expects the FMS entity id in `organisation_id`;
        # the selected property decides which one (Kolkata -> 6290, Jaipur ->
        # 6295) so the ticket attaches to the right org in the dashboard. Falls
        # back to the hotel-wide value above when nothing matches.
        per_property_entity_id = self._resolve_property_entity_id(
            context=context,
            pending=pending,
            integration=integration,
        )
        if not per_property_entity_id:
            # Rescue: structured property scope can be missing at form-submit
            # (lead context rebuilt, e.g. after the guest edited the prefilled
            # form). The ticket's own issue/message text usually still names the
            # property, so resolve the FMS entity from that BEFORE we fall back
            # to a non-property org id that the wrong-tenant guard would block
            # (which silently strands the ticket as a local-only record).
            per_property_entity_id = self._resolve_property_entity_id_from_text(
                context=context,
                text=f"{issue}\n{message}",
            )
            if per_property_entity_id:
                logger.info(
                    "Ticket entity resolved from issue/message text: %s (session_id=%s)",
                    per_property_entity_id,
                    str(getattr(context, "session_id", "") or "").strip(),
                )
        if per_property_entity_id:
            if organisation_id and organisation_id != per_property_entity_id:
                logger.info(
                    "Ticket entity override by property: %s -> %s (session_id=%s)",
                    organisation_id,
                    per_property_entity_id,
                    str(context.session_id or "").strip(),
                )
            organisation_id = per_property_entity_id
        # Multi-property tenant guard: if this hotel has a per-property entity
        # map but the resolved entity is NOT one of its configured property/FMS
        # entities (e.g. the 5703 fallback, or a Zone location whose FMS id is
        # not configured yet), pushing it to the external API would mis-route to
        # the WRONG company. Flag it so create_ticket creates the ticket
        # LOCAL-ONLY (KePSLA page) and skips the external API, instead of
        # mis-routing — the guest still always gets a ticket.
        _entity_map = self._load_property_entity_map()
        _hotel_key = str(getattr(context, "hotel_code", "") or "").strip().lower()
        _hotel_entities = _entity_map.get(_hotel_key)
        from services import prebooking_settings_service as _pbs

        # A hotel set up in the admin (Bot Training -> Pre-booking) only ever
        # sends tickets to the FMS entities configured for it there. Until they
        # are, nothing goes out: the test-harness fallback (5703) belongs to
        # another company, and a ticket must never land on someone else's
        # dashboard.
        _admin_routed = _pbs.is_admin_managed(_hotel_key)
        _force_local_only = (bool(_hotel_entities) or _admin_routed) and str(organisation_id) not in set(
            (_hotel_entities or {}).values()
        )
        if _force_local_only and _admin_routed:
            logger.warning(
                "Ticket kept LOCAL: %s has no FMS entity configured for this ticket "
                "(Pre-booking -> Ticket routing); resolved entity %r is not one of its own. "
                "session_id=%s",
                _hotel_key,
                organisation_id,
                str(getattr(context, "session_id", "") or "").strip(),
            )
        # Pre-booking guests have NOT checked in, so there is never a room number.
        # Recognize the canonical normalized phase ("Pre Booking") too — this guard
        # previously normalized only hyphens, so the spaced display name "Pre Booking"
        # slipped through and room-number extraction wrongly ran, pulling "at" out of
        # "...book a room at <property>" and showing "AT" in the room-number column.
        _phase_norm = str(phase or "").strip().lower().replace("-", "_").replace(" ", "_")
        _is_pre_booking = phase_value == "Pre Booking" or _phase_norm in ("pre_booking", "prebooking", "")
        # Pre-ARRIVAL = has NOT checked in yet = pre_booking OR pre_checkin. Such
        # guests never have a room number assigned, so we must NEVER extract one
        # from the free-text request — that regex was pulling stray English words
        # after "room" (e.g. "...decorate the guest's room BEFORE arrival" -> "BEFORE",
        # "birthday room DECORATION" -> "DECORATION") into the room-number column.
        _is_pre_arrival = _is_pre_booking or _phase_norm in ("pre_checkin", "precheckin")
        if _is_pre_booking:
            # Guests haven't checked in yet; room number is always null for pre-booking tickets.
            room_number = ""
        else:
            # pre_checkin/during_stay/post_checkout may carry an AUTHORITATIVE room
            # number from the booking/context — that's fine to use; only TEXT
            # extraction below is gated on actual check-in (during_stay/post).
            room_number = self._first_non_empty(
                context.room_number,
                pending.get("room_number"),
                integration.get("room_number"),
                latest_ticket.get("room_number"),
            )

        now_ist = datetime.now(IST).strftime("%H:%M:%S %d-%m-%Y")
        issue_text = str(issue or "").strip() or str(message or "").strip()
        if not _is_pre_arrival and not room_number:
            room_number = self._extract_room_number_from_text(issue_text)
        if not _is_pre_arrival and not room_number:
            room_number = self._extract_room_number_from_text(message)

        department_allocated = self._first_non_empty(
            department_id,
            pending.get("department_id"),
            pending.get("ticket_department_id"),
            integration.get("department_id"),
            integration.get("department_allocated"),
            latest_ticket.get("department_id"),
            latest_ticket.get("department_allocated"),
        )
        department_manager_value = self._first_non_empty(
            department_manager,
            pending.get("department_head"),
            pending.get("department_manager"),
            integration.get("department_head"),
            integration.get("department_manager"),
            latest_ticket.get("department_manager"),
        )
        sentiment_value = self._first_non_empty(
            sentiment_score,
            pending.get("sentiment_score"),
            pending.get("ticket_sentiment_score"),
            integration.get("sentiment_score"),
            latest_ticket.get("sentiment_score"),
        )
        message_text = self._build_ticket_message_context(
            context=context,
            explicit_message=message,
            issue_text=issue_text,
        )
        source_value = self._resolve_ticket_source(
            explicit_source=source,
            integration=integration,
            channel=context.channel,
        )
        priority_value = self._normalize_priority(priority)
        # Pre-booking leads are low-urgency by default: a prospect filling a
        # booking/enquiry form is a sales lead, not an in-stay incident. Force
        # LOW for pre-booking tickets (config-gated) but never downgrade a
        # genuine emergency the LLM marked CRITICAL.
        if (
            _is_pre_booking
            and priority_value != "CRITICAL"
            and bool(getattr(settings, "ticketing_prebooking_force_low_priority", True))
        ):
            priority_value = "LOW"
        category_value = self._normalize_category(category)
        status_value = self._normalize_ticket_status(ticket_status)

        # Resolve guest name from form data, pending data, integration, or context
        guest_name = self._first_non_empty(
            pending.get("guest_name"),
            pending.get("full_name"),
            pending.get("name"),
            integration.get("guest_name"),
            context.guest_name,
        )

        # Resolve property name from pending data / context. The LIVE
        # active-property scope (what the router locked THIS turn) is
        # authoritative for the ticket label and MUST win over
        # booking_property_name / _stay_property_name / the plain property_name
        # field — those carry a value that can leak stale across conversations in
        # a session. Observed bug: scope correctly = "Zone Connect Park Parra Goa"
        # but booking_property_name was frozen on a prior "Mussoorie" conversation,
        # so the ticket was mislabelled Mussoorie. Use the single in-scope property
        # first, then the scope name, then fall back to the booking/stay fields.
        property_name = None
        _active_names = pending.get("_active_property_names")
        if isinstance(_active_names, list) and len(_active_names) == 1:
            property_name = str(_active_names[0]).strip() or None
        if not property_name:
            property_name = self._first_non_empty(
                pending.get("_selected_property_scope_name"),
                pending.get("property_name"),
                pending.get("property"),
                pending.get("hotel_name"),
                integration.get("property_name"),
                getattr(context, "booking_property_name", None),
            )
        room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_category"),
            integration.get("room_type"),
            getattr(context, "booking_room_type", None),
        )

        payload = {
            "guest_id": str(guest_id or "").strip(),
            "room_number": str(room_number or "").strip(),
            "organisation_id": str(organisation_id or "").strip(),
            "issue": issue_text,
            "message": message_text,
            "sentiment_score": str(sentiment_value or "").strip(),
            "department_allocated": str(department_allocated or "").strip(),
            "department_manager": str(department_manager_value or "").strip(),
            "assigned_to": str(assigned_to or "").strip(),
            "priority": priority_value,
            "categorization": category_value,
            "sub_categorization": str(sub_category or "").strip(),
            "manager_notes": str(manager_notes or "").strip(),
            "created_at": now_ist,
            "session_id": str(context.session_id or "").strip(),
            "phase": phase_value,
            "ticket_status": status_value,
            "chatBot": "yes",
            "sla_due_time": str(sla_due_time or "").strip(),
            "outlet_id": str(outlet_id or "").strip(),
            "source": source_value,
            "guest_name": str(guest_name or "").strip(),
            "property_name": str(property_name or "").strip(),
            "room_type": str(room_type or "").strip(),
        }

        # Internal-only flag (popped before the API call) — see create_ticket.
        # When set, the ticket is created LOCAL-ONLY (KePSLA) and the external
        # API call is skipped (entity not a configured FMS entity for this tenant).
        if _force_local_only:
            payload["_force_local_only"] = True
        # Internal-only: lets create_ticket apply hotel settings (e.g. the ticket
        # priority set per service in the admin) once the caller has stamped the
        # service on the payload.
        if _hotel_key:
            payload["_hotel_code"] = _hotel_key

        # Internal-only: the rotating per-thread conversation id active when this
        # ticket was raised. `_`-prefixed so create_ticket strips it from the
        # external POST; _save_ticket_to_db reads it back to stamp the ticket row
        # so the KePSLA Conversation History can be scoped to THIS thread only.
        _conv_uid = str(pending.get("_active_conversation_uid") or "").strip()
        if _conv_uid:
            payload["_conversation_uid"] = _conv_uid

        # Backward-compatible aliases some APIs accept.
        payload["department_id"] = payload["department_allocated"]
        payload["category"] = payload["categorization"]
        payload["sub_category"] = payload["sub_categorization"]
        # Kepsla API expects camelCase variants
        payload["guestName"] = payload["guest_name"]
        payload["propertyName"] = payload["property_name"]
        payload["roomType"] = payload["room_type"]

        resolved_group_id = group_id if group_id is not None else integration.get("group_id")
        resolved_message_id = message_id if message_id is not None else integration.get("message_id")
        # ── THE KEYS THE CHAT IS FILED UNDER, SO THE TICKET CAN FIND IT ──────
        # The guest's conversation is written to KEBO_ENGAGE_SESSIONS stamped
        # with widget_id, group_id and conversation_id. The ticket was sent with
        # none of them - 45 fields and not one of those three - so the only
        # thing tying a ticket to its chat was session_id, and the dashboard
        # showed no history. `resolve_widget_group` already knows the answer for
        # this hotel (Sarovar: widget 53, group 244); it was only ever used to
        # look up the guest's name and to stamp our own row, never to fill in
        # the payload that actually goes out.
        if resolved_group_id in (None, ""):
            try:
                from services.customer_details_service import (
                    customer_details_service as _cds_p,
                )
                from services.db_config_service import db_config_service as _dcs_p

                _wid, _gid = _cds_p.resolve_widget_group(
                    str(_dcs_p.get_current_hotel_code() or "").strip()
                )
                if _gid:
                    resolved_group_id = _gid
                if _wid and not payload.get("widget_id"):
                    payload["widget_id"] = _wid
            except Exception as _wg_exc:
                logger.warning(
                    "could not resolve widget/group for the ticket payload "
                    "(non-fatal): %r", _wg_exc
                )
        # The rotating per-thread id the chat rows carry. Sent under its plain
        # name as well as the `_`-prefixed internal copy above, which
        # create_ticket strips before the POST.
        if _conv_uid and not payload.get("conversation_id"):
            payload["conversation_id"] = _conv_uid
        if resolved_group_id not in (None, ""):
            # ── group_id IS A NUMBER TO THIS API, NOT A STRING ──────────────
            # `resolve_widget_group` answers in strings ('53', '244'), and
            # sending group_id:"244" is refused with HTTP 500 "Something went
            # wrong" - no field named, so it reads like a server fault rather
            # than our payload. Isolated by posting the same known-good ticket
            # five times, changing one field each: group_id:244 -> 200,
            # group_id:"244" -> 500, widget_id and conversation_id fine either
            # way. Coerced when it is numeric; anything non-numeric is passed
            # through untouched so a future non-integer id still reaches them.
            try:
                payload["group_id"] = int(str(resolved_group_id).strip())
            except (TypeError, ValueError):
                payload["group_id"] = resolved_group_id
        if payload.get("widget_id") not in (None, ""):
            try:
                payload["widget_id"] = int(str(payload["widget_id"]).strip())
            except (TypeError, ValueError):
                pass
        if resolved_message_id not in (None, ""):
            payload["message_id"] = resolved_message_id

        # ── THE EXACT SENTENCE THIS TICKET IS ABOUT ─────────────────────────
        # Internal-only (`_`-prefixed, so create_ticket strips it before the
        # POST). _resolve_engage_message_id matches the guest's chat row against
        # THIS, not against the whole transcript: the transcript carries every
        # earlier turn too, so "does the text appear in it" would happily accept
        # the PREVIOUS message and anchor the ticket to the wrong sentence.
        try:
            _msgs = list(getattr(context, "messages", None) or [])
            for _m in reversed(_msgs):
                if str(getattr(getattr(_m, "role", ""), "value", getattr(_m, "role", ""))).lower() == "user":
                    _txt = str(getattr(_m, "content", "") or "").strip()
                    if _txt:
                        payload["_anchor_text"] = _txt
                    break
        except Exception:
            pass  # falls back to the transcript check

        if input_tokens is not None:
            payload["input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            payload["output_tokens"] = int(output_tokens)
        if total_tokens is not None:
            payload["total_tokens"] = int(total_tokens)
        if cost is not None:
            payload["cost"] = float(cost)

        return payload

    @staticmethod
    async def _resolve_engage_message_id(payload: dict[str, Any]) -> None:
        """Replace `message_id` with the guest's KEBO_ENGAGE_SESSIONS row id.

        Mutates `payload` in place. Leaves it alone when it already holds a
        positive integer (e.g. the WhatsApp path, whose ids point at
        GHN_FF_WHATSAPP_MESSAGES instead).

        Only the NEWEST guest row for this session is considered, and it is
        accepted only when its text actually appears in the ticket's own
        transcript. A wrong anchor would draw the ticket against someone else's
        sentence, which is worse than no anchor at all — so on any doubt the
        field is dropped and the ticket files exactly as it does today.
        """
        raw = payload.get("message_id")

        # Already a usable id? leave it.
        try:
            if raw is not None and int(str(raw).strip()) > 0:
                return
        except (TypeError, ValueError):
            pass  # a UUID or junk — that is what we are here to replace

        payload.pop("message_id", None)

        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return

        # The exact sentence this ticket is about, when the builder knew it.
        anchor_text = str(payload.get("_anchor_text") or "").strip()

        # Fallback only: the full transcript. Weaker, because it also contains
        # every earlier turn — so it can never be more than a sanity check.
        haystack = "\n".join(
            str(payload.get(key) or "") for key in ("message", "issue")
        )
        if not anchor_text and not haystack.strip():
            return

        try:
            from models.database import AsyncSessionLocal
            from sqlalchemy import text as _text

            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(
                        _text(
                            "SELECT id, message FROM KEBO_ENGAGE_SESSIONS "
                            "WHERE session_id = :sid AND UPPER(sender) = 'USER' "
                            "ORDER BY id DESC LIMIT 1"
                        ),
                        {"sid": session_id},
                    )
                ).first()

            if not row:
                return
            row_id, row_message = int(row[0]), str(row[1] or "").strip()
            if row_id <= 0 or not row_message:
                return

            # The guest row must be the one this ticket is actually about.
            def _norm(text: str) -> str:
                return " ".join(text.split()).casefold()

            if anchor_text:
                # Strict: the row must BE this turn's sentence, not merely appear
                # somewhere in the transcript. This is what stops a path that writes
                # its chat row AFTER the turn (the HTTP route) from anchoring the
                # ticket to the previous message, which the transcript still holds.
                if _norm(row_message) != _norm(anchor_text):
                    logger.info(
                        "ticket message_id not anchored: newest guest row %s for "
                        "session %s is not this turn's message (row=%r turn=%r)",
                        row_id, session_id, row_message[:60], anchor_text[:60],
                    )
                    return
            elif row_message[:160] not in haystack:
                logger.info(
                    "ticket message_id not anchored: newest guest row %s for "
                    "session %s is not in this ticket's transcript",
                    row_id, session_id,
                )
                return

            payload["message_id"] = row_id
        except Exception as exc:
            # Never fail a ticket over its chat anchor.
            logger.warning("could not resolve engage message_id (non-fatal): %r", exc)

    @classmethod
    def _first_non_empty(cls, *values: Any) -> str:
        for value in values:
            text = cls._safe_str(value)
            if text:
                return text
        return ""

    @classmethod
    def _derive_generated_guest_id(
        cls,
        *,
        context: ConversationContext,
        integration: dict[str, Any],
        pending: dict[str, Any],
    ) -> str:
        seed_parts = [
            cls._safe_str(getattr(context, "session_id", "")),
            cls._safe_str(getattr(context, "hotel_code", "")),
            cls._safe_str(integration.get("conversation_id")),
            cls._safe_str(integration.get("group_id")),
            cls._safe_str(
                integration.get("user_id")
                or pending.get("user_id")
                or integration.get("wa_number")
                or pending.get("wa_number")
            ),
            cls._safe_str(
                pending.get("guest_phone")
                or integration.get("guest_phone")
                or integration.get("wa_number")
                or getattr(context, "guest_phone", "")
            ),
        ]
        seed = "|".join(part for part in seed_parts if part)
        if not seed:
            return ""
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        try:
            numeric_value = int(digest[:15], 16) % 900000000 + 100000000
        except (TypeError, ValueError):
            return ""
        return str(numeric_value)

    @classmethod
    def _extract_room_number_from_text(cls, value: Any) -> str:
        text = cls._safe_str(value)
        if not text:
            return ""
        # Scan EVERY "room <token>" occurrence and keep the FIRST token that
        # actually looks like a room number — i.e. it contains at least one
        # digit. Without the digit requirement the regex grabbed the English
        # word that merely followed "room" ("in-room DINING" -> "DINING",
        # "birthday room DECORATION" -> "DECORATION", "your room BEFORE
        # arrival" -> "BEFORE"), dumping junk into the room-number column. Using
        # finditer (not the first match) means a leading "in-room dining ..."
        # is skipped and a genuine "room 100" later in the same line still wins.
        for match in re.finditer(
            r"\broom(?:\s*(?:number|no\.?))?\s*(?:is|=|:)?\s*([A-Za-z0-9-]{1,10})\b",
            text,
            flags=re.IGNORECASE,
        ):
            candidate = cls._safe_str(match.group(1))
            if not candidate:
                continue
            if not re.fullmatch(r"[A-Za-z0-9-]{1,10}", candidate):
                continue
            # A pure word after "room" (no digit) is never a room number.
            if not any(ch.isdigit() for ch in candidate):
                continue
            return candidate.upper()
        return ""

    # Emoji / pictographic codepoint ranges the external ticketing API chokes on.
    _EMOJI_RANGES: tuple[tuple[int, int], ...] = (
        (0x1F000, 0x1FAFF),  # emoji & pictographs (supplementary planes)
        (0x2600, 0x27BF),    # misc symbols & dingbats
        (0x2B00, 0x2BFF),    # misc symbols & arrows (incl. ⭐)
        (0xFE00, 0xFE0F),    # variation selectors
        (0x1F1E6, 0x1F1FF),  # regional indicators
    )

    @classmethod
    def _sanitize_ticket_text(cls, text: Any) -> str:
        """Make free text safe + readable for the external ticketing API.

        The API rejected ticket payloads whose message carried the raw chat
        transcript (markdown, emoji, internal ``<<...>>`` display markers, HTML
        entities, control chars) — it returned ``{"id": 0}`` and created nothing.
        This strips that noise while PRESERVING multilingual letters (Hindi,
        Korean, …) so non-English tickets still go through. Whitespace-only or
        empty input returns "".
        """
        import unicodedata

        raw = str(text or "")
        if not raw.strip():
            return ""
        # Drop internal display markers like <<SHOW_BOOKING_BUTTON: Kolkata>>.
        raw = re.sub(r"<<[^>]*>>", " ", raw)
        # Un-escape the HTML entities the widget sometimes injects.
        for token, repl in (
            ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&nbsp;", " "), ("&quot;", '"'),
        ):
            raw = raw.replace(token, repl)
        # Strip markdown emphasis / heading / list / quote markers.
        raw = re.sub(r"[*_`#>]+", " ", raw)

        out_chars: list[str] = []
        for ch in raw:
            if ch in ("\n", "\t"):
                out_chars.append(ch)
                continue
            cp = ord(ch)
            if any(lo <= cp <= hi for lo, hi in cls._EMOJI_RANGES) or cp in (0x200D, 0x20E3):
                continue  # emoji / ZWJ / keycap
            # Drop other control / format chars; keep letters of every script.
            if unicodedata.category(ch)[0] == "C":
                continue
            out_chars.append(ch)
        cleaned = "".join(out_chars)
        # Collapse runs of whitespace.
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{2,}", "\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _looks_like_internal_form_dump(text: Any) -> bool:
        """True for the internal form-submission payload bubbles.

        Form submits store a structured scope dump as a "user" message, e.g.
        ``Active Property Ids: ['zone_park_jammu'] ... Service Id: support_request
        ... Entity Id: 5703``. Those are plumbing, not a real guest utterance, so
        they must never leak into the transcript we push to the external API.
        """
        t = str(text or "")
        if "Active Property Ids:" in t or "Selected Property Scope" in t:
            return True
        if "Service Id:" in t and ("Entity Id:" in t or "Full Name:" in t):
            return True
        return False

    def _build_conversation_transcript(
        self,
        context: ConversationContext,
        *,
        max_chars: int,
    ) -> str:
        """Sanitized transcript of the SINGLE chat thread that raised the ticket.

        Scoped to the active ``conversation_uid`` so a browser session holding
        many conversations only contributes the relevant one. Each turn is run
        through :meth:`_sanitize_ticket_text` (strips markdown / emoji /
        ``<<...>>`` markers / control chars) so the external API accepts it.
        Keeps the most RECENT turns when the thread is longer than ``max_chars``.
        """
        messages = list(getattr(context, "messages", None) or [])
        if not messages:
            return ""
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        active_uid = str(pending.get("_active_conversation_uid") or "").strip()

        lines: list[str] = []
        for msg in messages:
            try:
                role = str(msg.role.value)
            except Exception:
                role = str(getattr(msg, "role", "") or "")
            meta = getattr(msg, "metadata", None) or {}
            # Scope to the active chat thread only. When a thread is active, drop
            # both other-thread messages and untagged (legacy) ones so an earlier
            # conversation never bleeds into this ticket's transcript.
            if active_uid:
                msg_uid = str(meta.get("conversation_uid") or "").strip()
                if msg_uid != active_uid:
                    continue
            content = str(getattr(msg, "content", "") or "")
            if self._looks_like_internal_form_dump(content):
                continue
            clean = self._sanitize_ticket_text(content)
            if not clean:
                continue
            # "Guest" / "Bot" labels (mirrors the KePSLA conversation view) are
            # the robust turn delimiter even if the external dashboard collapses
            # newlines. Emojis/markdown are already stripped by _sanitize_ticket_text,
            # so this stays id:0-safe.
            label = "Guest" if role == "user" else "Bot"
            lines.append(f"{label}: {clean}")

        if not lines:
            return ""
        # Keep the tail (latest turns carry the actual ask) within the cap.
        # Turns are separated by a BLANK line so the single "message" field reads
        # as a clean back-and-forth conversation, not a run-on transcript.
        kept: list[str] = []
        total = 0
        for line in reversed(lines):
            if total + len(line) + 2 > max_chars and kept:
                break
            kept.append(line)
            total += len(line) + 2
        return "\n\n".join(reversed(kept)).strip()

    def _build_ticket_message_context(
        self,
        *,
        context: ConversationContext,
        explicit_message: str,
        issue_text: str,
        max_messages: int = 12,  # retained for signature compatibility (unused)
        max_chars: int = 1500,
    ) -> str:
        """Build a CLEAN, bounded ticket message for the external API.

        Default (``ticketing_external_transcript_enabled``): send the sanitized
        transcript of the one chat thread that raised the ticket, followed by the
        issue summary — so the external "API page" shows the actual messages, not
        just a one-line summary. The transcript is heavily sanitized + bounded to
        avoid the ``id:0`` rejection the old RAW-transcript approach caused.

        When the flag is off (or no transcript is available), falls back to the
        prior summary-only behavior: the explicit message (the form's structured
        detail, or the issue) plus the issue summary when it adds information.
        """
        explicit = self._sanitize_ticket_text(explicit_message)
        issue = self._sanitize_ticket_text(issue_text)

        transcript = ""
        if bool(getattr(settings, "ticketing_external_transcript_enabled", True)):
            try:
                cap = int(getattr(settings, "ticketing_external_transcript_max_chars", 4000))
            except Exception:
                cap = 4000
            cap = max(500, cap)
            # Reserve room for the trailing issue summary line.
            transcript = self._build_conversation_transcript(
                context, max_chars=max(500, cap - 300)
            )

        if transcript:
            text = transcript
            if issue and issue.lower() not in text.lower():
                text = f"{text}\nIssue Summary: {issue}"
            return text.strip()

        # Summary-only fallback.
        text = explicit or issue
        if not text:
            return ""
        # Append the issue summary when it's distinct and still fits.
        if issue and issue.lower() not in text.lower():
            candidate = f"{text}\nIssue Summary: {issue}"
            if len(candidate) <= max_chars:
                text = candidate
        return text[:max_chars].strip()

    @staticmethod
    def get_integration_context(context: ConversationContext) -> dict[str, Any]:
        """Read reserved integration context from pending_data."""
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = pending.get("_integration", {})
        return integration if isinstance(integration, dict) else {}

    def get_latest_ticket(self, context: ConversationContext) -> dict[str, Any]:
        """Fetch latest ticket snapshot from conversation memory/pending data."""
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}

        memory = pending.get("_memory", {})
        if isinstance(memory, dict):
            facts = memory.get("facts", {})
            if isinstance(facts, dict):
                latest = facts.get("latest_ticket")
                if isinstance(latest, dict):
                    return self._normalize_ticket_snapshot(latest)

        latest_from_pending = pending.get("latest_ticket")
        if isinstance(latest_from_pending, dict):
            return self._normalize_ticket_snapshot(latest_from_pending)
        return {}

    @staticmethod
    def _normalize_ticket_snapshot(ticket: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(ticket or {})
        if not normalized.get("id"):
            normalized["id"] = str(
                normalized.get("ticket_id")
                or normalized.get("ticketId")
                or normalized.get("ticketNo")
                or ""
            ).strip()
        if not normalized.get("assigned_id"):
            normalized["assigned_id"] = str(
                normalized.get("assignedId")
                or normalized.get("agent_id")
                or normalized.get("to_agent_id")
                or ""
            ).strip()
        if not normalized.get("department_id"):
            normalized["department_id"] = str(
                normalized.get("ticket_department_id")
                or normalized.get("department_allocated")
                or normalized.get("department")
                or ""
            ).strip()
        return normalized

    async def _apply_department_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Resolve a department for the payload using live entity departments.
        Preference order:
        1) LLM mapping (when available)
        2) Existing explicit department_id from payload (current behavior fallback)
        3) Deterministic keyword/name heuristic fallback
        Also remaps 'categorization' to a valid Kepsla dashboard category
        using the resolved department name as the canonical source.
        """
        payload_next = dict(payload or {})
        existing_department_id = self._safe_str(
            payload_next.get("department_allocated") or payload_next.get("department_id")
        )

        entity_id = self._first_non_empty(
            payload_next.get("entity_id"),
            payload_next.get("organisation_id"),
            payload_next.get("organization_id"),
            payload_next.get("org_id"),
        )
        if not entity_id:
            return payload_next

        try:
            from integrations.lumira_ticketing_repository import lumira_ticketing_repository
            from models.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db_session:
                departments = await lumira_ticketing_repository.fetch_departments_of_entity(
                    db_session,
                    entity_id=entity_id,
                )
                # A property whose hotel->org mapping has not been created yet
                # reaches no departments at all, and its tickets went out with
                # the field empty — the dashboard then shows no department even
                # though it keeps one we send. Department ids are global, so
                # fall back to this hotel's own allowlist by name. The ticket
                # is at least filed under the right team; it still has no
                # assignee, because staff hang off the org we could not reach.
                if not departments:
                    allowed_names = self._allowed_department_names_for(payload_next)
                    if allowed_names:
                        departments = await lumira_ticketing_repository.fetch_departments_by_name(
                            db_session,
                            names=allowed_names,
                        )
                        self._write_ticket_debug_event(
                            "ticket_department_mapping_name_fallback",
                            entity_id=entity_id,
                            allowed_names=allowed_names,
                            resolved=len(departments),
                        )
        except Exception as exc:
            self._write_ticket_debug_event(
                "ticket_department_mapping_lookup_failed",
                entity_id=entity_id,
                error=str(exc),
            )
            return payload_next

        # Enforce the per-hotel department allowlist (closed set) BEFORE picking.
        # The LLM/heuristic can only choose from this filtered list, so a ticket
        # is never assigned to a department outside the configured set — even if
        # that department has a manager. No allowlist for the hotel => unchanged.
        departments = self._filter_departments_to_allowlist(payload_next, departments)

        existing_dept: dict[str, Any] | None = None
        if existing_department_id and existing_department_id not in {"0", "null", "none"}:
            for dept in (departments or []):
                if isinstance(dept, dict) and self._safe_str(dept.get("department_id")) == existing_department_id:
                    existing_dept = dept
                    break
            if existing_dept is None:
                self._write_ticket_debug_event(
                    "ticket_department_mapping_invalid_existing_department_id",
                    entity_id=entity_id,
                    existing_department_id=existing_department_id,
                )

        llm_department = await self._llm_resolve_department_for_ticket_payload(
            payload=payload_next,
            departments=departments,
        )
        matched_department: dict[str, Any] | None = None
        mapping_source = ""

        if isinstance(llm_department, dict):
            matched_department = llm_department
            mapping_source = "llm"
        elif isinstance(existing_dept, dict):
            matched_department = existing_dept
            mapping_source = "existing_department_fallback"
        else:
            matched_department = self._resolve_department_for_ticket_payload(
                payload=payload_next,
                departments=departments,
            )
            mapping_source = "heuristic_fallback"

        if not isinstance(matched_department, dict):
            self._write_ticket_debug_event(
                "ticket_department_mapping_unresolved",
                entity_id=entity_id,
                payload=payload_next,
            )
            return payload_next

        self._write_ticket_debug_event(
            "ticket_department_mapping_resolved",
            source=mapping_source,
            entity_id=entity_id,
            matched_department=matched_department,
        )

        mapped_department_id = self._safe_str(matched_department.get("department_id"))
        if mapped_department_id:
            payload_next["department_allocated"] = mapped_department_id
            payload_next["department_id"] = mapped_department_id
        # The NAME of the department this ticket was actually assigned to. The
        # KePSLA summariser separately names a department for its one-liner,
        # and the two disagreed on roughly one ticket in five — a ticket routed
        # to Banquet was filed locally as "reservations". This is the name that
        # goes with the id we send, so it is the one the DB row should carry.
        mapped_department_name = self._safe_str(matched_department.get("department_name"))
        if mapped_department_name:
            payload_next["_mapped_department_name"] = mapped_department_name
        mapped_department_manager = self._safe_str(
            matched_department.get("department_head")
            or matched_department.get("department_name")
        )
        if mapped_department_manager:
            payload_next["department_manager"] = mapped_department_manager

        # ── Remap categorization to a valid Kepsla dashboard category ──
        payload_next = self._remap_categorization_to_accepted_value(
            payload_next, matched_department
        )
        return payload_next

    # ── Category allowlist for the external ticketing API ──────────────────
    # The Kepsla ticket API silently rejects a ticket (returns id:0, creating
    # nothing) when the `categorization` value is not one it recognises. The
    # accepted values below are confirmed from logged API responses
    # (new_bot_log_tickets): they returned a real id. The classic failure is a
    # DEPARTMENT name that is not also a valid category — e.g. "Reception Front
    # Office" (a real department) was rejected 9/9 times, while plain "Front
    # Office" is accepted. We therefore coerce every outbound category onto an
    # accepted value, falling back to a safe accepted default, so a ticket is
    # NEVER rejected purely because of its category.
    # Keys are normalized (lowercase; -, _, /, & collapsed to single spaces).
    _ACCEPTED_TICKET_CATEGORY_BY_KEY: dict[str, str] = {
        "guest relations": "Guest Relations",
        "room booking request": "room booking request",
        "room booking": "room booking request",
        "housekeeping": "Housekeeping",
        "support request": "support request",
        "business centre": "Business Centre",
        "business center": "Business Centre",
        "in room dining": "In-Room Dining",
        "front office": "Front Office",
        "cashiering billing": "Cashiering Billing",
        "spa booking": "spa booking",
        "complaint": "complaint",
        "technical support": "technical_support",
        "airport transfer": "airport transfer",
        "airport pick up": "airport transfer",
    }
    # Near-miss labels (mostly department names that are not valid categories)
    # mapped to the closest ACCEPTED category. Every target here MUST be a value
    # present in _ACCEPTED_TICKET_CATEGORY_BY_KEY above.
    _TICKET_CATEGORY_REMAP_BY_KEY: dict[str, str] = {
        "reception front office": "Front Office",
        "reception": "Front Office",
        "front desk": "Front Office",
        "billing": "Cashiering Billing",
        "cashiering": "Cashiering Billing",
        "food beverage": "In-Room Dining",
        "f b": "In-Room Dining",
        "restaurant": "In-Room Dining",
        "room service": "In-Room Dining",
        "spa wellness": "spa booking",
        "wellness": "spa booking",
        "maintenance": "technical_support",
        "it": "technical_support",
        "concierge": "Guest Relations",
        # ── THE DEPARTMENTS THIS TENANT ACTUALLY USES ────────────────────────
        # The department is resolved by code and is reliable; the model's
        # `categorization` is free text and often is not a category at all
        # ("request", "in-room_maintenance"). Engineering was missing here, so a
        # broken AC fell through to the safe default and reached staff as Guest
        # Relations instead of the team that fixes air conditioning.
        "engineering": "technical_support",
        "administration": "Guest Relations",
        # Zone's 4 requested departments (Reservations 43, F&B 34, Banquets 1,
        # Complaints 44) -> each onto a CONFIRMED-accepted category so a ticket is
        # never rejected on category. "food beverage" already maps above. The
        # dashboard shows the DEPARTMENT, not this category, so these are just
        # API-safe labels; we never send "reservations"/"banquets" raw.
        "reservations": "room booking request",
        "reservation": "room booking request",
        "banquets": "Guest Relations",
        "banquet": "Guest Relations",
        "complaints": "complaint",
    }
    # Used when nothing matches: the most common accepted category. An empty
    # category is itself accepted by the API, so empty input stays empty.
    _SAFE_DEFAULT_TICKET_CATEGORY: str = "Guest Relations"

    # ── THE SERVICE ALREADY KNOWS THE CATEGORY ───────────────────────────────
    # The category was being taken from the model's free-text guess, and anything
    # unrecognised fell to the safe default - so an AC fault was filed under
    # Guest Relations instead of Engineering, and a pre-arrival birthday cake
    # under In-Room Dining. But the service that handled the turn is known for
    # certain and travels on sub_category, so there is nothing to guess. Keyed by
    # the live service ids for this tenant.
    _CATEGORY_BY_SERVICE_ID: dict[str, str] = {
        "in_room_dining": "In-Room Dining",
        "housekeeping": "Housekeeping",
        "maintenance": "technical_support",
        "spa_booking": "spa booking",
        "arrival_transport": "airport transfer",
        "transport": "airport transfer",
        "table_reservation": "In-Room Dining",      # F&B; closest accepted value
        "meeting_banquet": "Business Centre",
        "special_requests": "Guest Relations",
        "support_prechkin": "support request",
        "support_duringstay": "support request",
        "support_postcheckout": "support request",
    }

    @classmethod
    def _normalize_category_key(cls, value: Any) -> str:
        text = cls._safe_str(value).lower().strip()
        if not text:
            return ""
        text = re.sub(r"[-_/&]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @classmethod
    def _coerce_ticket_category(cls, raw: Any, dept_name: Any = "") -> str:
        """Return a category the external ticket API accepts.

        Tries the given value, then the resolved department name, against the
        accepted allowlist and the near-miss remap. Anything else becomes the
        safe accepted default, so the API never rejects a ticket on category
        alone. Empty input stays empty (an empty category is accepted).
        """
        # DEPARTMENT FIRST. The department is resolved by code and is right; the
        # model's `categorization` is free text it makes up, and some of its
        # inventions happen to BE accepted values - "complaint" for a broken AC
        # won over department Engineering and the ticket reached staff as a
        # complaint rather than a repair. An accepted-but-wrong label is worse
        # than an unrecognised one, because nothing downstream corrects it.
        for candidate in (dept_name, raw):
            key = cls._normalize_category_key(candidate)
            if not key:
                continue
            if key in cls._ACCEPTED_TICKET_CATEGORY_BY_KEY:
                return cls._ACCEPTED_TICKET_CATEGORY_BY_KEY[key]
            if key in cls._TICKET_CATEGORY_REMAP_BY_KEY:
                return cls._TICKET_CATEGORY_REMAP_BY_KEY[key]
        # Non-empty but unrecognized -> safe accepted default.
        if cls._normalize_category_key(raw) or cls._normalize_category_key(dept_name):
            return cls._SAFE_DEFAULT_TICKET_CATEGORY
        return ""

    @classmethod
    def _remap_categorization_to_accepted_value(
        cls,
        payload: dict[str, Any],
        matched_department: dict[str, Any],
    ) -> dict[str, Any]:
        """Coerce categorization to a value the external ticketing API accepts.

        The Kepsla dashboard API rejects unknown categorization strings (returns
        ticket_id=0). Map the current value (or, when it isn't accepted, the
        resolved department name) onto the accepted allowlist, falling back to a
        safe accepted default. The original label is preserved as sub-category.
        """
        original_cat = cls._safe_str(payload.get("categorization"))
        dept_name = cls._safe_str(
            matched_department.get("department_name")
            or matched_department.get("department_head")
        ).strip()
        # ── A FOOD ORDER IS NEVER IN DOUBT ───────────────────────────────────
        # The model set categorization to "request" for an in-room dining order.
        # That is not an accepted API value, so the fallback below coerced it to
        # "Guest Relations" with department "Engineering" - a plate of naan and a
        # coffee filed to the wrong team, where the kitchen would never see it.
        # The sub_category was already `in_room_dining`, so the service is known
        # for certain; there is nothing to guess.
        _sub = cls._safe_str(payload.get("sub_categorization")
                             or payload.get("sub_category")).strip().lower()
        _by_service = (cls._CATEGORY_BY_SERVICE_ID.get(_sub)
                       or cls._CATEGORY_BY_SERVICE_ID.get(original_cat.strip().lower()))
        if _by_service:
            if original_cat.strip() != _by_service:
                payload["categorization"] = _by_service
                payload["category"] = _by_service
                if not cls._safe_str(payload.get("sub_categorization")):
                    payload["sub_categorization"] = _sub or original_cat
                    payload["sub_category"] = _sub or original_cat
                logger.info(
                    "[TICKET] %s ticket categorised as %r (model had %r) - the "
                    "service decides the category, not a guess",
                    _sub or original_cat, _by_service, original_cat)
            return payload
        coerced = cls._coerce_ticket_category(original_cat, dept_name)
        if coerced != original_cat:
            payload["categorization"] = coerced
            payload["category"] = coerced  # keep the alias in sync
            # Preserve the original service/department label as sub-category.
            if not cls._safe_str(payload.get("sub_categorization")) and original_cat:
                payload["sub_categorization"] = original_cat
                payload["sub_category"] = original_cat
            logger.info(
                "Coerced ticket category for API: '%s' -> '%s' (dept '%s')",
                original_cat, coerced, dept_name,
            )
        return payload

    async def _llm_resolve_department_for_ticket_payload(
        self,
        *,
        payload: dict[str, Any],
        departments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not bool(getattr(settings, "openai_api_key", "")):
            return None
        if not isinstance(departments, list) or not departments:
            return None

        by_id: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        normalized_departments: list[dict[str, str]] = []
        for department in departments:
            if not isinstance(department, dict):
                continue
            dep_id = self._safe_str(department.get("department_id"))
            dep_name = self._safe_str(department.get("department_name"))
            if not dep_id or not dep_name:
                continue
            normalized_departments.append(
                {
                    "department_id": dep_id,
                    "department_name": dep_name,
                }
            )
            by_id[dep_id] = department
            by_name[dep_name.lower()] = department
        if not normalized_departments:
            return None

        issue_text = self._safe_str(payload.get("issue"))
        message_text = self._safe_str(payload.get("message"))
        if len(issue_text) > 700:
            issue_text = issue_text[:700]
        if len(message_text) > 2400:
            message_text = message_text[:2400]

        prompt_payload = {
            "ticket_context": {
                "issue": issue_text,
                "message": message_text,
                "category": self._safe_str(payload.get("categorization") or payload.get("category")),
                "sub_category": self._safe_str(payload.get("sub_categorization") or payload.get("sub_category")),
                "service_id": self._safe_str(payload.get("service_id")),
                "service_name": self._safe_str(payload.get("service_name")),
                "phase": self._safe_str(payload.get("phase")),
            },
            "available_departments": normalized_departments,
        }
        system_prompt = (
            "You are a strict department mapper for hotel support tickets.\n"
            "From available_departments, choose exactly one best department for this ticket.\n"
            "Rules:\n"
            "1) You MUST select one department_id from available_departments.\n"
            "2) Never return empty department_id.\n"
            "3) Never invent IDs or names.\n"
            # ticket_context.message is the rolling chat transcript of EARLIER
            # turns in the same session — it does not contain the turn that
            # raised this ticket, and in a multi-property session it is usually
            # about someone else's request. Without this rule the model routed
            # on it: a cold-breakfast complaint was sent to Banquet with the
            # reason "Booking request for banquet hall" (the previous guest's
            # wedding enquiry), and a dinner-table request was sent to
            # complaints quoting the previous guest's front-desk complaint. It
            # stays in the prompt because a thin issue line ("please call me
            # back") needs it to be routable at all.
            "4) ticket_context.issue is THIS ticket's own description and is "
            "authoritative. ticket_context.message is earlier chat from the same "
            "session and is often about a different request entirely — read it "
            "ONLY when issue is too thin to tell what is being asked, and never "
            "let it override a clear issue.\n"
            "5) If ambiguous, choose the most operationally relevant department.\n"
            "Return ONLY JSON: {\"department_id\":\"...\",\"department_name\":\"...\",\"reason\":\"short\"}"
        )

        try:
            from llm.client import llm_client

            llm_result = await llm_client.chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
                ],
                temperature=0.0,
                trace_context={
                    "responder_type": "service",
                    "agent": "ticket_department_mapper",
                },
            )
        except Exception as exc:
            self._write_ticket_debug_event(
                "ticket_department_mapping_llm_failed",
                error=str(exc),
            )
            return None

        if not isinstance(llm_result, dict):
            return None

        selected_dep_id = self._safe_str(llm_result.get("department_id") or llm_result.get("id"))
        selected_dep_name = self._safe_str(llm_result.get("department_name"))
        if selected_dep_id and selected_dep_id in by_id:
            return by_id[selected_dep_id]
        if selected_dep_name and selected_dep_name.lower() in by_name:
            return by_name[selected_dep_name.lower()]
        return None

    @classmethod
    def _resolve_department_for_ticket_payload(
        cls,
        *,
        payload: dict[str, Any],
        departments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not isinstance(departments, list) or not departments:
            return None

        explicit_department_id = cls._safe_str(
            payload.get("department_id") or payload.get("department_allocated")
        )
        if explicit_department_id and explicit_department_id not in {"0", "null", "none"}:
            for department in departments:
                if cls._safe_str(department.get("department_id")) == explicit_department_id:
                    return department

        text = " ".join(
            cls._safe_str(payload.get(field))
            for field in (
                "issue",
                "message",
                "categorization",
                "category",
                "sub_categorization",
                "sub_category",
                "service_id",
                "service_name",
            )
        ).lower()
        if not text:
            return departments[0]

        # Direct department-name mention match.
        for department in sorted(
            departments,
            key=lambda item: len(cls._safe_str(item.get("department_name"))),
            reverse=True,
        ):
            department_name = cls._safe_str(department.get("department_name")).lower()
            if department_name and department_name in text:
                return department

        keyword_groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
            (("housekeeping", "cleaning", "towel", "linen", "bed", "laundry"), ("housekeeping",)),
            (("ac", "air conditioner", "maintenance", "plumbing", "electrical", "repair"), ("maintenance", "engineering")),
            (("food", "meal", "room service", "breakfast", "dinner", "restaurant", "dining", "menu"), ("room service", "f&b", "food", "dining", "restaurant", "ird")),
            (("spa", "wellness", "massage", "therapy"), ("spa", "wellness")),
            (("taxi", "airport", "pickup", "drop", "transport", "cab", "chauffeur"), ("transport", "concierge", "travel")),
            (("billing", "bill", "invoice", "payment", "refund"), ("front desk", "finance", "accounts", "guest relations")),
            (("booking", "reservation", "check in", "check out"), ("front office", "reception", "reservation", "sales", "guest relations")),
            (("security", "unsafe", "emergency"), ("security",)),
            (("complaint", "issue"), ("guest relations", "front desk")),
        ]

        for issue_keywords, department_keywords in keyword_groups:
            if not any(token in text for token in issue_keywords):
                continue
            for department in departments:
                department_name = cls._safe_str(department.get("department_name")).lower()
                if any(keyword in department_name for keyword in department_keywords):
                    return department

        return departments[0]

    async def _http_client(self) -> "httpx.AsyncClient":
        """One pooled client for every ticketing call, created on first use.

        Kept on the instance rather than opened per request so the TLS handshake
        to the ticketing host is paid once and the connection is reused. Rebuilt
        if it was ever closed, so a shutdown mid-process cannot wedge ticketing.
        """
        client = getattr(self, "_shared_client", None)
        if client is not None and not client.is_closed:
            return client
        lock = getattr(self, "_shared_client_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._shared_client_lock = lock
        async with lock:
            client = getattr(self, "_shared_client", None)
            if client is not None and not client.is_closed:
                return client
            client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                    keepalive_expiry=60.0,
                ),
                timeout=float(getattr(settings, "ticketing_timeout_seconds", 30.0) or 30.0),
            )
            self._shared_client = client
            return client

    @staticmethod
    async def _retry_pause(backoff: float, attempt: int) -> None:
        """Exponential backoff with jitter, so a struggling API is not stampeded."""
        import random as _random

        delay = float(backoff) * (2 ** max(0, attempt - 1))
        await asyncio.sleep(min(delay, 15.0) + _random.uniform(0.0, 0.4))

    async def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any],
        *,
        debug_context: dict[str, Any] | None = None,
        expect_ticket_id: bool = True,
    ) -> TicketingResult:
        method_upper = str(method or "POST").upper()
        timeout = float(getattr(settings, "ticketing_timeout_seconds", 10.0) or 10.0)

        # ── CARRY THE PROPERTY NAME IN A FIELD THAT SURVIVES ────────────────
        # The dashboard shows the property by resolving the entity itself, and
        # for a hotel with no row in either of its lookup tables that resolve
        # returns nothing: the ticket lands under the bare group name ("Zone
        # Hotels") with no way to tell which hotel it belongs to. We DO send
        # `propertyName`, but the API does not store it, so it never reaches
        # the screen.
        #
        # `sub_categorization` does survive, verbatim, in a varchar(100). So
        # the property goes in front of whatever it already holds — kept, not
        # replaced, so anything filtering on the old value still matches. Done
        # here rather than at payload-build time because several later passes
        # rewrite sub_categorization; this is the last point before the wire,
        # so the suffix is whatever the ticket actually ended up with.
        #
        # This does not fix the property column — that is decided server-side
        # from the entity. It stops a ticket being unattributable on screen
        # while the mapping rows are missing.
        try:
            _prop = self._safe_str(payload.get("property_name") or payload.get("propertyName"))
            if _prop:
                _sub = self._safe_str(
                    payload.get("sub_categorization") or payload.get("sub_category")
                )
                if not _sub.lower().startswith(_prop.lower()):
                    _combined = (f"{_prop} | {_sub}" if _sub else _prop)[:100]
                    payload["sub_categorization"] = _combined
                    if "sub_category" in payload:
                        payload["sub_category"] = _combined
        except Exception:
            logger.debug("property-in-sub_category tag skipped", exc_info=True)

        self._write_ticket_debug_event(
            "ticketing_http_request",
            method=method_upper,
            url=url,
            payload=payload,
            timeout_seconds=timeout,
            context=debug_context or {},
        )
        # ── ONE SLOW SPELL MUST NOT LOSE THE TICKET ─────────────────────────
        # This opened a BRAND NEW AsyncClient per call - a full TLS handshake to
        # an HTTPS endpoint on a non-standard port every time - and gave up on
        # the first exception. Measured on 2026-09-02: 14 consecutive creates
        # answered in 2.8-6.8s, then the API crossed the 10s ceiling for ~20
        # minutes and SEVEN tickets in a row were lost for good - a table
        # reservation, a wifi fault, a room change and a request for a DOCTOR -
        # while the guest was told each had been passed to the team. The API was
        # answering in 3.5s again by the time it was checked.
        #
        # So: a pooled client (connections kept alive between calls) and a
        # bounded retry with exponential backoff. Retrying a POST that may have
        # already been processed can duplicate a ticket; that trade is made
        # deliberately and is the same one `_trim_already_raised` documents - a
        # duplicate is a nuisance, a dropped request is a guest waiting for
        # something nobody is bringing. Attempts and backoff are settings, so a
        # deployment that wants the old behaviour sets attempts to 1.
        attempts = max(1, int(getattr(settings, "ticketing_max_attempts", 3) or 1))
        backoff = float(getattr(settings, "ticketing_retry_backoff_seconds", 1.5) or 1.5)
        budget = float(getattr(settings, "ticketing_total_budget_seconds", 45.0) or 45.0)
        connect_timeout = float(
            getattr(settings, "ticketing_connect_timeout_seconds", 5.0) or 5.0
        )
        # Read gets the full timeout; connecting does not. A dead host answered a
        # retry probe in 68s because each attempt burned the whole read timeout
        # just failing to connect - three of those is longer than any guest will
        # sit through.
        _timeout = httpx.Timeout(timeout, connect=min(connect_timeout, timeout))
        _started = asyncio.get_event_loop().time()
        response = None
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if attempt > 1 and (asyncio.get_event_loop().time() - _started) >= budget:
                logger.warning(
                    "Ticketing %s %s: %.1fs budget spent after %d attempt(s) — "
                    "giving up rather than keeping the guest waiting",
                    method_upper, url, budget, attempt - 1,
                )
                break
            try:
                client = await self._http_client()
                response = await client.request(
                    method_upper, url, json=payload, timeout=_timeout
                )
                # A 5xx is the server telling us it failed THIS time; the same
                # request a moment later often lands. 4xx is our payload being
                # wrong and will never improve, so it is returned as-is.
                #
                # 3xx and 429 join it. A live create came back 302 - a redirect
                # we do not follow on a POST, so the ticket was simply dropped -
                # while the two calls right after it answered 200. Whatever
                # produced it (a balancer, a session bounce) was momentary, and
                # a request nobody retries is a guest waiting on staff who were
                # never told.
                _retryable = (
                    response.status_code >= 500
                    or response.status_code == 429
                    or 300 <= response.status_code < 400
                )
                if _retryable and attempt < attempts:
                    logger.warning(
                        "Ticketing %s %s returned HTTP %s (attempt %d/%d) — retrying",
                        method_upper, url, response.status_code, attempt, attempts,
                    )
                    await self._retry_pause(backoff, attempt)
                    continue
                break
            except Exception as exc:
                last_error = exc
                # ── A READ TIMEOUT IS NOT WORTH THE GUEST'S TIME ────────────
                # Ticket creation is awaited inside the turn, so every second
                # spent here is a second the guest sits watching the typing
                # dots. A refused connection or a 302 fails in milliseconds and
                # retrying costs ~2s, which is worth it. A READ timeout has
                # already spent the full timeout doing nothing - retrying it
                # twice more turns one slow turn into a minute of silence, and
                # the request may well have been processed at the far end
                # anyway, so a retry risks a duplicate for no gain. One attempt
                # is all a read timeout gets.
                _read_timeout = isinstance(exc, httpx.ReadTimeout)
                if attempt < attempts and not _read_timeout:
                    logger.warning(
                        "Ticketing request failed (%s) on attempt %d/%d — retrying: %r",
                        method_upper, attempt, attempts, exc,
                    )
                    self._write_ticket_debug_event(
                        "ticketing_http_retry",
                        method=method_upper, url=url, attempt=attempt,
                        attempts=attempts, error=str(exc),
                        context=debug_context or {},
                    )
                    await self._retry_pause(backoff, attempt)
                    continue
                logger.exception(
                    "Ticketing request failed after %d attempt(s): %s %s",
                    attempts, method_upper, url,
                )
                self._write_ticket_debug_event(
                    "ticketing_http_exception",
                    method=method_upper,
                    url=url,
                    payload=payload,
                    error=str(exc),
                    attempts=attempts,
                    context=debug_context or {},
                )
                return TicketingResult(
                    success=False,
                    error=str(exc),
                    payload=dict(payload or {}),
                )
        if response is None:
            return TicketingResult(
                success=False,
                error=str(last_error or "ticketing_request_failed"),
                payload=dict(payload or {}),
            )

        raw = self._safe_json(response)
        success = 200 <= response.status_code < 300
        ticket_id = self._extract_first_value(raw, ("ticket_id", "ticketId", "id", "ticketNo", "ticket_number"))
        # Only ticket-create/update responses are expected to carry a ticket_id.
        # Handoff responses never do — they return queue_status / no_agent_available.
        if expect_ticket_id and str(ticket_id or "").strip() in ("0", ""):
            ticket_id = ""
            if success:
                success = False
                logger.warning(
                    "Ticket API returned HTTP 200 but ticket_id is 0/empty — treating as failure (url=%s)",
                    url,
                )
        assigned_id = self._extract_first_value(raw, ("assignedId", "assigned_id", "agent_id", "to_agent_id"))
        error = ""
        if not success:
            extracted = self._extract_first_value(raw, ("error", "message", "detail"))
            if extracted:
                error = extracted
            else:
                # Avoid storing raw HTML error pages as the error message
                text = str(response.text or "").strip()
                if text and "<html" in text.lower():
                    error = f"External API returned HTTP {response.status_code}"
                else:
                    error = text or f"HTTP {response.status_code}"
        self._write_ticket_debug_event(
            "ticketing_http_response",
            method=method_upper,
            url=url,
            status_code=response.status_code,
            success=success,
            ticket_id=ticket_id,
            assigned_id=assigned_id,
            error=error,
            response_body=raw,
            context=debug_context or {},
        )

        return TicketingResult(
            success=success,
            ticket_id=ticket_id,
            assigned_id=assigned_id,
            status_code=response.status_code,
            error=error,
            payload=dict(payload or {}),
            response=raw,
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except Exception:
            text = str(response.text or "").strip()
            return {"raw": text}

        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
        return {"value": parsed}

    @classmethod
    def _extract_first_value(cls, data: Any, preferred_keys: tuple[str, ...]) -> str:
        """Depth-first search to find first non-empty scalar by key preference."""
        if not preferred_keys:
            return ""

        stack: list[Any] = [data]
        visited: set[int] = set()
        while stack:
            current = stack.pop()
            current_id = id(current)
            if current_id in visited:
                continue
            visited.add(current_id)

            if isinstance(current, dict):
                for key in preferred_keys:
                    if key in current and current[key] not in (None, ""):
                        return str(current[key]).strip()
                for value in current.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)

            elif isinstance(current, list):
                for item in current:
                    if isinstance(item, (dict, list)):
                        stack.append(item)
                    elif item not in (None, "") and len(preferred_keys) == 1:
                        return str(item).strip()
        return ""

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        return normalize_identifier(value)

    @classmethod
    def _normalize_phase_identifier(cls, value: Any) -> str:
        normalized = cls._normalize_identifier(value).replace("-", "_")
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

    @staticmethod
    def _service_aliases_for_matching(service: dict[str, Any]) -> list[str]:
        aliases: list[str] = []
        if not isinstance(service, dict):
            return aliases
        for field in ("id", "name", "type", "description", "cuisine"):
            value = str(service.get(field) or "").strip()
            if value and value not in aliases:
                aliases.append(value)
        return aliases

    @staticmethod
    def _service_alias_match_score(message_text: str, message_tokens: list[str], alias: str) -> float:
        alias_text = str(alias or "").strip().lower()
        if not alias_text:
            return 0.0
        if alias_text in message_text:
            return 0.98

        alias_tokens = [token for token in re.findall(r"[a-z0-9]+", alias_text) if len(token) >= 3]
        if not alias_tokens:
            return 0.0
        if not message_tokens:
            return 0.0

        hits = 0
        for token in alias_tokens:
            token_hit = False
            for msg_token in message_tokens:
                if msg_token == token:
                    token_hit = True
                    break
                if len(token) >= 5 and len(msg_token) >= 4 and (
                    msg_token.startswith(token[:4]) or token.startswith(msg_token[:4])
                ):
                    token_hit = True
                    break
                if len(token) >= 5 and len(msg_token) >= 4 and SequenceMatcher(a=token, b=msg_token).ratio() >= 0.86:
                    token_hit = True
                    break
            if token_hit:
                hits += 1
        if hits <= 0:
            return 0.0
        return hits / max(1, len(alias_tokens))

    def _match_service_for_ticket_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None

        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            return None

        payload_phase = self._normalize_phase_identifier(payload.get("phase"))
        explicit_service_id = self._normalize_identifier(payload.get("service_id") or payload.get("service"))
        explicit_service_name = str(payload.get("service_name") or "").strip().lower()
        message_blob = " ".join(
            str(payload.get(field) or "").strip().lower()
            for field in (
                "service_name",
                "service",
                "service_id",
                "issue",
                "message",
                "sub_category",
                "categorization",
                "category",
                "ticket_sub_category",
            )
            if str(payload.get(field) or "").strip()
        ).strip()
        if not message_blob and not explicit_service_id and not explicit_service_name:
            return None

        message_tokens = re.findall(r"[a-z0-9]+", message_blob)
        best_service: dict[str, Any] | None = None
        best_score = 0.0

        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue

            service_phase = self._normalize_phase_identifier(service.get("phase_id"))
            if payload_phase and service_phase and service_phase != payload_phase:
                continue

            service_id = self._normalize_identifier(service.get("id"))
            service_name = str(service.get("name") or "").strip().lower()

            score = 0.0
            if explicit_service_id and service_id and explicit_service_id == service_id:
                score = 1.0
            elif explicit_service_name and service_name and (
                explicit_service_name == service_name
                or explicit_service_name in service_name
                or service_name in explicit_service_name
            ):
                score = 0.98
            else:
                for alias in self._service_aliases_for_matching(service):
                    score = max(
                        score,
                        self._service_alias_match_score(message_blob, message_tokens, alias),
                    )

            if score > best_score:
                best_score = score
                best_service = service

        if best_service is None:
            return None
        if best_score < 0.72:
            return None

        return {
            "service_id": str(best_service.get("id") or "").strip(),
            "service_name": str(best_service.get("name") or best_service.get("id") or "").strip(),
            "service_phase_id": self._normalize_phase_identifier(best_service.get("phase_id")),
            "ticketing_enabled": bool(best_service.get("ticketing_enabled", True)),
            "match_score": best_score,
            "payload_phase_id": payload_phase,
        }

    def detect_service_ticketing_disabled(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        matched = self._match_service_for_ticket_payload(payload)
        if not isinstance(matched, dict):
            return None
        if bool(matched.get("ticketing_enabled", True)):
            return None
        return {
            "service_id": str(matched.get("service_id") or ""),
            "service_name": str(matched.get("service_name") or ""),
            "service_phase_id": str(matched.get("service_phase_id") or ""),
            "payload_phase_id": str(matched.get("payload_phase_id") or ""),
            "match_score": float(matched.get("match_score") or 0.0),
        }

    @staticmethod
    def _normalize_priority(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "MEDIUM"
        mapping = {
            "critical": "CRITICAL",
            "urgent": "CRITICAL",
            "high": "HIGH",
            "medium": "MEDIUM",
            "med": "MEDIUM",
            "low": "LOW",
        }
        return mapping.get(raw, "MEDIUM")

    @staticmethod
    def _normalize_category(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "request"
        mapping = {
            "request": "request",
            "complaint": "complaint",
            "upsell": "upsell",
            "inquiry": "inquiry",
            "enquiry": "inquiry",
            "conversation": "inquiry",
            "order": "request",
            "food_order": "request",
            "food order": "request",
            "in-room dining": "request",
            "dining": "request",
            "maintenance": "complaint",
            "emergency": "complaint",
        }
        return mapping.get(raw, raw)

    @classmethod
    def _normalize_phase(cls, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "During Stay"

        if raw in {"Booking", "Pre Checkin", "During Stay", "Post Checkout", "Pre Booking"}:
            return raw

        normalized_key = cls._normalize_identifier(raw).replace("-", "_")
        mapping = {
            "booking": "Booking",
            "pre_checkin": "Pre Checkin",
            "precheckin": "Pre Checkin",
            "during_stay": "During Stay",
            "duringstay": "During Stay",
            "in_stay": "During Stay",
            "instay": "During Stay",
            "post_checkout": "Post Checkout",
            "postcheckout": "Post Checkout",
            "pre_booking": "Pre Booking",
            "prebooking": "Pre Booking",
        }
        return mapping.get(normalized_key, raw)

    @staticmethod
    def _normalize_ticket_status(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "open"
        mapping = {
            "open": "open",
            "in_progress": "in_progress",
            "in progress": "in_progress",
            "closed": "closed",
            "cancel": "cancel",
            "cancelled": "cancel",
            "canceled": "cancel",
            "breached": "breached",
        }
        return mapping.get(raw, "open")

    @staticmethod
    def _default_phase_for_context(integration: dict[str, Any]) -> str:
        flow = str(integration.get("flow") or integration.get("bot_mode") or "").strip().lower()
        if flow in {"engage", "booking", "booking_bot"}:
            return "Pre Booking"
        return "During Stay"

    @staticmethod
    def _source_from_channel(channel: str | None) -> str:
        normalized = str(channel or "").strip().lower()
        if normalized in {"whatsapp", "wa"}:
            return "whatsapp_bot"
        # ── THE WIDGET'S OWN CHANNEL NAMES ──────────────────────────────────
        # This is the branch the web widget actually lands on: most ticket
        # paths pass no explicit source, so resolution falls through to the
        # channel — which is "web_widget" for every /ws/chat connection. It
        # returned "manual", meaning a ticket our own bot raised was filed as
        # if a human had typed it into the dashboard, and the chat-history page
        # therefore had no conversation to draw it against.
        if normalized in {"web_widget", "web", "widget", "chat", "chat_bot", "webchat"}:
            return "booking_bot"
        return "manual"

    @classmethod
    def _resolve_ticket_source(
        cls,
        *,
        explicit_source: str,
        integration: dict[str, Any],
        channel: str | None,
    ) -> str:
        def normalize_source(raw_source: str) -> str | None:
            normalized_source = cls._normalize_identifier(raw_source).replace("-", "_")
            mapping = {
                "whatsapp": "whatsapp_bot",
                "wa": "whatsapp_bot",
                "whatsapp_bot": "whatsapp_bot",
                "booking": "booking_bot",
                "booking_bot": "booking_bot",
                "engage": "booking_bot",
                "taskly": "taskly",
                "manual": "manual",
                # ── THE WEB WIDGET IS THE BOOKING BOT ────────────────────────
                # These four all mean "our web chat widget raised this". They
                # were absent from the map (or pointed at "manual"), and the
                # `.get(..., "manual")` default silently swallowed them — so
                # every widget ticket was filed as if a human had typed it into
                # the dashboard by hand. The dashboard uses ticket_source to
                # decide WHICH message store `message_id` points at:
                # whatsapp_bot -> GHN_FF_WHATSAPP_MESSAGES, booking_bot ->
                # KEBO_ENGAGE_SESSIONS, manual -> no conversation at all. Filed
                # as "manual" the ticket has no chat to hang off, which is why
                # it reached the tickets page but never drew inline in the
                # conversation on the chat-history page. Verified against prod:
                # booking_bot is the ONLY source that ever carries a real
                # message_id (1280/1280 rows), and 1188 of those resolve to a
                # KEBO_ENGAGE_SESSIONS row in the ticket's own session_id.
                "chat_bot": "booking_bot",
                "web": "booking_bot",
                "web_widget": "booking_bot",
                "widget": "booking_bot",
            }
            # None — NOT "manual" — when the value is not one of the four the
            # dashboard understands. Callers pass INTERNAL labels here
            # ("room_booking_intent", "llm_orchestration", "complaint_handler",
            # …) that describe which code path raised the ticket, not which
            # channel the guest used. Defaulting those to "manual" meant a
            # ticket our own bot raised was filed as if a human had typed it
            # into the dashboard — and every new handler label silently
            # inherited the same bug. An unrecognised label now defers to the
            # channel below, which is the thing that actually knows.
            return mapping.get(normalized_source)

        explicit = normalize_source(str(explicit_source or ""))
        if explicit:
            return explicit

        from_integration = str(
            integration.get("ticket_source")
            or integration.get("source")
            or ""
        ).strip()
        if from_integration:
            from_integration_source = normalize_source(from_integration)
            if from_integration_source:
                return from_integration_source

        flow = str(integration.get("flow") or integration.get("bot_mode") or "").strip().lower()
        if flow in {"engage", "booking", "booking_bot"}:
            return "booking_bot"

        return normalize_source(cls._source_from_channel(channel)) or "manual"

    @staticmethod
    def _ticketing_base_url() -> str:
        return str(settings.ticketing_base_url or "").strip().rstrip("/")

    @staticmethod
    def _use_local_mode() -> bool:
        return bool(getattr(settings, "ticketing_local_mode", False))

    @staticmethod
    def _allow_api_failure_local_fallback() -> bool:
        """
        Allow local fallback only when explicitly enabled.
        This prevents exposing LOCAL-* ticket IDs as successful external tickets.
        """
        return bool(getattr(settings, "ticketing_api_failure_local_fallback_enabled", False))

    @staticmethod
    def _local_store_path() -> Path:
        raw = str(getattr(settings, "ticketing_local_store_file", "") or "").strip()
        if not raw:
            raw = "./data/ticketing/local_tickets.json"
        return Path(raw)

    def _local_csv_path(self) -> Path:
        raw = str(getattr(settings, "ticketing_local_csv_file", "") or "").strip()
        if raw:
            return Path(raw)
        store_path = self._local_store_path()
        if store_path.suffix.lower() == ".json":
            return store_path.with_suffix(".csv")
        return store_path.parent / f"{store_path.name}.csv"

    @staticmethod
    def _default_local_store() -> dict[str, Any]:
        return {"next_id": 1, "tickets": []}

    def _normalize_local_store_data(self, raw_store: Any) -> dict[str, Any]:
        if not isinstance(raw_store, dict):
            return self._default_local_store()

        raw_tickets = raw_store.get("tickets")
        tickets: list[dict[str, Any]] = []
        if isinstance(raw_tickets, list):
            for index, item in enumerate(raw_tickets, start=1):
                if not isinstance(item, dict):
                    continue
                record = dict(item)
                ticket_id = self._safe_str(record.get("ticket_id") or record.get("id")) or f"LOCAL-{index}"
                record["id"] = ticket_id
                record["ticket_id"] = ticket_id
                if not isinstance(record.get("payload"), dict):
                    record["payload"] = {}
                created_at = self._safe_str(record.get("created_at")) or datetime.now(UTC).isoformat()
                updated_at = self._safe_str(record.get("updated_at")) or created_at
                record["created_at"] = created_at
                record["updated_at"] = updated_at
                if not self._safe_str(record.get("status")):
                    record["status"] = self._safe_str(record["payload"].get("ticket_status")) or "open"
                if not self._safe_str(record.get("mode")):
                    record["mode"] = "local_simulation"
                tickets.append(record)

        max_numeric_id = 0
        for index, record in enumerate(tickets, start=1):
            numeric_id = self._extract_local_numeric_id(record.get("ticket_id") or record.get("id"), fallback=index)
            if numeric_id > max_numeric_id:
                max_numeric_id = numeric_id

        next_id = max_numeric_id + 1 if tickets else 1
        raw_next = raw_store.get("next_id")
        if isinstance(raw_next, int) and raw_next > next_id:
            next_id = raw_next
        return {"next_id": next_id, "tickets": tickets}

    def _rebuild_local_store_from_csv(self, *, reason: str) -> dict[str, Any]:
        csv_path = self._local_csv_path()
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            logger.warning(
                "Local ticket JSON store unusable (%s) and CSV recovery file missing/empty at %s",
                reason,
                csv_path,
            )
            return self._default_local_store()

        recovered_tickets: list[dict[str, Any]] = []
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    if not isinstance(row, dict):
                        continue
                    ticket_id = self._safe_str(row.get("ticket_id") or row.get("id"))
                    if not ticket_id:
                        continue

                    payload: dict[str, Any] = {}
                    payload_json = self._safe_str(row.get("payload_json"))
                    if payload_json:
                        try:
                            parsed_payload = json.loads(payload_json)
                            if isinstance(parsed_payload, dict):
                                payload = parsed_payload
                        except Exception:
                            payload = {}

                    if not payload:
                        payload = {
                            "session_id": self._safe_str(row.get("session_id")),
                            "guest_id": self._safe_str(row.get("guest_id")),
                            "room_number": self._safe_str(row.get("room_number")),
                            "issue": self._safe_str(row.get("issue")),
                            "message": self._safe_str(row.get("message") or row.get("issue")),
                            "ticket_status": self._safe_str(row.get("status") or "open"),
                            "categorization": self._safe_str(row.get("category")),
                            "sub_categorization": self._safe_str(row.get("sub_category")),
                            "source": self._safe_str(row.get("ticket_source")),
                        }

                    created_at = self._safe_str(
                        row.get("local_created_at_utc")
                        or row.get("created_at")
                    ) or datetime.now(UTC).isoformat()
                    updated_at = self._safe_str(
                        row.get("local_updated_at_utc")
                        or row.get("updated_at")
                    ) or created_at

                    recovered_tickets.append(
                        {
                            "id": ticket_id,
                            "ticket_id": ticket_id,
                            "status": self._safe_str(row.get("status") or payload.get("ticket_status")) or "open",
                            "issue": self._safe_str(row.get("issue") or payload.get("issue")),
                            "manager_notes": self._safe_str(row.get("manager_notes") or payload.get("manager_notes")),
                            "room_number": self._safe_str(row.get("room_number") or payload.get("room_number")),
                            "guest_id": self._safe_str(row.get("guest_id") or payload.get("guest_id")),
                            "assigned_id": self._safe_str(row.get("assigned_id") or payload.get("assigned_to")),
                            "created_at": created_at,
                            "updated_at": updated_at,
                            "payload": payload,
                            "mode": self._safe_str(row.get("mode")) or "local_simulation",
                        }
                    )
        except Exception:
            logger.exception("Failed to recover local ticket JSON store from CSV at %s", csv_path)
            return self._default_local_store()

        normalized = self._normalize_local_store_data({"tickets": recovered_tickets})
        logger.warning(
            "Recovered local ticket JSON store from CSV at %s (reason=%s, recovered_tickets=%s)",
            csv_path,
            reason,
            len(normalized.get("tickets", [])),
        )
        return normalized

    def _load_local_store(self) -> dict[str, Any]:
        path = self._local_store_path()
        if not path.exists():
            recovered = self._rebuild_local_store_from_csv(reason="json_missing")
            if recovered.get("tickets"):
                try:
                    self._save_local_store(recovered)
                except Exception:
                    logger.exception("Failed to persist recovered local ticket JSON store to %s", path)
            return recovered

        try:
            raw_text = path.read_text(encoding="utf-8")
            if not str(raw_text).strip():
                recovered = self._rebuild_local_store_from_csv(reason="json_empty")
                if recovered.get("tickets"):
                    try:
                        self._save_local_store(recovered)
                    except Exception:
                        logger.exception("Failed to persist recovered local ticket JSON store to %s", path)
                return recovered

            parsed = json.loads(raw_text)
            normalized = self._normalize_local_store_data(parsed)
            self._bootstrap_local_csv_if_needed(normalized.get("tickets", []))
            return normalized
        except Exception:
            logger.exception("Failed to read local ticket store at %s", path)
            recovered = self._rebuild_local_store_from_csv(reason="json_parse_error")
            if recovered.get("tickets"):
                try:
                    self._save_local_store(recovered)
                except Exception:
                    logger.exception("Failed to persist recovered local ticket JSON store to %s", path)
            return recovered

    def _save_local_store(self, data: dict[str, Any]) -> None:
        path = self._local_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        normalized = self._normalize_local_store_data(data)
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
        logger.info(
            "Local ticket JSON store saved: path=%s tickets=%s next_id=%s",
            path,
            len(normalized.get("tickets", [])),
            normalized.get("next_id"),
        )

    @staticmethod
    def _safe_str(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _mask_identifier(value: Any, keep: int = 4) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) <= keep:
            return "*" * len(text)
        return ("*" * (len(text) - keep)) + text[-keep:]

    def _extract_local_numeric_id(self, value: Any, fallback: int) -> int:
        raw = self._safe_str(value)
        if raw.isdigit():
            return int(raw)
        if raw.upper().startswith("LOCAL-"):
            numeric = raw.split("-", 1)[1].strip()
            if numeric.isdigit():
                return int(numeric)
        return fallback

    def _build_local_csv_row(
        self,
        *,
        local_id: int,
        ticket_id: str,
        payload: dict[str, Any],
        mode: str,
        local_created_at: str,
        local_updated_at: str,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_safe = payload if isinstance(payload, dict) else {}
        response_safe = response if isinstance(response, dict) else {}

        department_alloc = self._safe_str(
            payload_safe.get("department_allocated") or payload_safe.get("department_id")
        )
        category = self._safe_str(payload_safe.get("categorization") or payload_safe.get("category"))
        sub_category = self._safe_str(
            payload_safe.get("sub_categorization") or payload_safe.get("sub_category")
        )
        status = self._safe_str(payload_safe.get("ticket_status")) or "open"
        ticket_source = self._safe_str(payload_safe.get("source") or payload_safe.get("ticket_source"))
        entity_id = self._safe_str(
            payload_safe.get("entity_id")
            or payload_safe.get("organisation_id")
            or payload_safe.get("organization_id")
            or payload_safe.get("org_id")
        )
        assigned_to = self._safe_str(payload_safe.get("assigned_to"))
        assigned_id = self._safe_str(
            payload_safe.get("assigned_id")
            or payload_safe.get("assignedId")
            or response_safe.get("assigned_id")
            or response_safe.get("assignedId")
            or assigned_to
        )

        return {
            "id": local_id,
            "ticket_id": ticket_id,
            "session_id": self._safe_str(payload_safe.get("session_id")),
            "guest_id": self._safe_str(payload_safe.get("guest_id")),
            "entity_id": entity_id,
            "fms_entity_id": self._safe_str(payload_safe.get("fms_entity_id")),
            "issue": self._safe_str(payload_safe.get("issue")),
            "message_id": self._safe_str(payload_safe.get("message_id")),
            "sentiment_score": self._safe_str(payload_safe.get("sentiment_score")),
            "department_alloc": department_alloc,
            "priority": self._safe_str(payload_safe.get("priority")),
            "category": category,
            "phase": self._safe_str(payload_safe.get("phase")),
            "status": status,
            "sla_due": self._safe_str(payload_safe.get("sla_due_time")),
            "escalation_stage": self._safe_str(payload_safe.get("escalation_stage") or 0),
            "created_at": self._safe_str(payload_safe.get("created_at")) or local_created_at,
            "updated_at": local_updated_at,
            "room_number": self._safe_str(payload_safe.get("room_number")),
            "message": self._safe_str(payload_safe.get("message")),
            "customer_feedback": self._safe_str(payload_safe.get("customer_feedback")),
            "manager_notes": self._safe_str(payload_safe.get("manager_notes")),
            "assignee_notes": self._safe_str(payload_safe.get("assignee_notes")),
            "assigned_to": assigned_to,
            "assigned_id": assigned_id,
            "customer_rating": self._safe_str(payload_safe.get("customer_rating")),
            "department_id": self._safe_str(payload_safe.get("department_id") or department_alloc),
            "sla_due_at": self._safe_str(payload_safe.get("sla_due_at")),
            "sla_duration_minutes": self._safe_str(payload_safe.get("sla_duration_minutes")),
            "ticket_auto_assign": self._safe_str(payload_safe.get("ticket_auto_assign") or 0),
            "closed_at": self._safe_str(payload_safe.get("closed_at")),
            "outlet_id": self._safe_str(payload_safe.get("outlet_id")),
            "input_tokens": self._safe_str(payload_safe.get("input_tokens") or 0),
            "output_tokens": self._safe_str(payload_safe.get("output_tokens") or 0),
            "total_tokens": self._safe_str(payload_safe.get("total_tokens") or 0),
            "cost": self._safe_str(payload_safe.get("cost") or 0),
            "sub_category": sub_category,
            "guest_name": self._safe_str(payload_safe.get("guest_name")),
            "property_name": self._safe_str(payload_safe.get("property_name")),
            "room_type": self._safe_str(payload_safe.get("room_type")),
            "compensation_type": self._safe_str(payload_safe.get("compensation_type")),
            "compensation_currency": self._safe_str(payload_safe.get("compensation_currency")),
            "compensation_amount": self._safe_str(payload_safe.get("compensation_amount") or 0),
            "group_id": self._safe_str(payload_safe.get("group_id")),
            "ticket_source": ticket_source,
            "cancelled_notes": self._safe_str(payload_safe.get("cancelled_notes")),
            "mode": mode,
            "local_created_at_utc": local_created_at,
            "local_updated_at_utc": local_updated_at,
            "payload_json": json.dumps(payload_safe, ensure_ascii=False),
            "response_json": json.dumps(response_safe, ensure_ascii=False),
        }

    def _append_local_ticket_csv_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        csv_path = self._local_csv_path()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = csv_path.exists() and csv_path.stat().st_size > 0
        with csv_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self._LOCAL_TICKET_CSV_HEADERS))
            if not file_exists:
                writer.writeheader()
            for row in rows:
                writer.writerow({header: row.get(header, "") for header in self._LOCAL_TICKET_CSV_HEADERS})

    def _rewrite_local_csv_from_tickets(self, tickets: list[dict[str, Any]]) -> tuple[bool, Path]:
        """
        Rewrite local CSV from JSON store to keep both stores fully in sync.
        This prevents drift when append operations fail or are interrupted.
        """
        csv_path = self._local_csv_path()
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        rows: list[dict[str, Any]] = []
        for index, ticket in enumerate(tickets or [], start=1):
            if not isinstance(ticket, dict):
                continue
            payload = ticket.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            ticket_id = self._safe_str(ticket.get("ticket_id") or ticket.get("id")) or f"LOCAL-{index}"
            local_id = self._extract_local_numeric_id(ticket.get("id") or ticket_id, fallback=index)
            local_created_at = self._safe_str(ticket.get("created_at")) or datetime.now(UTC).isoformat()
            local_updated_at = self._safe_str(ticket.get("updated_at")) or local_created_at
            rows.append(
                self._build_local_csv_row(
                    local_id=local_id,
                    ticket_id=ticket_id,
                    payload=payload,
                    mode=self._safe_str(ticket.get("mode")) or "local_simulation",
                    local_created_at=local_created_at,
                    local_updated_at=local_updated_at,
                    response={"status": self._safe_str(ticket.get("status")) or "open"},
                )
            )

        try:
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self._LOCAL_TICKET_CSV_HEADERS))
                writer.writeheader()
                for row in rows:
                    writer.writerow({header: row.get(header, "") for header in self._LOCAL_TICKET_CSV_HEADERS})
            logger.info(
                "Local ticket CSV synced: path=%s rows=%s",
                csv_path,
                len(rows),
            )
            return True, csv_path
        except PermissionError:
            # Fallback mirror file when primary CSV is locked by another process.
            mirror_path = csv_path.with_name(f"{csv_path.stem}_mirror{csv_path.suffix}")
            with mirror_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self._LOCAL_TICKET_CSV_HEADERS))
                writer.writeheader()
                for row in rows:
                    writer.writerow({header: row.get(header, "") for header in self._LOCAL_TICKET_CSV_HEADERS})
            logger.warning(
                "Local ticket CSV primary path locked; wrote mirror file instead: mirror_path=%s rows=%s",
                mirror_path,
                len(rows),
            )
            return False, mirror_path

    def _bootstrap_local_csv_if_needed(self, existing_tickets: list[dict[str, Any]]) -> None:
        csv_path = self._local_csv_path()
        if csv_path.exists() and csv_path.stat().st_size > 0:
            return
        if not existing_tickets:
            return

        rows: list[dict[str, Any]] = []
        for index, ticket in enumerate(existing_tickets, start=1):
            if not isinstance(ticket, dict):
                continue
            payload = ticket.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            ticket_id = self._safe_str(ticket.get("ticket_id") or ticket.get("id"))
            local_id = self._extract_local_numeric_id(ticket.get("id") or ticket_id, fallback=index)
            local_created_at = self._safe_str(ticket.get("created_at")) or datetime.now(UTC).isoformat()
            local_updated_at = self._safe_str(ticket.get("updated_at")) or local_created_at
            rows.append(
                self._build_local_csv_row(
                    local_id=local_id,
                    ticket_id=ticket_id or f"LOCAL-{local_id}",
                    payload=payload,
                    mode=self._safe_str(ticket.get("mode")) or "local_simulation",
                    local_created_at=local_created_at,
                    local_updated_at=local_updated_at,
                    response={"status": "backfilled_from_json"},
                )
            )

        self._append_local_ticket_csv_rows(rows)

    async def _enrich_guest_name_from_pms(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Fill an empty ``guest_name`` from the PMS guest list keyed by entity +
        room number, for kebo hotels only.

        During_stay tickets collect the room number, not the guest's name, so the
        external dashboard shows a blank name. The name lives in
        ``GHN_FF_PROD.GHN_FF_GUEST_INFO`` (``ENTITY_ID`` == the FMS id we push as
        ``organisation_id``; matched on ``ROOM_NUMBER``, active stay preferred).
        No-op when a name is already present, the hotel isn't a kebo hotel, or no
        room/entity is known. Best-effort — never raises, so a lookup miss (or an
        unpopulated PMS room) just leaves the name blank as before.
        """
        try:
            existing = str(payload.get("guest_name") or payload.get("guestName") or "").strip()
            if existing:
                return payload
            room = str(payload.get("room_number") or "").strip()
            entity = str(payload.get("organisation_id") or "").strip()
            if not room or not entity:
                return payload
            try:
                entity_int = int(entity)
            except (TypeError, ValueError):
                return payload

            # Kebo hotels only — KePSLA / other tenants keep their existing behavior.
            from services.customer_details_service import customer_details_service as _cds
            from services.db_config_service import db_config_service as _dcs

            hotel_code = str(_dcs.get_current_hotel_code() or "").strip()
            widget_id, _group_id = _cds.resolve_widget_group(hotel_code)
            if not widget_id:
                return payload

            from models.database import AsyncSessionLocal
            from sqlalchemy import text as _text

            resolved_name = ""
            async with AsyncSessionLocal() as db_session:
                row = (
                    await db_session.execute(
                        _text(
                            """
                            SELECT GUEST_NAME
                            FROM GHN_FF_PROD.GHN_FF_GUEST_INFO
                            WHERE ENTITY_ID = :entity
                              AND TRIM(ROOM_NUMBER) = :room
                              AND GUEST_NAME IS NOT NULL
                              AND TRIM(GUEST_NAME) <> ''
                            ORDER BY
                                CASE WHEN NOW() BETWEEN CHECK_IN_DATE AND CHECK_OUT_DATE
                                     THEN 0 ELSE 1 END,
                                UPDATED_DATE DESC
                            LIMIT 1
                            """
                        ),
                        {"entity": entity_int, "room": room},
                    )
                ).first()
                if row:
                    resolved_name = str(row[0] or "").strip()

            if resolved_name:
                payload["guest_name"] = resolved_name
                payload["guestName"] = resolved_name
                self._write_ticket_debug_event(
                    "ticket_guest_name_resolved_from_pms",
                    entity_id=entity,
                    room_number=room,
                    guest_name=resolved_name,
                )
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment
            logger.debug("guest-name PMS enrichment skipped: %r", exc)
        return payload

    # ── DB persistence (dual-write) ───────────────────────────────────────────

    async def _save_ticket_to_db(
        self, payload: dict[str, Any], result: "TicketingResult"
    ) -> None:
        """Best-effort write of a ticket to new_bot_tickets (KePSLA DB).

        Always runs regardless of whether the external Lumira API succeeded.
        If Lumira is down, the KePSLA record will have ticket_ref=None but all
        form data is preserved. Never raises.
        """
        try:
            from models.database import AsyncSessionLocal, Ticket, Hotel, Conversation, Guest
            from sqlalchemy import select as _select
            from services.db_config_service import db_config_service as _dcs

            session_id = str(payload.get("session_id") or "").strip()

            # Resolve hotel_code from the most-reliable source first.
            # The bug we're guarding against: callers historically pass
            # `entity_id` (Lumira numeric tenant ID like "5703") in the payload
            # but rarely set a Kebo `hotel_code` (string like "KHIL"). If we
            # write "5703" into new_bot_tickets.hotel_code, the admin UI's
            # property-scoped filter (which uses the Kebo string code) won't
            # match the row and the ticket appears missing.
            #
            # Order of preference:
            #   1. payload.hotel_code        — explicit Kebo code from caller
            #   2. db_config contextvar      — pushed by every chat handler
            #   3. session_id → conversation — fall-back lookup
            #   4. payload.entity_id         — last resort (legacy numeric)
            hotel_code = str(payload.get("hotel_code") or "").strip().upper()
            if not hotel_code:
                ctx_code = str(_dcs.get_current_hotel_code() or "").strip().upper()
                if ctx_code and ctx_code != "DEFAULT":
                    hotel_code = ctx_code

            async with AsyncSessionLocal() as db_session:
                # Resolve hotel_id from hotel_code
                hotel_id = None
                if hotel_code:
                    row = await db_session.execute(
                        _select(Hotel.id).where(Hotel.code == hotel_code)
                    )
                    hotel_id = row.scalar_one_or_none()

                # Resolve conversation_id from session_id
                conv_id = None
                if session_id:
                    row = await db_session.execute(
                        _select(Conversation.id).where(Conversation.session_id == session_id)
                    )
                    conv_id = row.scalar_one_or_none()

                # If hotel still unresolved, fall back to the conversation's hotel_code.
                if not hotel_id and conv_id:
                    row = await db_session.execute(
                        _select(Conversation.hotel_code).where(Conversation.id == conv_id)
                    )
                    conv_hotel = (row.scalar_one_or_none() or "").strip().upper()
                    if conv_hotel:
                        hotel_code = conv_hotel
                        row = await db_session.execute(
                            _select(Hotel.id).where(Hotel.code == hotel_code)
                        )
                        hotel_id = row.scalar_one_or_none()

                # Absolute last resort — keep the legacy numeric tenant id so
                # the row isn't orphaned with NULL hotel_code. The admin UI's
                # property filter won't show it, but the All scope still will.
                if not hotel_code:
                    hotel_code = str(payload.get("entity_id") or "").strip().upper()

                # Resolve guest_db_id by phone
                guest_db_id = None
                guest_phone = str(payload.get("guest_phone") or "").strip()
                if hotel_id and guest_phone:
                    row = await db_session.execute(
                        _select(Guest.id).where(
                            Guest.hotel_id == hotel_id,
                            Guest.phone_number == guest_phone,
                        )
                    )
                    guest_db_id = row.scalar_one_or_none()

                # Parse SLA datetime if present
                sla_due_at = None
                sla_raw = str(payload.get("sla_due_time") or "").strip()
                if sla_raw:
                    try:
                        from datetime import datetime as _dt
                        sla_due_at = _dt.fromisoformat(sla_raw.replace("Z", "+00:00"))
                    except Exception:
                        pass

                ticket = Ticket(
                    ticket_ref=str(result.ticket_id or "").strip() or None,
                    hotel_id=hotel_id,
                    conversation_id=conv_id,
                    guest_db_id=guest_db_id,
                    session_id=session_id or None,
                    conversation_uid=str(payload.get("_conversation_uid") or "").strip() or None,
                    guest_name=(
                        str(payload.get("guest_name") or payload.get("guestName") or "").strip() or None
                    ),
                    guest_id=str(payload.get("guest_id") or "").strip() or None,
                    room_number=str(payload.get("room_number") or "").strip() or None,
                    hotel_code=hotel_code or None,
                    property_name=(
                        str(payload.get("property_name") or payload.get("propertyName") or "").strip() or None
                    ),
                    source=str(payload.get("source") or payload.get("ticket_source") or "web").strip(),
                    issue=str(payload.get("issue") or "").strip() or None,
                    message=str(payload.get("message") or "").strip() or None,
                    # Mirror the KePSLA one-line description already generated
                    # up-front (and pushed to the external API as the issue), so
                    # the dashboard row matches without a second LLM call.
                    description=str(payload.get("_kepsla_description") or "").strip() or None,
                    category=str(payload.get("categorization") or payload.get("category") or "complaint").strip(),
                    sub_category=(
                        str(payload.get("sub_categorization") or payload.get("sub_category") or "").strip() or None
                    ),
                    phase=str(payload.get("phase") or "").strip() or None,
                    priority=str(payload.get("priority") or "medium").strip().lower(),
                    status=str(payload.get("ticket_status") or "open").strip().lower(),
                    department=(
                        str(
                            # The department actually assigned wins over the
                            # summariser's own guess at one: they are separate
                            # LLM calls and they disagree, and this row should
                            # say where the ticket really went.
                            payload.get("_mapped_department_name")
                            or payload.get("_kepsla_department")
                            or payload.get("department_allocated")
                            or payload.get("department_id")
                            or ""
                        ).strip()
                        or None
                    ),
                    assignee=str(payload.get("assigned_to") or "").strip() or None,
                    manager_notes=str(payload.get("manager_notes") or "").strip() or None,
                    is_escalated=False,
                    sla_due_at=sla_due_at,
                    sentiment_score=str(payload.get("sentiment_score") or "").strip() or None,
                    lumira_payload=payload,
                    lumira_response=(
                        result.response
                        if isinstance(result.response, dict)
                        else None
                    ),
                )
                db_session.add(ticket)
                await db_session.commit()
                await db_session.refresh(ticket)
                # The hotel has it now, whatever the external API said.
                result.saved_to_dashboard = True
                new_ticket_id = int(ticket.id) if ticket.id is not None else 0
                # Mark this session as having a ticket so the per-turn cancel/edit
                # interceptor knows to look (and can skip the DB query otherwise).
                if session_id:
                    self._sessions_with_tickets.add(str(session_id).strip())

            # Dashboard guest identity: mirror the resolved name into
            # GHN_CHAT_BOT_CUSTOMER_DETAILS (keyed by Session_Id) for kebo hotels
            # so the dashboard shows a name against conversationally-raised tickets
            # too — not just form submits. Upsert per session; PROCESSED so the
            # Java email job skips it. Only when a real name is known (resolved
            # from PMS or captured on the ticket). Best-effort; never fatal.
            try:
                from services.customer_details_service import customer_details_service as _cds_t

                _cd_name = str(payload.get("guest_name") or payload.get("guestName") or "").strip()
                _cd_widget, _cd_group = _cds_t.resolve_widget_group(hotel_code)
                if _cd_widget and session_id and _cd_name:
                    _cds_t.write_bg(
                        session_id=session_id,
                        conversation_id=str(payload.get("_conversation_uid") or "").strip() or session_id,
                        widget_id=_cd_widget,
                        group_id=_cd_group,
                        guest_name=_cd_name,
                        phone=str(payload.get("guest_phone") or "").strip(),
                        property_name=str(
                            payload.get("property_name") or payload.get("propertyName") or ""
                        ).strip(),
                        topic=str(payload.get("issue") or "").strip(),
                        processing_status=str(
                            getattr(settings, "customer_details_processing_status", "PROCESSED")
                            or "PROCESSED"
                        ),
                    )
            except Exception:
                logger.debug("ticket-create customer_details write skipped", exc_info=True)

            # Fire-and-forget: generate description + KePSLA department via LLM.
            # Skipped when the description was already generated up-front (see
            # _apply_kepsla_description) — the row is already filled.
            already_summarized = bool(str(payload.get("_kepsla_description") or "").strip())
            if new_ticket_id and not already_summarized:
                try:
                    asyncio.create_task(
                        self._fill_kepsla_summary_for_ticket(new_ticket_id, dict(payload or {}))
                    )
                except RuntimeError:
                    # No running event loop (rare): run inline, still best-effort.
                    await self._fill_kepsla_summary_for_ticket(new_ticket_id, dict(payload or {}))
        except Exception as exc:
            logger.warning("Failed to save ticket to DB (non-fatal): %s", exc)

    async def _create_ticket_local(self, payload: dict[str, Any]) -> TicketingResult:
        self._write_ticket_debug_event(
            "local_ticket_create_start",
            local_store_path=str(self._local_store_path()),
            local_csv_path=str(self._local_csv_path()),
            payload=payload,
        )
        logger.info(
            "Local ticket create requested: session_id=%s issue=%s category=%s sub_category=%s",
            self._safe_str((payload or {}).get("session_id")),
            self._safe_str((payload or {}).get("issue"))[:160],
            self._safe_str((payload or {}).get("categorization") or (payload or {}).get("category")),
            self._safe_str((payload or {}).get("sub_categorization") or (payload or {}).get("sub_category")),
        )
        store = self._load_local_store()
        next_id = int(store.get("next_id") or 1)
        ticket_id = f"LOCAL-{next_id}"
        now_iso = datetime.now(UTC).isoformat()

        record = {
            "id": ticket_id,
            "ticket_id": ticket_id,
            "status": str(payload.get("ticket_status") or "open").strip().lower() or "open",
            "issue": str(payload.get("issue") or "").strip(),
            "manager_notes": str(payload.get("manager_notes") or "").strip(),
            "room_number": str(payload.get("room_number") or "").strip(),
            "guest_id": str(payload.get("guest_id") or "").strip(),
            "assigned_id": str(payload.get("assigned_to") or "").strip(),
            "created_at": now_iso,
            "updated_at": now_iso,
            "payload": dict(payload or {}),
            "mode": "local_simulation",
        }

        tickets = list(store.get("tickets") or [])
        logger.info(
            "Local ticket store loaded for create: tickets=%s next_id=%s",
            len(tickets),
            next_id,
        )
        tickets.append(record)
        store["tickets"] = tickets
        store["next_id"] = next_id + 1
        try:
            self._save_local_store(store)
        except Exception as exc:
            logger.exception("Failed to persist local ticket create")
            return TicketingResult(
                success=False,
                error=str(exc),
                payload=dict(payload or {}),
            )

        response = {
            "status": "success",
            "ticket_id": ticket_id,
            "id": ticket_id,
            "mode": "local_simulation",
            "ticket_record": dict(record),
        }

        csv_written = True
        csv_primary_synced = True
        csv_path_used = self._local_csv_path()
        try:
            csv_primary_synced, csv_path_used = self._rewrite_local_csv_from_tickets(store["tickets"])
        except Exception:
            logger.exception("Failed to sync local ticket CSV from JSON store")
            csv_written = False

        response["csv_written"] = csv_written
        response["csv_primary_synced"] = csv_primary_synced
        response["csv_path"] = str(csv_path_used)
        logger.info(
            "Local ticket create completed: ticket_id=%s total_tickets=%s csv_written=%s csv_primary_synced=%s csv_path=%s",
            ticket_id,
            len(store.get("tickets") or []),
            csv_written,
            csv_primary_synced,
            csv_path_used,
        )
        self._write_ticket_debug_event(
            "local_ticket_create_completed",
            success=True,
            ticket_id=ticket_id,
            total_tickets=len(store.get("tickets") or []),
            csv_written=csv_written,
            csv_primary_synced=csv_primary_synced,
            csv_path=str(csv_path_used),
            local_store_path=str(self._local_store_path()),
            local_csv_path=str(self._local_csv_path()),
            response=response,
        )
        return TicketingResult(
            success=True,
            ticket_id=ticket_id,
            assigned_id=record["assigned_id"],
            status_code=200,
            payload=dict(payload or {}),
            response=response,
        )

    async def _update_ticket_local(
        self,
        ticket_id: str,
        manager_notes: str,
        *,
        extra_fields: dict[str, Any] | None = None,
    ) -> TicketingResult:
        self._write_ticket_debug_event(
            "local_ticket_update_start",
            ticket_id=ticket_id,
            manager_notes=manager_notes,
            extra_fields=extra_fields or {},
            local_store_path=str(self._local_store_path()),
            local_csv_path=str(self._local_csv_path()),
        )
        logger.info(
            "Local ticket update requested: ticket_id=%s manager_notes_chars=%s",
            ticket_id,
            len(str(manager_notes or "")),
        )
        store = self._load_local_store()
        tickets = list(store.get("tickets") or [])
        target_index = -1
        for idx, item in enumerate(tickets):
            if not isinstance(item, dict):
                continue
            current_id = str(item.get("id") or item.get("ticket_id") or "").strip()
            if current_id == ticket_id:
                target_index = idx
                break

        if target_index < 0:
            logger.warning("Local ticket update failed: ticket not found ticket_id=%s", ticket_id)
            return TicketingResult(
                success=False,
                error=f"Local ticket not found: {ticket_id}",
                ticket_id=ticket_id,
                status_code=404,
            )

        record = dict(tickets[target_index])
        existing_notes = str(record.get("manager_notes") or "").strip()
        if existing_notes:
            record["manager_notes"] = f"{existing_notes}\n{manager_notes}"
        else:
            record["manager_notes"] = manager_notes
        record["updated_at"] = datetime.now(UTC).isoformat()

        if isinstance(extra_fields, dict):
            for key, value in extra_fields.items():
                if value is not None:
                    record[key] = value

        tickets[target_index] = record
        store["tickets"] = tickets
        try:
            self._save_local_store(store)
        except Exception as exc:
            logger.exception("Failed to persist local ticket update")
            return TicketingResult(
                success=False,
                error=str(exc),
                ticket_id=ticket_id,
                status_code=500,
            )

        csv_written = True
        csv_primary_synced = True
        csv_path_used = self._local_csv_path()
        try:
            csv_primary_synced, csv_path_used = self._rewrite_local_csv_from_tickets(store["tickets"])
        except Exception:
            logger.exception("Failed to sync local ticket CSV after update")
            csv_written = False

        response = {
            "status": "success",
            "ticket_id": ticket_id,
            "id": ticket_id,
            "manager_notes": record.get("manager_notes"),
            "mode": "local_simulation",
            "csv_written": csv_written,
            "csv_primary_synced": csv_primary_synced,
            "csv_path": str(csv_path_used),
        }
        logger.info(
            "Local ticket update completed: ticket_id=%s csv_written=%s csv_primary_synced=%s csv_path=%s",
            ticket_id,
            csv_written,
            csv_primary_synced,
            csv_path_used,
        )
        self._write_ticket_debug_event(
            "local_ticket_update_completed",
            success=True,
            ticket_id=ticket_id,
            csv_written=csv_written,
            csv_primary_synced=csv_primary_synced,
            csv_path=str(csv_path_used),
            local_store_path=str(self._local_store_path()),
            local_csv_path=str(self._local_csv_path()),
            response=response,
        )
        return TicketingResult(
            success=True,
            ticket_id=ticket_id,
            assigned_id=str(record.get("assigned_id") or "").strip(),
            status_code=200,
            payload={"manager_notes": manager_notes},
            response=response,
        )

    # ── KePSLA-side LLM summary (description + department) ───────────────────

    KEPSLA_DEPARTMENTS: tuple[str, ...] = (
        "Reception Front Office",
        "Guest Relations",
        "Housekeeping",
        "In-Room Dining",
    )

    async def _apply_kepsla_description(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Generate the KePSLA one-line description up-front so the SAME text
        shown on the KePSLA page is pushed to the external ticketing API.

        Stashes the resolved description + KePSLA department on internal
        (underscore-prefixed) payload keys. The caller uses _kepsla_description
        as the `issue` on the OUTBOUND API request, and _save_ticket_to_db
        persists it to new_bot_tickets.description — without a second LLM call.

        The payload's own `issue` is deliberately left untouched so the
        duplicate-suppression fingerprint stays stable (the LLM summary is not
        guaranteed byte-identical across calls). Best-effort: returns the
        payload unchanged when disabled, when the LLM is unavailable, or on any
        failure (the original issue then stands and the DB description is filled
        by the existing fire-and-forget fallback).
        """
        if not bool(getattr(settings, "ticketing_kepsla_description_to_api_enabled", True)):
            return payload
        try:
            summary = await self._generate_kepsla_summary(payload)
        except Exception as exc:
            logger.warning("KePSLA description pre-generation failed (non-fatal): %s", exc)
            return payload
        if not isinstance(summary, dict):
            return payload
        description = self._safe_str(summary.get("description"))
        if not description:
            return payload

        payload_next = dict(payload)
        payload_next["_kepsla_description"] = description
        kepsla_department = self._safe_str(summary.get("department"))
        if kepsla_department:
            payload_next["_kepsla_department"] = kepsla_department

        self._write_ticket_debug_event(
            "ticket_kepsla_description_applied",
            description=description,
            kepsla_department=kepsla_department,
            original_issue=self._safe_str(payload.get("issue")),
        )
        return payload_next

    @staticmethod
    def _missing_booking_summary_facts(description: str, facts: dict[str, Any]) -> list[str]:
        missing = []
        for key in ("check_in", "check_out"):
            value = str(facts.get(key) or "").strip()
            if value and value not in description:
                missing.append(f"{key}={value}")
        guests = str(facts.get("guests") or "").strip()
        if guests and not re.search(
            rf"(?:\b{re.escape(guests)}\s+(?:guests?|people|persons?|adults?|pax)\b|"
            rf"\b(?:party|group)\s+of\s+{re.escape(guests)}\b)", description, re.I,
        ):
            missing.append(f"guests={guests}")
        return missing

    async def _generate_kepsla_summary(
        self, payload: dict[str, Any]
    ) -> dict[str, str] | None:
        """Use gpt-4o-mini to produce a tiny staff-facing description and pick one
        of the four KePSLA departments.  Returns {description, department} or None
        on failure. Never raises.
        """
        if not bool(getattr(settings, "openai_api_key", "")):
            return None
        try:
            issue = self._safe_str(payload.get("issue"))[:400]
            category = self._safe_str(payload.get("categorization") or payload.get("category"))
            sub_category = self._safe_str(
                payload.get("sub_categorization") or payload.get("sub_category")
            )
            phase = self._safe_str(payload.get("phase"))
            service_name = self._safe_str(payload.get("service_name"))
            property_name = self._safe_str(
                payload.get("property_name") or payload.get("propertyName")
            )
            room_type = self._safe_str(payload.get("room_type") or payload.get("roomType"))
            room_number = self._safe_str(payload.get("room_number"))

            # Base the description ONLY on THIS ticket's request (`issue`), never
            # on the full conversation transcript. Previously we fed
            # payload["message"] here — that is the whole sanitized chat thread
            # (_build_ticket_message_context). In a long during-stay thread where
            # the guest made many different requests, the summariser latched onto
            # an EARLIER ask: a "call the doctor" ticket was described as "fresh
            # towels to room 421", a candlelight-massage ticket as "laundry
            # closet", and the wrong topic even mis-picked the department
            # (doctor -> Housekeeping). The `issue` line already IS the concrete
            # reason this ticket was raised, so the summary must key off it alone.
            ctx = {
                "request": issue,
                "category": category,
                "sub_category": sub_category,
                "phase": phase,
                "service_name": service_name,
                "property_name": property_name,
                "room_type": room_type,
                "room_number": room_number,
            }
            booking_facts = payload.get("_booking_facts")
            if not isinstance(booking_facts, dict):
                booking_facts = {}
            if booking_facts:
                ctx["required_booking_facts"] = booking_facts

            # Department options: prefer THIS hotel's own configured departments
            # (the real team names it routes tickets to, from its ticketing
            # allowlist) so tickets are tagged with departments that actually
            # exist for the property. Falls back to the 4 generic labels for a
            # hotel with no allowlist configured. Per-hotel by construction — each
            # hotel uses its own list, so hotels are independent.
            allowed = list(self.KEPSLA_DEPARTMENTS)
            try:
                _raw = str(getattr(settings, "ticketing_allowed_departments_json", "") or "").strip()
                _hc = self._safe_str(payload.get("hotel_code")).strip().lower()
                if not _hc:
                    try:
                        from services.db_config_service import db_config_service
                        _hc = str(db_config_service.get_current_hotel_code() or "").strip().lower()
                    except Exception:
                        _hc = ""
                from services import prebooking_settings_service as _pbs

                _admin_depts = _pbs.allowed_departments_overlay().get(_hc) if _hc else None
                if _admin_depts:
                    # Departments set in the admin (Bot Training -> Pre-booking).
                    allowed = list(_admin_depts)
                    _raw = ""
                if _raw and _hc:
                    _parsed = json.loads(_raw)
                    if isinstance(_parsed, dict):
                        for _k, _v in _parsed.items():
                            if str(_k).strip().lower() == _hc and isinstance(_v, list):
                                _names = [str(n).strip() for n in _v if str(n).strip()]
                                if _names:
                                    allowed = _names
                                break
            except Exception:
                logger.debug("kepsla summary: per-hotel dept options lookup failed", exc_info=True)
            system_prompt = (
                "You are a hotel-staff ticket summarizer. From the ticket context, "
                "produce two things:\n"
                "1) description: ONE short sentence (max 25 words) describing what the guest needs, "
                "based SOLELY on the `request` field (this ticket's actual request). Do NOT introduce "
                "any topic that is not in `request`. Concrete, neutral, staff-facing. No greetings. No fluff.\n"
                "   KEEP THE ACTIONABLE SPECIFICS. If `request` states dates, times, a party size, a "
                "room or table number, a room type or a quantity, carry them into the sentence — they "
                "are what staff act on, and a summary that drops them makes the ticket useless. Trim "
                "wording, never facts; go slightly over 25 words rather than lose one.\n"
                "2) department: pick EXACTLY ONE label from THIS hotel's department list — "
                f"{allowed}. Match the guest's need to the CLOSEST department in that list by "
                "MEANING (department names differ per hotel). Routing guide:\n"
                "- Transport / airport or railway pickup or drop / shuttle / cab / arrival logistics; "
                "spa & wellness requests; celebration / decoration / special in-room setups; room "
                "upgrades; early or late check-in; and general 'talk to staff' / callbacks / requests "
                "the front desk coordinates -> the FRONT OFFICE / reception / front-desk department.\n"
                "- Food, meals, dietary requests (veg/Jain/vegan), room service, restaurant, bar -> "
                "the FOOD & BEVERAGE / dining / in-room dining department.\n"
                "- Cleaning, linen, towels, amenity replenishment, housekeeping -> the HOUSEKEEPING department.\n"
                "- Maintenance faults (AC, plumbing, electrical, TV, Wi-Fi hardware, repairs) -> the "
                "ENGINEERING / maintenance department.\n"
                "- Billing, invoices, GST, accounts, general admin -> the ADMINISTRATION / accounts department "
                "(or the front-office one if there is no admin department).\n"
                "If nothing clearly fits, choose the front-office / reception department.\n"
                'Return ONLY JSON: {"description":"...","department":"..."}'
            )
            from llm.client import llm_client
            if booking_facts:
                system_prompt += (
                    "\nThe required_booking_facts are verified form values for this request. "
                    "Include BOTH dates exactly as YYYY-MM-DD and the numeric guest count "
                    "with the word guests. Write the sentence naturally; preserve these facts."
                )
            summary_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)},
            ]
            for attempt in range(2 if booking_facts else 1):
                llm_result = await llm_client.chat_with_json(
                    messages=summary_messages,
                    model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                    temperature=0.0, max_tokens=200,
                    trace_context={"responder_type": "service", "agent": "ticket_kepsla_summary"},
                )
                if not isinstance(llm_result, dict):
                    return None
                missing = self._missing_booking_summary_facts(
                    self._safe_str(llm_result.get("description"))[:500], booking_facts,
                )
                if not missing:
                    break
                if attempt == 1:
                    # Leave the original complete request intact on failure.
                    logger.warning("Booking summary omitted required fields: %s", missing)
                    return None
                summary_messages.extend([
                    {"role": "assistant", "content": json.dumps(llm_result)},
                    {"role": "user", "content": "Rewrite the summary, preserving these missing facts: " + ", ".join(missing)},
                ])
            if not isinstance(llm_result, dict):
                return None
            description = self._safe_str(llm_result.get("description"))[:500]
            department_raw = self._safe_str(llm_result.get("department"))
            # Strict membership check against THIS hotel's department options
            # (case-insensitive). If the model returned something off-list, fall
            # back to the first option (the front-office / reception department,
            # which is listed first for every property).
            department = ""
            for allowed_label in allowed:
                if department_raw.lower() == allowed_label.lower():
                    department = allowed_label
                    break
            if not department:
                department = allowed[0] if allowed else "Reception Front Office"
            if not description:
                return None
            return {"description": description, "department": department}
        except Exception as exc:
            logger.warning("KePSLA summary LLM call failed (non-fatal): %s", exc)
            return None

    async def _fill_kepsla_summary_for_ticket(
        self, ticket_db_id: int, payload: dict[str, Any]
    ) -> None:
        """Generate description + department via LLM and UPDATE the ticket row.
        Best-effort: never raises, runs as a background task.
        """
        try:
            summary = await self._generate_kepsla_summary(payload)
            if not summary:
                return
            from models.database import AsyncSessionLocal, Ticket
            async with AsyncSessionLocal() as db_session:
                row = await db_session.get(Ticket, int(ticket_db_id))
                if not row:
                    return
                row.description = summary.get("description") or None
                dept = summary.get("department") or None
                if dept:
                    row.department = dept
                await db_session.commit()
        except Exception as exc:
            logger.warning(
                "Failed to fill KePSLA summary for ticket %s (non-fatal): %s",
                ticket_db_id, exc,
            )


ticketing_service = TicketingService()
