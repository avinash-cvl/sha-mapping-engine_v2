"""Exercise run_record end to end, then clean up after itself."""
import sys, uuid
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from dotenv import load_dotenv
load_dotenv(r"d:/Himalaya/sha-mapping-engine_v2/.env")
from common import db, scope, run_record
import sqlalchemy as sa
conn = db.get_connection()
ok = True
def chk(l, got, want):
    global ok
    g = got == want; ok &= g
    print(f"  {'PASS' if g else 'FAIL'}  {l:52} got={got} want={want}")

rid = uuid.uuid4()
counts = scope.resolve(conn, "zepto", "competitor", "lip makeup", "lip balms")
run_record.start(conn, rid, counts, workers=4, use_llm=True,
                 triggered_by="test", trigger_type="manual",
                 config_overrides={"SEMANTIC_TOPK": "150"})

row = conn.execute(sa.text("SELECT channel,pipeline,category,scoped_total,status,triggered_by,git_sha,source_row_count FROM audit.engine_run WHERE execution_id=:i"), {"i": str(rid)}).first()
chk("run row written", row is not None, True)
chk("channel", row[0], "zepto")
chk("scoped_total matches resolver", row[3], counts.scoped_total)
chk("status starts running", row[4], "running")
chk("triggered_by recorded", row[5], "test")
chk("git_sha captured", bool(row[6]), True)
chk("source_row_count pinned", row[7], counts.source_row_count)

n = conn.execute(sa.text("SELECT COUNT(*) FROM audit.engine_run_config WHERE execution_id=:i"), {"i": str(rid)}).scalar()
chk("config snapshot rows", n >= len(run_record.TRACKED_CONFIG), True)
ov = conn.execute(sa.text("SELECT config_value,is_override FROM audit.engine_run_config WHERE execution_id=:i AND config_key='SEMANTIC_TOPK'"), {"i": str(rid)}).first()
chk("override flagged", ov[1], True)
tp = conn.execute(sa.text("SELECT config_value FROM audit.engine_run_config WHERE execution_id=:i AND config_key='TOP_N_OUTPUT'"), {"i": str(rid)}).scalar()
chk("TOP_N_OUTPUT captured", tp, "3")

run_record.heartbeat(conn, rid, processed=100, failed=2)
hb = conn.execute(sa.text("SELECT processed,failed,heartbeat_at FROM audit.engine_run WHERE execution_id=:i"), {"i": str(rid)}).first()
chk("heartbeat updates progress", (hb[0], hb[1]), (100, 2))

mix = run_record.outcome_for(conn, "zepto", "competitor", "lip makeup", "lip balms")
chk("outcome_for reads real mix", sum(mix.values()), 274)
run_record.finish(conn, rid, status="completed", processed=274, failed=0, outcome=mix)
fin = conn.execute(sa.text("SELECT status,processed,ended_at FROM audit.engine_run WHERE execution_id=:i"), {"i": str(rid)}).first()
chk("status closed", fin[0], "completed")
chk("ended_at set", fin[2] is not None, True)
oc = conn.execute(sa.text("SELECT COUNT(*) FROM audit.engine_run_outcome WHERE execution_id=:i"), {"i": str(rid)}).scalar()
chk("outcome rows written", oc, len([v for v in mix.values() if v]))

# stale reconcile
rid2 = uuid.uuid4()
run_record.start(conn, rid2, counts, workers=1, use_llm=False)
conn.execute(sa.text("UPDATE audit.engine_run SET heartbeat_at=DATEADD(hour,-5,SYSUTCDATETIME()) WHERE execution_id=:i"), {"i": str(rid2)})
conn.commit()
marked = run_record.reconcile_stale(conn, max_age_minutes=30)
chk("stale run marked", marked >= 1, True)
st = conn.execute(sa.text("SELECT status FROM audit.engine_run WHERE execution_id=:i"), {"i": str(rid2)}).scalar()
chk("stale status applied", st, "stale")

# comparison view
cmp_ = conn.execute(sa.text("SELECT COUNT(*) FROM audit.vw_engine_run_compare WHERE before_run=:a AND after_run=:b"), {"a": str(rid2), "b": str(rid)}).scalar()
chk("compare view returns rows", cmp_ > 0, True)

for x in (rid, rid2):
    conn.execute(sa.text("DELETE FROM audit.engine_run WHERE execution_id=:i"), {"i": str(x)})
conn.commit()
left = conn.execute(sa.text("SELECT COUNT(*) FROM audit.engine_run")).scalar()
chk("cleaned up (cascade)", left, 0)
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
