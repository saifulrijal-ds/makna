"""Does a request that passes every governance check return per-person rows?"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "spikes/compiler"))
import run_spike as rs
from mini_compiler import Model, compile_request
model = Model.load(Path(__file__).resolve().parents[3] / "semantic/schema/examples")
con = rs.build_db()
req = {"metric": "npf_outstanding_principal", "dimensions": ["customer.phone_number", "customer.birth_year", "customer.province"], "end": "2026-07-01"}
out = compile_request(model, req)
rows = con.execute(out["sql"]).fetchall()
print("request:", req)
print("compiled OK (no governance error); rows:", len(rows))
for r in rows[:5]: print("  ", r)
# how many result groups contain exactly one customer?
groups = con.execute("""
 SELECT '***' || right(CAST(phone_number AS VARCHAR), 4) p, CAST(date_trunc('year', birth_date) AS DATE) y, province, count(*) n
 FROM customers GROUP BY 1,2,3""").fetchall()
single = sum(1 for g in groups if g[3] == 1)
print(f"customers: 200; distinct (masked phone, birth year, province) groups: {len(groups)}; groups holding exactly one customer: {single}")
# can the masked key be joined back to the raw table? (what an agent with any second data source could do)
m = con.execute("""SELECT count(*) FROM customers c1 WHERE (SELECT count(*) FROM customers c2 WHERE right(c2.phone_number,4)=right(c1.phone_number,4))=1""").fetchone()[0]
print(f"customers uniquely identified by last 4 phone digits alone: {m}/200")
