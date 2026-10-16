"""Exit 0 the moment the prod DB actually answers a query, 1 otherwise.

A TCP check on 3306 is not enough - the port can accept while the VPN is
half-up and the query still fails - so this runs a real SELECT.
"""
import sys

from sqlalchemy import create_engine, text

URL = [l.split("=", 1)[1].strip()
       for l in open("d:/Sana Hp Laptop Data/D Drive Data/kepslasw/new_botv/new_bot/.env",
                     encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")

try:
    eng = create_engine(URL, connect_args={"connect_timeout": 8})
    with eng.connect() as c:
        n = c.execute(text(
            "SELECT COUNT(*) FROM new_bot_services WHERE id=98")).scalar()
    print("DB UP (services row 98 count=%s)" % n)
    sys.exit(0)
except Exception as exc:
    print("down: %r" % (str(exc)[:120],))
    sys.exit(1)
