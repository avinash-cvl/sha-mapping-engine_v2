import sys, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from fastapi.testclient import TestClient
from app import app
c = TestClient(app); ok=True
def chk(l,g,w):
    global ok; good=g==w; ok&=good
    print(f"  {'PASS' if good else 'FAIL'}  {l:52} got={g} want={w}")
c.post("/api/session/login", json={"email":"admin@covalenseglobal.com","password":"Admin@123"})

d = c.get("/api/engine/logs?limit=5").json()
chk("log listing 200", d["total"] > 1000, True)
chk("returns limited page", len(d["files"]), 5)
chk("newest first", d["files"][0]["modified"] >= d["files"][-1]["modified"], True)
f = d["files"][0]
chk("carries run_id where present", "run_id" in f, True)
chk("carries a readable label", bool(f["label"]), True)
print("   newest:", f["label"], f"{f['size_bytes']//1024}KB")

s = c.get("/api/engine/logs?q=amazon&limit=3").json()
chk("search filters", all("amazon" in x["name"].lower() for x in s["files"]), True)

big = sorted(c.get("/api/engine/logs?limit=1000").json()["files"],
             key=lambda x: -x["size_bytes"])[0]
p = c.get(f"/api/engine/logs/{big['name']}").json()
chk("parses a real log", p["counts"]["lines"] > 1000, True)
chk("finds groups", p["totals"]["groups"] >= 1, True)
chk("unparsed stays low", p["counts"]["unparsed"] / p["counts"]["lines"] < 0.15, True)
print(f"   {big['label']}: {p['counts']['lines']} lines, {p['totals']['groups']} groups, "
      f"{p['totals']['successful']} ok, {p['totals']['failed']} failed")

chk("raw tail on request", "raw_tail" in c.get(f"/api/engine/logs/{big['name']}?raw=true").json(), True)
chk("unknown log 404", c.get("/api/engine/logs/nope.log").status_code, 404)

# path traversal must not reach outside logs/
for evil in ("../.env", "..%2f.env", "....//.env"):
    r = c.get(f"/api/engine/logs/{evil}")
    chk(f"traversal blocked: {evil[:14]}", r.status_code in (404, 400), True)

chk("listing needs auth", TestClient(app).get("/api/engine/logs").status_code, 401)
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
