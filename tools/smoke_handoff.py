"""
Smoke test: hit the dev rep-dashboard handoff API directly with the new payload
shape (including widget_id). Run from the new_bot project root:

    python tools/smoke_handoff.py

Confirms three things:
1. AGENT_HANDOFF_API_URL is set in .env
2. The HTTP call succeeds (no 5xx / connection error)
3. The dashboard returns something parseable
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Make the project root importable when invoked directly.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings  # noqa: E402
from services.ticketing_service import ticketing_service  # noqa: E402


async def main() -> int:
    url = (settings.agent_handoff_api_url or "").strip()
    print(f"[smoke] AGENT_HANDOFF_API_URL = {url!r}")
    if not url:
        print("[smoke] FAIL: AGENT_HANDOFF_API_URL not configured")
        return 2

    result = await ticketing_service.handoff_to_agent(
        conversation_id="smoke-conv-001",
        session_id="smoke-sess-001",
        reason="Smoke test from new_bot: validating widget_id field",
        agent_id="0",
        widget_id="5",
    )

    print("[smoke] success      :", result.success)
    print("[smoke] status_code  :", result.status_code)
    print("[smoke] error        :", result.error)
    print("[smoke] payload sent :", json.dumps(result.payload, indent=2))
    print("[smoke] response body:", json.dumps(result.response, indent=2, default=str)[:1200])
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
