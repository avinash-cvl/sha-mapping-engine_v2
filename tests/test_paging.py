import sys, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from fastapi.testclient import TestClient
from app import app
import sqlalchemy as sa
from common import db

c = TestClient(app); ok=True
def chk(l,g,w):
    global ok; good=g==w; ok&=good
    print(f"  {'PASS' if good else 'FAIL'}  {l:52} got={g} want={w}")

conn = db.get_connection()
conn.execute(sa.text("""INSERT INTO config.users(name,email,password_hash,role,source_ownership,status,force_password_change,created_at,updated_at)
  SELECT 'Pg Test','pg-test@local.invalid',password_hash,'ADMIN','AllSources',1,0,SYSUTCDATETIME(),SYSUTCDATETIME()
  FROM config.users WHERE LOWER(email)='admin@covalenseglobal.com'"""))
conn.commit()
c.post("/api/session/login", json={"email":"admin@covalenseglobal.com","password":"Admin@123"})

base = "/api/engine/results?channel=zepto"
first = c.get(f"{base}&limit=10&offset=0").json()
chk("page 1 returns limit rows", len(first["rows"]), 10)
chk("total exceeds page", first["total"] > 10, True)
chk("offset echoed", first["offset"], 0)

second = c.get(f"{base}&limit=10&offset=10").json()
chk("page 2 offset echoed", second["offset"], 10)

a = [r["sku"] for r in first["rows"]]
b = [r["sku"] for r in second["rows"]]
chk("pages do not overlap", len(set(a) & set(b)), 0)

# walk the whole set and confirm nothing is lost or duplicated
seen, off = [], 0
while off < first["total"]:
    seen += [r["sku"] for r in c.get(f"{base}&limit=50&offset={off}").json()["rows"]]
    off += 50
chk("walking all pages yields every row", len(seen), first["total"])
chk("no duplicates across pages", len(set(seen)), first["total"])

last_off = (first["total"] // 10) * 10
last = c.get(f"{base}&limit=10&offset={last_off}").json()
chk("last page non-empty", len(last["rows"]) > 0, True)
chk("past the end returns empty, not error",
    len(c.get(f"{base}&limit=10&offset=999999").json()["rows"]), 0)
chk("limit is capped", c.get(f"{base}&limit=99999").status_code, 422)
chk("negative offset rejected", c.get(f"{base}&offset=-5").status_code, 422)

f = c.get(f"{base}&status=AutoMatch&limit=5&offset=0").json()
chk("filter+paging together", all(r["status"]=="AutoMatch" for r in f["rows"]), True)

conn.execute(sa.text("DELETE FROM config.users WHERE email='pg-test@local.invalid'")); conn.commit()
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
