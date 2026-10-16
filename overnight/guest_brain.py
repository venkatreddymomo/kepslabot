"""A guest who actually READS the bot's reply before answering.

The fixed-script harness sent the next line no matter what the bot had just said.
So when the bot ended a turn with "Would you like to add anything else?", the script
fired "yes place it" — and a bot that took the "yes" as answering its own question
was marked as broken when it had behaved reasonably. A scripted list cannot tell a
bot bug from a script bug.

This drives the same scenarios as a person would: each step is a GOAL ("get the
order placed"), and the words are written fresh each turn after reading what the bot
actually said. A goal is done when the bot's reply shows it is done, not after a
fixed number of turns.
"""
import json
import os

from openai import OpenAI

_MODEL = os.environ.get("GUEST_MODEL", "gpt-4o-mini")

SYSTEM = (
    "You are role-playing a hotel guest talking to the hotel's chat assistant. "
    "You are Venkat, staying in room 1400.\n"
    "You will be given your CURRENT GOAL and the assistant's LAST REPLY.\n"
    "Write the guest's next message: ONE short, natural line, the way a real person "
    "types on a phone. Lower case is fine.\n"
    "\n"
    "RULES THAT MATTER:\n"
    "- READ the assistant's last reply and answer THAT. If it asked you a question, "
    "your message must make sense as a reply to the question it actually asked.\n"
    "- Never answer with a bare 'yes' when the assistant asked something open like "
    "'anything else?' and you actually want something specific — say the specific "
    "thing ('no thanks, just place the order').\n"
    "- Do NOT invent new requests that are not part of your goal.\n"
    "- Do not be a tester. No meta-commentary, no 'as a test', no numbering.\n"
    "\n"
    "Also decide whether the GOAL IS NOW MET, judged ONLY from the assistant's last "
    "reply.\n"
    "- A goal to GET SOMETHING DONE (place an order, get towels sent, report a fault) "
    "is met ONLY when the assistant says it IS DONE — placed, on its way, passed to the "
    "team, being arranged. Being ASKED to confirm is NOT done: 'shall I place this "
    "order?' means it has NOT been placed, so goal_met is FALSE and your next message "
    "must push it over the line.\n"
    "- A goal to FIND SOMETHING OUT is met once the assistant has actually answered it.\n"
    "\n"
    'Return STRICT JSON: {"message": "<what the guest types>", "goal_met": true|false}\n'
    "If the goal is already met, still return the message you would send to MOVE ON, "
    "and goal_met true."
)


class Guest:
    def __init__(self) -> None:
        # SYNC on purpose: Playwright's sync API already runs its own event
        # loop, so asyncio.run() inside it raises. No asyncio here at all.
        self._client = OpenAI(api_key=_read_key())

    def say(self, *, goal: str, last_bot: str, history: list[dict]) -> tuple[str, bool]:
        """The guest's next line, plus whether the goal now looks met."""
        convo = "\n".join(
            "{}: {}".format("guest" if h["role"] == "user" else "assistant",
                            str(h["content"])[:400])
            for h in history[-8:]
        )
        user = json.dumps({
            "your_goal": goal,
            "assistants_last_reply": str(last_bot or "")[:1500],
            "recent_conversation": convo,
        }, ensure_ascii=False)
        r = self._client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.5,
            max_tokens=120,
            response_format={"type": "json_object"},
        )
        try:
            d = json.loads(r.choices[0].message.content or "{}")
        except ValueError:
            return "", False
        return str(d.get("message") or "").strip(), bool(d.get("goal_met"))


def _read_key() -> str:
    for line in open(".env", encoding="utf-8"):
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("OPENAI_API_KEY", "")


_DONE_SYSTEM = (
    "A hotel guest had a goal. Read ONLY the assistant's last reply and decide whether "
    "the goal is now DONE.\n"
    'Return STRICT JSON: {"done": true|false}\n'
    "true ONLY when the reply states the thing HAS HAPPENED — the order is placed / on "
    "its way / with the kitchen, the request has been passed to the team, the booking is "
    "confirmed, or the question has actually been answered.\n"
    "false when the assistant is still ASKING anything about it. 'Shall I place this "
    "order?', 'Should I go ahead?', 'Would you like to add anything else?' all mean it "
    "has NOT happened yet. A summary of the order is not a placed order.\n"
    "When in doubt, false."
)


def goal_is_done(client, *, goal: str, last_bot: str) -> bool:
    """Asked as its own question, of a model with nothing else to weigh.

    Folding this into the message-writing call made it answer 'met' the moment the
    bot showed a tidy order summary — so a run 'passed' while the kitchen had
    received nothing. One question, one job.
    """
    try:
        r = client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "system", "content": _DONE_SYSTEM},
                      {"role": "user", "content": json.dumps(
                          {"goal": goal, "assistants_last_reply": str(last_bot or "")[:1500]},
                          ensure_ascii=False)}],
            temperature=0.0,
            max_tokens=20,
            response_format={"type": "json_object"},
        )
        return bool(json.loads(r.choices[0].message.content or "{}").get("done"))
    except Exception:
        return False
