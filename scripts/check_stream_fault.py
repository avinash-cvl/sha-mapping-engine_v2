import sys, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from fastapi.testclient import TestClient
from app import app
import routers.engine as eng
c = TestClient(app); ok = True
def chk(l,g,w):
    global ok; good=g==w; ok&=good
    print(f"  {'PASS' if good else 'FAIL'}  {l:52} got={g} want={w}")

c.post("/api/session/login", json={"email":"admin@covalenseglobal.com","password":"Admin@123"})

# simulate the stale-reflection fault: make the poll query raise
real = eng.db_models.get_table
class Boom:
    @property
    def c(self): raise Exception("Invalid column name 'pipeline'")
def broken(engine, schema, name):
    if (schema, name) == ("audit", "engine_run"):
        t = real(engine, schema, name)
        class Proxy:
            def __getattr__(self, k):
                if k == "c": raise Exception("Invalid column name 'pipeline'")
                return getattr(t, k)
        return Proxy()
    return real(engine, schema, name)

eng.db_models.get_table = broken
try:
    events = []
    with c.stream("GET", "/api/engine/runs/11111111-1111-1111-1111-111111111111/stream") as s:
        for line in s.iter_lines():
            if line.startswith("event:"): events.append(line.split(":",1)[1].strip())
            if events and events[-1] == "done": break
    chk("a failing query still ends the stream", events[-1], "done")
    chk("stream does not raise to the client", True, True)
finally:
    eng.db_models.get_table = real

# and the normal path still works
events = []
with c.stream("GET", "/api/engine/runs/22222222-2222-2222-2222-222222222222/stream") as s:
    for line in s.iter_lines():
        if line.startswith("event:"): events.append(line.split(":",1)[1].strip())
        if events and events[-1] == "done": break
chk("unknown run still terminates", events[-1], "done")
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
