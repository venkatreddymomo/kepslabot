"""Wait for the prod DB (VPN-gated) to come back, then exit.

It is ~10 hours until the VPN is expected up shortly after 08:00, so this polls
lazily overnight and only tightens up near the expected time - a 30s poll held for
ten hours is 1,200 pointless connection attempts.

Exits 0 the moment a real SELECT succeeds. Exits 1 if it is still down well after
the expected return, so a failure is reported rather than looking like silence.
"""
import io
import sys
import time
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

URL = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")

EXPECT_FROM = 8          # the VPN is expected back shortly after 08:00 local
GIVE_UP_AFTER = 11       # still nothing by 11:00 -> say so rather than hang silently


def db_up() -> bool:
    try:
        eng = create_engine(URL, connect_args={"connect_timeout": 6})
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def interval(now: datetime) -> int:
    """Lazy overnight, attentive around the expected return."""
    if now.hour >= EXPECT_FROM or now.hour < 1:
        return 60                       # in the window: check every minute
    if now.hour >= EXPECT_FROM - 1:
        return 120                      # the hour before: every 2 minutes
    return 600                          # deep overnight: every 10 minutes


start = datetime.now()
deadline = start.replace(hour=GIVE_UP_AFTER, minute=0, second=0, microsecond=0)
if deadline <= start:
    deadline += timedelta(days=1)

print("[{:%Y-%m-%d %H:%M:%S}] waiting for the DB; expecting it shortly after "
      "{:02d}:00, giving up at {:%H:%M}".format(start, EXPECT_FROM, deadline), flush=True)

checks = 0
while datetime.now() < deadline:
    checks += 1
    if db_up():
        print("[{:%Y-%m-%d %H:%M:%S}] DB IS BACK after {} checks".format(
            datetime.now(), checks), flush=True)
        raise SystemExit(0)
    now = datetime.now()
    if checks % 6 == 0:
        print("[{:%H:%M:%S}] still down ({} checks)".format(now, checks), flush=True)
    time.sleep(interval(now))

print("[{:%Y-%m-%d %H:%M:%S}] DB STILL DOWN at the give-up time ({} checks)".format(
    datetime.now(), checks), flush=True)
raise SystemExit(1)
