import asyncio
import queue
import sys
from contextlib import suppress
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import models.database as database_module
from config.settings import settings
from core.context_manager import ContextManager
from services.customer_details_service import CustomerDetailsService
from services.db_log_sink import DBLogSink
from services.engage_session_service import EngageSessionService


def _drain_one(sink: DBLogSink) -> tuple[str, dict]:
    rows = sink._drain(drain_all=True)
    assert len(rows) == 1
    return rows[0]


def test_db_log_sink_promotes_nested_backend_correlation_fields() -> None:
    sink = DBLogSink()
    sink.emit(
        "observability",
        {
            "event": "chat_message_processed",
            "payload": {
                "session_id": "sess-backend",
                "hotel_code": "zone",
                "trace_id": "trc-123",
                "turn_trace_id": "turn-456",
            },
        },
    )

    table, row = _drain_one(sink)
    assert table == "backend"
    assert row["session_id"] == "sess-backend"
    assert row["hotel_code"] == "zone"
    assert row["trace_id"] == "trc-123"
    assert row["turn_trace_id"] == "turn-456"


def test_db_log_sink_maps_conversation_phase_without_clobbering_phase_object() -> None:
    sink = DBLogSink()
    sink.emit(
        "conversation_audit",
        {
            "phase": {"resolved_phase_id": "during_stay"},
            "intent": "service_routed",
        },
        user_message="Need towels",
        bot_message="Sending housekeeping",
        phase_id="during_stay",
    )

    table, row = _drain_one(sink)
    assert table == "messages"
    assert row["phase"] == "during_stay"
    assert row["user_text"] == "Need towels"
    assert row["bot_text"] == "Sending housekeeping"


def test_db_log_sink_promotes_ticket_fields_from_nested_payload() -> None:
    sink = DBLogSink()
    sink.emit(
        "ticketing",
        {
            "event": "ticket_create_api_completed",
            "ticket_id": "T-1001",
            "payload": {
                "session_id": "sess-ticket",
                "hotel_code": "zone",
                "service_id": "spa_booking",
                "phase": "during_stay",
                "ticket_status": "open",
            },
        },
    )

    table, row = _drain_one(sink)
    assert table == "tickets"
    assert row["session_id"] == "sess-ticket"
    assert row["hotel_code"] == "zone"
    assert row["ticket_ref"] == "T-1001"
    assert row["service_id"] == "spa_booking"
    assert row["status"] == "open"
    assert row["stage"] == "ticket_create_api_completed"


def test_db_log_sink_stats_expose_queue_pressure_metrics() -> None:
    sink = DBLogSink()
    sink._maxsize = 1
    sink._q = queue.Queue(maxsize=1)

    sink.emit("observability", {"event": "one"})
    sink.emit("observability", {"event": "two"})

    stats = sink.stats()
    assert stats["queued"] == 1
    assert stats["dropped"] == 1
    assert stats["dropped_queue_full"] == 1
    assert stats["maxsize"] == 1


def test_customer_details_merge_updates_existing_session_row_without_losing_data() -> None:
    params = CustomerDetailsService().build_params(
        session_id="sess-cd",
        guest_name="",
        email="new@example.com",
        phone="",
        topic="airport transfer",
    )
    existing = {
        "Conversation_Id": "conv-1",
        "Widget_Id": 53,
        "Group_Id": 244,
        "Form_Id": None,
        "Form_Type": "ci_form",
        "Guest_Name": "Sana Shaaista",
        "First_Name": "Sana",
        "Last_Name": "Shaaista",
        "Email_Id": "",
        "Mobile_number": "+91 9999999999",
        "Country": "India",
        "City": "Goa",
        "Property_Name": "Zone",
        "Topic": "room booking request",
        "Consent_Given": 1,
        "Others": '{"existing":true}',
        "Processing_Status": "PROCESSED",
    }

    merged = CustomerDetailsService._merge_existing_params(params, existing)

    assert merged["guest_name"] == "Sana Shaaista"
    assert merged["mobile"] == "+91 9999999999"
    assert merged["email"] == "new@example.com"
    assert merged["topic"] == "airport transfer"
    assert merged["widget_id"] == 53


@pytest.mark.asyncio
async def test_set_turn_display_payload_prefers_matching_conversation_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ContextManager(local_store_enabled=False)
    executed: list[tuple[str, dict]] = []

    class _Result:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _Session:
        async def execute(self, stmt, params):
            sql = str(stmt)
            executed.append((sql, dict(params)))
            if "SELECT m.id" in sql and "m.conversation_uid = :cuid" in sql:
                return _Result((42,))
            if "SELECT m.id" in sql:
                return _Result(None)
            return _Result(None)

        async def commit(self):
            executed.append(("COMMIT", {}))

    class _SessionCM:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(database_module, "AsyncSessionLocal", lambda: _SessionCM())

    await manager.set_turn_display_payload(
        "sess-display",
        {"display_message": "Hello"},
        conversation_uid="thread-2",
        db_session=None,
    )

    assert any(
        "m.conversation_uid = :cuid" in sql and params.get("cuid") == "thread-2"
        for sql, params in executed
    )
    assert any(
        sql.startswith("UPDATE new_bot_messages SET display_payload")
        and params.get("mid") == 42
        for sql, params in executed
    )


@pytest.mark.asyncio
async def test_engage_session_queue_is_bounded_and_tracks_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "engage_session_queue_maxsize", 1)
    service = EngageSessionService()
    blocker = asyncio.Event()

    async def fake_drain() -> None:
        await blocker.wait()

    monkeypatch.setattr(service, "_drain", fake_drain)

    service.write_turn_bg(session_id="sess-1", sender="USER", message="hi")
    service.write_turn_bg(session_id="sess-2", sender="USER", message="hello")

    stats = service.stats()
    assert stats["queued_now"] == 1
    assert stats["dropped"] == 1
    assert stats["maxsize"] == 1

    blocker.set()
    if service._worker is not None:
        service._worker.cancel()
        with suppress(asyncio.CancelledError):
            await service._worker
