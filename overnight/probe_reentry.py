"""Does the IRD re-entry judge recognise the turns that fell through to the book
answerer? Asks it the exact messages from the 50-turn run.

The guard only logs when it FIRES, so a false verdict leaves no trace - this is the
only way to tell a silent 'no' from a block that never ran.
"""
import asyncio
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from services.llm_orchestration_service import llm_orchestration_service as S

MESSAGES = [
    "what did i order",                     # T27 -> book answerer, invented red wine
    "cancel my second order",               # T28 -> book answerer, false denial
    "actually cancel my whole order",       # T29 -> locked refusal
    "make it 3 vegetable burgers instead of 2",
    "mujhe apna order dubara batao",        # Hindi recall
    "the AC in my room is not cooling properly",   # must stay FALSE
    "whats the wifi password",                     # must stay FALSE
    "book a table at kadak for 4 at 8pm",          # must stay FALSE
]


async def main():
    print("{:<46} {}".format("guest message", "about_the_order"))
    print("-" * 66)
    for m in MESSAGES:
        try:
            v = await S._guest_is_talking_about_their_food_order(user_message=m)
        except Exception as ex:
            v = "ERROR {!r}".format(ex)[:40]
        print("{:<46} {}".format(m[:46], v))


asyncio.run(main())
