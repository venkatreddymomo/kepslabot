"""Dump the In-Room Dining menu knowledge exactly as the service agent receives it,
so timing rules are judged against the real text and not a guess about it."""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

url = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
engine = create_engine(url, connect_args={"connect_timeout": 10})

with engine.connect() as conn:
    row = conn.execute(text(
        "SELECT id, name, service_prompt_pack "
        "FROM new_bot_services WHERE id=98")).first()
    m = dict(row._mapping)

pack = m.get("service_prompt_pack")
if isinstance(pack, (bytes, bytearray)):
    pack = pack.decode("utf-8", "replace")
if isinstance(pack, str):
    try:
        pack = json.loads(pack)
    except ValueError:
        pass

print("service {} '{}'".format(m["id"], m["name"]))
print("extracted_knowledge len:", len(str(m.get("extracted_knowledge") or "")))
if isinstance(pack, dict):
    print("prompt_pack keys:", list(pack.keys()))
    for k, v in pack.items():
        print("   {:34s} {}".format(k, len(str(v)) if v else 0))

blob = ""
if isinstance(pack, dict):
    blob = str(pack.get("extracted_knowledge") or "")
if not blob:
    blob = str(m.get("extracted_knowledge") or "")

open("overnight/ird_menu_dump.txt", "w", encoding="utf-8").write(blob)
print("\nmenu blob {} chars -> overnight/ird_menu_dump.txt".format(len(blob)))

low = blob.lower()
print("\n--- every line mentioning a time / timing ---")
for i, line in enumerate(blob.split("\n"), 1):
    l = line.lower()
    if any(t in l for t in ("timing", "available from", " am", " pm", "noon", "midnight", "24 hour", "24-hour")):
        print("{:5d} | {}".format(i, line.strip()[:200]))
