import sys, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from fastapi.testclient import TestClient
from app import app
c = TestClient(app)
ok = True
def chk(l,g,w):
    global ok; good=g==w; ok&=good
    print(f"  {'PASS' if good else 'FAIL'}  {l:52} got={g} want={w}")

# --- unauthenticated: every engine route must refuse
for path, method in (("/api/engine/channels","get"), ("/api/engine/runs","get"),
                     ("/api/engine/config","get"), ("/api/engine/compare?before=a&after=b","get")):
    chk(f"401 {path[:36]}", getattr(c,method)(path).status_code, 401)
chk("401 POST /scope", c.post("/api/engine/scope", json={"channel":"zepto"}).status_code, 401)
chk("401 POST /runs", c.post("/api/engine/runs", json={"channel":"zepto"}).status_code, 401)
chk("health stays open", c.get("/health").status_code, 200)
chk("login page open", c.get("/login").status_code, 200)

# --- bad credentials
chk("bad password 401", c.post("/api/session/login",
    json={"email":"console-test-admin@local","password":"wrong"}).status_code, 401)
chk("unknown user 401", c.post("/api/session/login",
    json={"email":"nobody@nowhere","password":"x"}).status_code, 401)

# --- USER account is refused
chk("USER refused 403", c.post("/api/session/login",
    json={"email":"console-test-user@local","password":"Test-Passw0rd!"}).status_code, 403)

# --- ADMIN signs in
r = c.post("/api/session/login", json={"email":"console-test-admin@local","password":"Test-Passw0rd!"})
chk("ADMIN login 200", r.status_code, 200)
chk("  role returned", r.json()["user"]["role"], "ADMIN")
chk("  cookie set", "console_session" in r.cookies, True)

chk("/me works", c.get("/api/session/me").status_code, 200)
chk("channels now 200", c.get("/api/engine/channels").status_code, 200)
r = c.post("/api/engine/scope", json={"channel":"zepto","category":"lip makeup"})
chk("scope now 200", r.status_code, 200)
chk("  competitor leg present", any(l["pipeline"]=="competitor" for l in r.json()["legs"]), True)

# --- override validation through the API
def launch(ov):
    return c.post("/api/engine/runs", json={"channel":"zepto","pipeline":"competitor",
        "category":"lip makeup","subcategory":"lip balms","workers":1,"overrides":ov})
chk("locked override rejected", launch({"TOP_N_OUTPUT":"5"}).status_code, 400)
chk("unknown key rejected", launch({"NOT_A_SETTING":"1"}).status_code, 400)
chk("out-of-range rejected", launch({"SEMANTIC_TOPK":"99999"}).status_code, 400)
chk("unbalanced weights rejected", launch({"W_PACK":"0.9"}).status_code, 400)

chk("logout 200", c.post("/api/session/logout").status_code, 200)
c.cookies.clear()
chk("refused again after logout", c.get("/api/engine/runs").status_code, 401)
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
