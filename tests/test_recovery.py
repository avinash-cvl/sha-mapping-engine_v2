"""Exercise reset_failed on synthetic Failed rows, then restore exactly."""
import sys
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from dotenv import load_dotenv
load_dotenv(r"d:/Himalaya/sha-mapping-engine_v2/.env")
from common import db, recovery
import sqlalchemy as sa
conn = db.get_connection()
ok = True
def chk(label, got, want):
    global ok
    good = got == want; ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label:54} got={got} want={want}")

# --- pick victims: 3 competitor rows + 1 APPROVED himalaya row
vic = [r[0] for r in conn.execute(sa.text("""
  SELECT TOP 3 sku FROM staging.zepto_products
  WHERE brand NOT IN ('Himalaya') AND category='lip makeup' ORDER BY id""")).all()]
appr = conn.execute(sa.text("""
  SELECT TOP 1 sku FROM staging.zepto_products
  WHERE review_status='Approved'""")).scalar()
print(f"victims={vic}\napproved victim={appr}\n")

orig = {r[0]: r[1] for r in conn.execute(sa.text(
  "SELECT sku, mapping_status FROM staging.zepto_products WHERE sku IN :s"
).bindparams(sa.bindparam("s", expanding=True)), {"s": vic + [appr]}).all()}

# --- plant Failed on all four, including the approved one
conn.execute(sa.text("UPDATE staging.zepto_products SET mapping_status='Failed' WHERE sku IN :s")
             .bindparams(sa.bindparam("s", expanding=True)), {"s": vic + [appr]})
conn.commit()

p = recovery.preview(conn, "zepto", "competitor", "lip makeup")
chk("preview finds the 3 competitor Failed rows", p.rows_found, 3)
chk("preview excludes the approved row", appr in p.skus, False)

r = recovery.reset_failed(conn, "zepto", "competitor", "lip makeup", actor="test")
chk("reset flipped exactly 3", r.rows_reset, 3)

now = {x[0]: x[1] for x in conn.execute(sa.text(
  "SELECT sku, mapping_status FROM staging.zepto_products WHERE sku IN :s"
).bindparams(sa.bindparam("s", expanding=True)), {"s": vic + [appr]}).all()}
chk("all 3 victims now PENDING", all(now[v]=="PENDING" for v in vic), True)
chk("APPROVED row still Failed (untouched)", now[appr], "Failed")

chk("second preview finds none left", recovery.preview(conn,"zepto","competitor","lip makeup").rows_found, 0)
chk("reset on empty scope is a no-op", recovery.reset_failed(conn,"zepto","competitor","lip makeup").rows_reset, 0)

n = conn.execute(sa.text("SELECT COUNT(*) FROM audit.activity_log WHERE action='FAILED_ROWS_RESET'")).scalar()
chk("audit row written", n >= 1, True)

# --- restore
for sku, st in orig.items():
    conn.execute(sa.text("UPDATE staging.zepto_products SET mapping_status=:st WHERE sku=:s"),
                 {"st": st, "s": sku})
conn.execute(sa.text("DELETE FROM audit.activity_log WHERE action='FAILED_ROWS_RESET'"))
conn.commit()
back = {x[0]: x[1] for x in conn.execute(sa.text(
  "SELECT sku, mapping_status FROM staging.zepto_products WHERE sku IN :s"
).bindparams(sa.bindparam("s", expanding=True)), {"s": vic + [appr]}).all()}
chk("restored to original statuses", back == orig, True)

print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
