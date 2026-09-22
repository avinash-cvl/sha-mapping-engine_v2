"""Assert the resolver against figures established by hand earlier today."""
import sys
sys.path.insert(0, r"d:/Himalaya/sha-mapping-engine_v2")
from dotenv import load_dotenv
load_dotenv(r"d:/Himalaya/sha-mapping-engine_v2/.env")
from common import db, scope
conn = db.get_connection()
ok = True
def chk(label, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label:52} got={got} want={want}")

h, c = scope.resolve_both(conn, "zepto", "lip makeup", "lip balms")
chk("zepto lip balms himalaya scoped", h.scoped_total, 4)
chk("zepto lip balms competitor scoped", c.scoped_total, 274)
chk("zepto lip balms competitor already_mapped", c.already_mapped, 274)
chk("zepto lip balms competitor to_run (all done)", c.to_run, 0)

h, c = scope.resolve_both(conn, "amazon")
chk("amazon himalaya approved_skipped", h.approved_skipped, 940)
chk("amazon competitor scoped", c.scoped_total, 151061)
chk("amazon source_row_count", c.source_row_count, 154762)

for ch, want in (("blinkit",13141),("swiggy",9165),("zepto",10283)):
    _, cc = scope.resolve_both(conn, ch)
    chk(f"{ch} competitor scoped", cc.scoped_total, want)

print()
print("ALL PASS" if ok else "SOME FAILED")
