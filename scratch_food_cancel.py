import asyncio, sys
sys.path.insert(0, ".")
from services.llm_orchestration_service import llm_orchestration_service as S, ConversationContext
from services.ticketing_service import ticketing_service as T
from datetime import datetime, timezone

CALLS = []
class R: success = True
async def fake_append(**kw): CALLS.append(kw); return R()

FOOD = {"ticket_ref": "10900",
        "issue": "In-room dining order for room 1400: 1 Masala Dosa",
        "category": "Food & Beverage", "sub_category": "in_room_dining",
        "description": "Guest ordered 1 Masala Dosa to room 1400.",
        "room_number": "1400",
        "created_at": datetime.now(timezone.utc).isoformat()}

def ctx():
    c = ConversationContext(session_id="t_food", hotel_code="SAROVAR_PORTICO_SRIPERUMBUDUR")
    c.pending_action = ""; c.pending_data = {}; c.suspended_services = []
    return c

async def run(msg, rows, expect):
    T.list_session_tickets = lambda sid, limit=15: asyncio.sleep(0, result=list(rows))
    T.session_has_known_tickets = lambda sid: True
    T.session_cancelled_refs = lambda sid: set()
    T._staff_have_picked_up = lambda ref: asyncio.sleep(0, result=False)
    T.mark_ticket_cancelled = lambda sid, ref: None
    T.append_ticket_comment = fake_append
    CALLS.clear()
    d = await S._maybe_handle_ticket_edit(user_message=msg, context=ctx())
    got = CALLS[0].get("ticket_ref") if CALLS else None
    ok = got == expect
    print(f"{'PASS' if ok else 'FAIL'} | {msg!r} -> cancelled={got} (expected {expect})")
    if not ok and d is not None:
        print(f"        reply: {str(d.response_text)[:150]}")
    return ok

async def main():
    n = 0
    for m in ("actually cancel my food order", "cancel my order", "cancel the dosa order",
              "actually cancel the in-room dining order"):
        n += await run(m, [FOOD], "10900")
    print(f"=== {n}/4 ===")
asyncio.run(main())
