import sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from fastapi.testclient import TestClient
from app import app
c = TestClient(app)
ok = True
def chk(l, got, want):
    global ok
    g = got == want; ok &= g
    print(f"  {'PASS' if g else 'FAIL'}  {l:50} got={got} want={want}")

r = c.post("/api/engine/scope", json={"channel":"zepto","category":"lip makeup","subcategory":"lip balms"})
chk("scope both pipelines 200", r.status_code, 200)
legs = {x["pipeline"]: x for x in r.json()["legs"]}
chk("himalaya scoped", legs["himalaya"]["scoped_total"], 4)
chk("competitor scoped", legs["competitor"]["scoped_total"], 274)
chk("two legs, never summed", len(r.json()["legs"]), 2)
chk("estimate present", "llm_calls" in r.json()["estimate"], True)

r = c.post("/api/engine/scope", json={"channel":"zepto","pipeline":"competitor"})
chk("single pipeline -> one leg", len(r.json()["legs"]), 1)

chk("bad channel rejected", c.post("/api/engine/scope", json={"channel":"nope"}).status_code, 400)
chk("bad pipeline rejected", c.post("/api/engine/scope", json={"channel":"zepto","pipeline":"x"}).status_code, 400)

r = c.get("/api/engine/channels")
chk("channels 200", r.status_code, 200)
chk("zepto has lip makeup", "lip makeup" in r.json()["zepto"], True)

r = c.get("/api/engine/runs?limit=5")
chk("runs list 200", r.status_code, 200)
runs = r.json()
chk("history has our wired run", len(runs) >= 1, True)
rid = runs[0]["execution_id"]

r = c.get(f"/api/engine/runs/{rid}")
chk("run detail 200", r.status_code, 200)
chk("detail has outcome", len(r.json()["outcome"]) > 0, True)
chk("detail has config", len(r.json()["config"]) >= 16, True)
chk("unknown run 404", c.get("/api/engine/runs/00000000-0000-0000-0000-000000000000").status_code, 404)

r = c.post("/api/engine/failed/preview", json={"channel":"zepto","pipeline":"competitor"})
chk("failed preview 200", r.status_code, 200)
chk("no failed rows now", r.json()["rows_found"], 0)

r = c.get(f"/api/engine/runs/{rid}/log?lines=5")
chk("log tail reachable", r.status_code in (200,404), True)

chk("health", c.get("/health").json()["status"], "ok")
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
