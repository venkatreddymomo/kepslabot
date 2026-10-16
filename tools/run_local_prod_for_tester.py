"""Run THIS checkout against the PROD database so a tester can try un-deployed fixes (via ngrok).

    python tools/run_local_prod_for_tester.py            # port 8019, real tickets (same as live)
    STUB_TICKETS=1 python tools/run_local_prod_for_tester.py   # no real tickets / staff alerts
    ngrok http 8019                                      # then share <ngrok url>/chat?widget_key=...

Why not plain `python main.py`: its startup (lifespan) re-seeds the SHARED prompt rows from the local
prompt files, runs DB migrations and regenerates service prompts - against prod that can change Zone
and Sarovar. This launcher skips all of that and only starts the DB log writer, tagging the rows
source_env='local' so tester turns can be found and told apart from the live server's.
"""
import os
import sys
from contextlib import asynccontextmanager

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("BOT_ENV_FILE", ".env")
os.environ["DB_LOGGING_SOURCE_ENV"] = "local"
os.environ.pop("ADMIN_DEV_URL", None)  # serve the built admin_ui/dist chat page

import uvicorn  # noqa: E402

from config.settings import settings  # noqa: E402
from main import app  # noqa: E402
from services.db_log_sink import db_log_sink  # noqa: E402

PORT = int(os.environ.get("PORT", "8019"))
STUB_TICKETS = os.environ.get("STUB_TICKETS", "").strip() in {"1", "true", "yes"}


@asynccontextmanager
async def _logging_only_lifespan(_app):
    db_log_sink.start()
    try:
        yield
    finally:
        await db_log_sink.stop()

app.router.lifespan_context = _logging_only_lifespan

if STUB_TICKETS:
    from services.ticketing_service import ticketing_service, TicketingResult
    import api.routes.chat as chat_routes

    async def _stub_create_ticket(payload):
        print(f"[STUB] ticket NOT created: {payload.get('service_id')} {str(payload.get('issue'))[:70]!r}", flush=True)
        return TicketingResult(success=True, ticket_id="LOCAL-TEST", status_code=200, payload=payload)

    async def _stub_update_ticket(**kw):
        print(f"[STUB] ticket NOT updated: {kw.get('ticket_ref')}", flush=True)
        return TicketingResult(success=True, ticket_id=str(kw.get("ticket_ref") or ""), status_code=200)

    async def _skip_booking_record(**_kw):
        return {}

    ticketing_service.create_ticket = _stub_create_ticket
    ticketing_service.update_ticket_details = _stub_update_ticket
    chat_routes._persist_prebooking_booking_if_possible = _skip_booking_record

if __name__ == "__main__":
    db_host = str(settings.database_url).split("@")[-1].split("/")[0]
    print(f"Local test server on http://127.0.0.1:{PORT}  | DB: {db_host}  | tickets: {'STUBBED' if STUB_TICKETS else 'REAL'}")
    print(f"Tester link: <ngrok url>/chat?widget_key=wk_KkRabdHTRYylT-eZ7Nu_1RiS&phase=pre_booking")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
