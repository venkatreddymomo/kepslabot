"""Wait for the in-flight run, then do two more, archiving between each.

Standalone so the three-run sequence completes whether or not the Claude
session is alive. Each run is the full 50-turn Playwright conversation through
the real widget; results land as run1_/run2_/run3_ files in overnight/.
"""
import datetime
import glob
import os
import shutil
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "overnight")
PY = r"C:\Users\Hp\AppData\Local\Programs\Python\Python313\python.exe"
JOB = os.path.join(OUT, "run_overnight_ui.py")


def log(msg):
    line = "[{:%Y-%m-%d %H:%M:%S}] CHAIN: {}".format(datetime.datetime.now(), msg)
    print(line, flush=True)
    with open(os.path.join(OUT, "chain_clean.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def job_running():
    try:
        out = subprocess.run(
            ["powershell", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*run_overnight_ui*' }).ProcessId"],
            capture_output=True, text=True, timeout=60,
        )
        return bool((out.stdout or "").strip())
    except Exception:
        return False


def archive(tag):
    for name in ("turns.jsonl", "forms.jsonl", "progress.log", "result.json"):
        src = os.path.join(OUT, name)
        if os.path.exists(src):
            shutil.move(src, os.path.join(OUT, "{}_{}".format(tag, name)))
    log("archived as {}_*".format(tag))


if __name__ == "__main__":
    log("=== clean 3-run chain on the corrected build ===")
    for n in (1, 2, 3):
        log("starting run {}".format(n))
        # relative script path + -WorkingDirectory: an absolute path containing
        # spaces gets mis-parsed by Start-Process and the job never launches,
        # which made an earlier chain report three runs after doing one.
        subprocess.Popen(
            ["powershell", "-Command",
             "Start-Process -FilePath '{py}' "
             "-ArgumentList 'overnight/run_overnight_ui.py' "
             "-WorkingDirectory '{root}' -WindowStyle Hidden".format(py=PY, root=ROOT)]
        )
        started = False
        for _ in range(40):                 # confirm it really came up
            time.sleep(15)
            if job_running():
                started = True
                break
        if not started:
            log("run {} FAILED TO START - aborting the chain".format(n))
            break
        while job_running():
            time.sleep(60)
        archive("run{}".format(n))
        log("run {} done".format(n))

    log("=== all three runs complete ===")
