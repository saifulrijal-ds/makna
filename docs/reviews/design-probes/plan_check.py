import json, re
from pathlib import Path
R = Path(__file__).resolve().parents[3] / "docs"
req = {}
for line in (R/"REQUIREMENTS.md").read_text().splitlines():
    m = re.match(r"\| ([A-Z]{3}-\d+) \| (.*) \| (.*) \|$", line)
    if m: req[m.group(1)] = m.group(3)
mvp = [k for k, v in req.items() if v.startswith("MVP")]
print(f"requirements: {len(req)}; MVP: {len(mvp)}")
plan = {}
for line in (R/"PLAN.md").read_text().splitlines():
    m = re.match(r"\| (WP-\d+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", line)
    if m:
        deps = re.findall(r"WP-\d+", m.group(4))
        if "all others" in m.group(4): deps = "ALL"
        plan[m.group(1)] = (m.group(2).strip(), sorted(re.findall(r"[A-Z]{3}-\d+", m.group(3))), deps)
prog = {p["id"]: p for p in json.loads((R/"progress.json").read_text())["packages"]}
print("packages: plan", len(plan), "progress", len(prog))
for wp, (title, reqs, deps) in plan.items():
    p = prog[wp]
    if p["title"] != title: print("TITLE DIFF", wp, title, "|", p["title"])
    if sorted(p["requirements"]) != reqs: print("REQ DIFF", wp, reqs, p["requirements"])
    pd = sorted(p["depends_on"]); 
    if deps == "ALL":
        if set(pd) != set(plan) - {wp}: print("DEP DIFF", wp)
    elif pd != sorted(deps): print("DEP DIFF", wp, deps, pd)
# section-level requirement lists inside each WP body (not the table) are not separately listed; check arch 16 table
arch = (R/"ARCHITECTURE.md").read_text()
sec16 = arch.split("## 16.")[1].split("## 17.")[0]
amap = {}
for line in sec16.splitlines():
    m = re.match(r"\| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", line)
    if not m or m.group(1).startswith("ID") or m.group(1).startswith("---"): continue
    ids = re.findall(r"[A-Z]{3}-\d+", m.group(1))
    wps = re.findall(r"WP-\d+", m.group(3))
    for i in ids: amap[i] = (m.group(2).strip(), wps)
plan_by_req = {}
for wp, (_, reqs, _) in plan.items():
    for r in reqs: plan_by_req.setdefault(r, []).append(wp)
print("\nMVP req | arch16 WPs | PLAN-table WPs | mismatch")
for r in mvp:
    a = amap.get(r, ("MISSING", []))
    pw = plan_by_req.get(r, [])
    flag = "" if set(a[1]) == set(pw) else "  <-- differs"
    if not a[1] or not pw: flag += "  <-- NO HOME"
    print(f"{r:6} | {','.join(a[1]) or '-':22} | {','.join(pw) or '-':22}{flag}")
# graph checks
deps = {wp: (set(plan) - {wp} if d == "ALL" else set(d)) for wp, (_, _, d) in plan.items()}
num = lambda w: int(w[3:])
for wp, ds in deps.items():
    for d in ds:
        if num(d) > num(wp): print("FORWARD DEP", wp, "->", d)
# cycle detection
state = {}
def visit(n, stack):
    if state.get(n) == 1: print("CYCLE", stack + [n]); return
    if state.get(n) == 2: return
    state[n] = 1
    for d in deps[n]: visit(d, stack + [n])
    state[n] = 2
for n in deps: visit(n, [])
print("cycle check done")
# transitive closure, for later reasoning
def closure(n, seen=None):
    seen = set() if seen is None else seen
    for d in deps[n]:
        if d not in seen: seen.add(d); closure(d, seen)
    return seen
for wp in ["WP-07","WP-08","WP-14","WP-17","WP-18","WP-19","WP-20"]:
    print(wp, "transitively depends on", sorted(closure(wp), key=num))
