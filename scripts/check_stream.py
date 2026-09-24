"""Manual check: does the engine adopt the API's run id, and does the stream end?

NOT A TEST, despite where this used to live. It has no test functions -- it is
a script that runs top to bottom and LAUNCHES A REAL ENGINE RUN. Under
tests/ pytest collected it by filename and executed it during every suite
run, spending LLM budget and mutating live data as a side effect of
collection.

Run it by hand when the streaming path needs checking:

    uv run python scripts/check_stream.py

Credentials come from the environment rather than being hardcoded -- the
address that was baked in here is a real shared account.
"""
import os
import sys, warnings, time, json; warnings.filterwarnings("ignore")
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from fastapi.testclient import TestClient
from app import app
c = TestClient(app)
ok=True
def chk(l,g,w):
    global ok; good=g==w; ok&=good
    print(f"  {'PASS' if good else 'FAIL'}  {l:52} got={g} want={w}")

EMAIL = os.environ.get("CONSOLE_CHECK_EMAIL")
PASSWORD = os.environ.get("CONSOLE_CHECK_PASSWORD")
if not (EMAIL and PASSWORD):
    sys.exit("set CONSOLE_CHECK_EMAIL and CONSOLE_CHECK_PASSWORD first")
c.post("/api/session/login", json={"email": EMAIL, "password": PASSWORD})
r = c.post("/api/engine/runs", json={"channel":"zepto","engine":"himalaya",
    "category":"lip makeup","subcategory":"lip balms","workers":2})
chk("launch 200", r.status_code, 200)
rid = r.json()["runs"][0]["run_id"]
print(f"  launched run_id = {rid}")

# the engine must adopt OUR id
import sqlalchemy as sa
from common import db
conn = db.get_connection()
found=None
for _ in range(40):
    found = conn.execute(sa.text("SELECT status,processed FROM audit.engine_run WHERE execution_id=:i"),{"i":rid}).first()
    if found: break
    time.sleep(1)
chk("engine adopted the API's run_id", found is not None, True)

for _ in range(60):
    row = conn.execute(sa.text("SELECT status,processed,failed FROM audit.engine_run WHERE execution_id=:i"),{"i":rid}).first()
    if row and row[0] != "running": break
    time.sleep(1)
chk("run completed", row[0], "completed")
print(f"  processed={row[1]} failed={row[2]}")

# stream must emit done and stop, not hang
events=[]
with c.stream("GET", f"/api/engine/runs/{rid}/stream") as s:
    for line in s.iter_lines():
        if line.startswith("event:"): events.append(line.split(":",1)[1].strip())
        if events and events[-1]=="done": break
chk("stream emits done", "done" in events, True)

# unknown id must terminate, not loop forever
ev=[]
with c.stream("GET", "/api/engine/runs/00000000-0000-0000-0000-000000000000/stream") as s:
    for line in s.iter_lines():
        if line.startswith("event:"): ev.append(line.split(":",1)[1].strip())
        if ev and ev[-1]=="done": break
chk("unknown run ends with done (no retry loop)", ev[-1], "done")
print("\n"+("ALL PASS" if ok else "SOME FAILED"))
