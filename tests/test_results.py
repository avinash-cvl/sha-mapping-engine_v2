import sys, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from fastapi.testclient import TestClient
from app import app
c = TestClient(app); ok=True
def chk(l,g,w):
    global ok; good=g==w; ok&=good
    print(f"  {'PASS' if good else 'FAIL'}  {l:48} got={g} want={w}")
c.post("/api/session/login", json={"email":"admin@covalenseglobal.com","password":"Admin@123"})

r = c.get("/api/engine/results?channel=zepto&category=lip makeup&limit=5")
chk("results 200", r.status_code, 200)
d = r.json()
chk("has total", d["total"] > 0, True)
chk("rows carry category", "category" in d["rows"][0], True)
chk("rows carry subcategory", "subcategory" in d["rows"][0], True)
chk("rows carry engine", d["rows"][0]["engine"] in ("himalaya","competitor"), True)
print("   sample:", {k: str(d["rows"][0][k])[:26] for k in ("sku","category","subcategory","status","product_code")})

f = c.get("/api/engine/results?channel=zepto&status=AutoMatch&limit=3").json()
chk("status filter works", all(x["status"]=="AutoMatch" for x in f["rows"]), True)
p = c.get("/api/engine/results?channel=zepto&engine=himalaya&limit=5").json()
chk("engine filter works", all(x["engine"]=="himalaya" for x in p["rows"]), True)
s = c.get("/api/engine/results?channel=zepto&q=lip&limit=3").json()
chk("search returns rows", s["total"] > 0, True)

g = c.get("/api/engine/results/groups?channel=zepto&category=lip makeup").json()
chk("groups 200", len(g) > 0, True)
print("   group:", {k: g[0][k] for k in ("category","subcategory","total","AutoMatch","automatch_rate")})

w = c.get("/api/engine/gate-warnings?limit=5").json()
chk("gate warnings returned", len(w) > 0, True)
print("   worst:", w[0]["node"], "rows=", w[0]["rows"], w[0]["state"])
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
