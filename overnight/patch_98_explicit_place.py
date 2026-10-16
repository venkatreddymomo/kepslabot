""""Place it" is an instruction, not an answer to whatever was last asked.

The prompt tells the agent to re-read its own last message before reading a bare
"yes" as the confirmation - a good rule, because a "yes" really does answer the
question that was actually asked. But it is written as an absolute ("treat yes as
the order confirmation ONLY when your last message asked for it"), and the agent
had just ended on the required open question, "What else would you like to add?".

So "yes place it" was read as "yes, suggest me more" and answered with an upsell.
The guest asked for their food to be placed and got a list of other dishes; nothing
reached the kitchen. A guest who NAMES the action is not answering a question.
"""
import io
import json
import os
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

SERVICE_ID = 98

ANCHOR = (
    "- Treat \"yes\" as the order confirmation ONLY when your last message actually "
    "asked for that confirmation (the checkout question above)."
)

REPLACEMENT = (
    "- Treat a BARE \"yes\" as the order confirmation ONLY when your last message "
    "actually asked for that confirmation (the checkout question above).\n"
    "- THAT APPLIES TO A BARE AGREEMENT ONLY. When the guest NAMES THE ACTION - "
    "\"place it\", \"yes place it\", \"go ahead and place the order\", \"confirm my "
    "order\", \"send it to the kitchen\", \"order it\" - they are telling you to place "
    "the order, whatever your own last message happened to ask. Place it on that turn. "
    "This holds even when you had just offered them something else or asked what else "
    "they would like to add: an instruction is not an answer to your question. NEVER "
    "reply to an explicit placement instruction with more suggestions, another question, "
    "or the order summary again - the guest has to repeat themselves while their food "
    "is never ordered."
)


def main() -> int:
    url = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    engine = create_engine(url, connect_args={"connect_timeout": 10})

    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT name, generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=:i"), {"i": SERVICE_ID}).first()
        if row is None:
            print("FAIL: service {} not found".format(SERVICE_ID))
            return 1
        prompt = row._mapping["generated_system_prompt"]

        os.makedirs("backups", exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bpath = "backups/service_{}_prompt_{}.json".format(SERVICE_ID, stamp)
        with open(bpath, "w", encoding="utf-8") as f:
            json.dump(dict(row._mapping), f, ensure_ascii=False, indent=2, default=str)
        print("backup -> {}".format(bpath))

        if "NAMES THE ACTION" in prompt:
            print("already applied")
            return 0
        found = prompt.count(ANCHOR)
        if found != 1:
            print("FAIL: anchor found {} times (need exactly 1)".format(found))
            return 1

        before = len(prompt)
        prompt = prompt.replace(ANCHOR, REPLACEMENT)
        conn.execute(text(
            "UPDATE new_bot_services SET generated_system_prompt=:p, "
            "generated_system_prompt_override=1 WHERE id=:i"),
            {"p": prompt, "i": SERVICE_ID})
        print("prompt {} -> {} chars".format(before, len(prompt)))

    with engine.connect() as conn:
        saved = conn.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=:i"), {"i": SERVICE_ID}).first()._mapping
        ok = True
        for marker in ("NAMES THE ACTION", "MOST OF THE MENU HAS NO STATED TIMING",
                       "CART UNTIL IT IS PLACED", "ONE QUESTION, ONE YES",
                       "menu_serving_windows"):
            if marker not in saved["generated_system_prompt"]:
                print("VERIFY FAIL: {!r} missing".format(marker))
                ok = False
        if int(saved["generated_system_prompt_override"] or 0) != 1:
            print("VERIFY FAIL: override lock is not 1")
            ok = False
        print("VERIFIED IN DB (all rules present)" if ok else "VERIFY FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
