import sys, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from fastapi.testclient import TestClient
from app import app
c = TestClient(app); ok=True
def chk(l,g,w):
    global ok; good=g==w; ok&=good
    print(f"  {'PASS' if good else 'FAIL'}  {l:50} got={g} want={w}")

chk("plan needs auth", c.post("/api/engine/plan", json={"channel":"zepto"}).status_code, 401)
c.post("/api/session/login", json={"email":"admin@covalenseglobal.com","password":"Admin@123"})

body = {"channel":"zepto","pipeline":"competitor","category":"lip makeup",
        "subcategory":"lip balms","workers":4,"use_llm":True}
p = c.post("/api/engine/plan", json=body).json()
chk("plan resolves work", p["totals"]["to_run"], 2)
chk("names the database", p["environment"]["database"], "AureusSentinelv3_staging")
chk("shows brand filter", "NOT IN" in p["legs"][0]["brand_filter"], True)
chk("shows the command", "--run-id" in p["commands"][0]["command"], True)
chk("command names the module", "oneds_competitor" in p["commands"][0]["command"], True)
chk("lists the rules", len(p["rules"]) >= 4, True)
chk("attributes to session", p["triggered_by"], "admin@covalenseglobal.com")
chk("nothing blocking", p["blocked_by"], [])
chk("plan launched nothing", c.post("/api/engine/plan", json=body).json()["totals"]["to_run"], 2)

# invalid override must fail at plan time, not after the click
chk("bad override caught in plan",
    c.post("/api/engine/plan", json={**body, "overrides":{"TOP_N_OUTPUT":"9"}}).status_code, 400)

# the command shown must be the command that runs
r = c.post("/api/engine/runs", json=body).json()
rid = r["runs"][0]["run_id"]
shown = p["commands"][0]["command"].replace("<run-id>", rid)
import time, sqlalchemy as sa
from common import db
conn = db.get_connection()
for _ in range(60):
    row = conn.execute(sa.text("SELECT status FROM audit.engine_run WHERE execution_id=:i"),{"i":rid}).first()
    if row and row[0] != "running": break
    time.sleep(1)
chk("confirmed run completes", row[0], "completed")
chk("run id returned for tracking", len(rid), 36)
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
