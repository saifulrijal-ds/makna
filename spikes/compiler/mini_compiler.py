"""Spike: compile a metric request into Trino SQL with sqlglot.

Not product code. It proves that the schema in semantic/schema/ carries enough
information to compile simple, ratio, semi-additive, and cross-table metrics
with multi-hop joins, and that governance checks can be enforced on the plan.
The product compiler (work packages WP-08 to WP-10 in docs/PLAN.md) should
follow the same stages but be written fresh, with tests.
"""
from __future__ import annotations

import glob
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

GRAINS = ("day", "week", "month", "quarter", "year")
OPS = {"=": exp.EQ, "!=": exp.NEQ, ">": exp.GT, ">=": exp.GTE, "<": exp.LT, "<=": exp.LTE}
AGGS = {"sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}


class CompileError(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.details = code, message, details


@dataclass
class Model:
    entities: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)

    @classmethod
    def load(cls, root: str | Path) -> "Model":
        m = cls()
        for f in sorted(glob.glob(f"{root}/**/*.yaml", recursive=True)):
            d = yaml.safe_load(Path(f).read_text())
            if d["kind"] == "entity":
                m.entities[d["name"]] = d
            elif d["kind"] == "metric":
                m.metrics[d["name"]] = d
        return m

    # ---- join graph -------------------------------------------------------
    def paths(self, src: str, dst: str, max_depth: int = 4) -> list[list[str]]:
        """All shortest paths src -> dst along many-to-one relationships."""
        if src == dst:
            return [[]]
        found, best = [], None
        q = deque([(src, [])])
        while q:
            ent, path = q.popleft()
            if best is not None and len(path) >= best:
                continue
            if len(path) >= max_depth:
                continue
            for rel in self.entities[ent].get("relationships", []):
                step = f"{ent}.{rel['name']}"
                if any(s.split(".")[0] == rel["to"] for s in path) or rel["to"] == src:
                    continue  # no cycles
                new = path + [step]
                if rel["to"] == dst:
                    best = len(new)
                    found.append(new)
                else:
                    q.append((rel["to"], new))
        return found

    def resolve_path(self, metric: dict, src: str, dst: str) -> list[str]:
        pinned = metric.get("join_paths", {}).get(dst)
        if pinned:
            self._check_path(src, dst, pinned)
            return pinned
        candidates = self.paths(src, dst)
        if not candidates:
            raise CompileError("NO_JOIN_PATH", f"No approved join path from {src} to {dst}.")
        if len(candidates) > 1:
            raise CompileError(
                "AMBIGUOUS_JOIN_PATH",
                f"{len(candidates)} equally short paths from {src} to {dst}; metric "
                f"{metric['name']} must pin one in join_paths.",
                options=candidates,
            )
        return candidates[0]

    def _check_path(self, src, dst, path):
        cur = src
        for step in path:
            ent, rel_name = step.split(".")
            rel = self.rel(ent, rel_name)
            if ent != cur:
                raise CompileError("INVALID_JOIN_PATH", f"Step {step} does not start at {cur}.")
            cur = rel["to"]
        if cur != dst:
            raise CompileError("INVALID_JOIN_PATH", f"Path ends at {cur}, not {dst}.")

    def rel(self, ent: str, name: str) -> dict:
        for r in self.entities[ent].get("relationships", []):
            if r["name"] == name:
                return r
        raise CompileError("UNKNOWN_RELATIONSHIP", f"{ent}.{name}")

    def dim(self, ref: str) -> tuple[str, dict]:
        ent, name = ref.split(".")
        for d in self.entities.get(ent, {}).get("dimensions", []):
            if d["name"] == name:
                return ent, d
        raise CompileError("UNKNOWN_DIMENSION", ref)

    def measure(self, ref: str) -> tuple[str, dict]:
        ent, name = ref.split(".")
        for m in self.entities[ent].get("measures", []):
            if m["name"] == name:
                return ent, m
        raise CompileError("UNKNOWN_MEASURE", ref)

    def leaves(self, name: str) -> list[dict]:
        m = self.metrics.get(name)
        if m is None:
            raise CompileError("UNKNOWN_METRIC", name)
        if m["type"] == "simple":
            return [m]
        parts = [m["numerator"], m["denominator"]] if m["type"] == "ratio" else m["inputs"]
        out = []
        for p in parts:
            for leaf in self.leaves(p):
                if leaf["name"] not in [x["name"] for x in out]:
                    out.append(leaf)
        return out


def _table(fqn: str, alias: str) -> exp.Table:
    cat, db, name = fqn.split(".")
    return exp.table_(name, db=db, catalog=cat, alias=exp.to_identifier(alias, quoted=True), quoted=True)


def _qualify(sql: str, alias: str) -> exp.Expression:
    """Parse a scalar expression and point every column at one table alias."""
    tree = sqlglot.parse_one(sql, dialect="trino")
    if tree.find(exp.Select, exp.Subquery, exp.AggFunc, exp.Window):
        raise CompileError("INVALID_EXPRESSION", f"Subqueries and aggregates are not allowed: {sql}")
    for col in tree.find_all(exp.Column):
        col.set("table", exp.to_identifier(alias, quoted=True))
        col.this.set("quoted", True)
    return tree


def _literal(v):
    if isinstance(v, bool):
        return exp.Boolean(this=v)
    if isinstance(v, (int, float)):
        return exp.Literal.number(v)
    return exp.Literal.string(str(v))


def _date(s: str) -> exp.Expression:
    return exp.Cast(this=exp.Literal.string(s), to=exp.DataType.build("date"))


def _trunc(grain: str, e: exp.Expression) -> exp.Expression:
    return exp.Cast(this=exp.TimestampTrunc(this=e, unit=exp.Literal.string(grain)), to=exp.DataType.build("date"))


def _mask(d: dict, e: exp.Expression) -> exp.Expression:
    rule = d["masking"]
    if rule["method"] == "redact":
        out = exp.Literal.string("***")
    elif rule["method"] == "partial":
        s = exp.Cast(this=e, to=exp.DataType.build("varchar"))
        out = exp.Concat(expressions=[exp.Literal.string("***"), exp.Right(this=s, expression=exp.Literal.number(rule["keep_last"]))])
    elif rule["method"] == "truncate_date":
        out = _trunc(rule["grain"], e)
    elif rule["method"] == "bucket":
        w = exp.Literal.number(rule["width"])
        out = exp.Mul(this=exp.Floor(this=exp.Div(this=e, expression=w)), expression=w)
    out.meta["masked"] = True
    return out


class LeafQuery:
    """Builds the aggregate query for one simple metric."""

    def __init__(self, model: Model, metric: dict, request: dict, steps: list[str]):
        self.model, self.metric, self.req, self.steps = model, metric, request, steps
        self.entity, self.meas = model.measure(metric["measure"])
        self.aliases: dict[tuple, str] = {(): "t0"}
        self.joins: list[exp.Join] = []
        self.personal_cols: set[tuple[str, str]] = set()

    def alias_for(self, target_entity: str) -> str:
        if target_entity == self.entity:
            return "t0"
        path = self.model.resolve_path(self.metric, self.entity, target_entity)
        for i in range(1, len(path) + 1):
            key = tuple(path[:i])
            if key in self.aliases:
                continue
            ent, rel_name = path[i - 1].split(".")
            rel = self.model.rel(ent, rel_name)
            left = self.aliases[tuple(path[: i - 1])]
            alias = f"t{len(self.aliases)}"
            self.aliases[key] = alias
            on = exp.and_(*[
                exp.EQ(this=exp.column(k["from"], table=left, quoted=True), expression=exp.column(k["to"], table=alias, quoted=True))
                for k in rel["keys"]
            ])
            self.joins.append(exp.Join(this=_table(self.model.entities[rel["to"]]["source"]["table"], alias), side="LEFT", on=on))
            self.steps.append(f"{self.metric['name']}: join {rel['to']} via {path[i - 1]} ({rel['description']})")
        return self.aliases[tuple(path)]

    def dim_expr(self, ref: str, *, for_filter: bool) -> exp.Expression:
        ent, d = self.model.dim(ref)
        sens = d.get("sensitivity") or self.model.entities[ent]["sensitivity"]
        alias = self.alias_for(ent)
        e = _qualify(d.get("expr", d["name"]), alias)
        if sens == "personal":
            if for_filter:
                raise CompileError("PERSONAL_DATA_REFUSED", f"{ref} is personal data and cannot be used in a filter.")
            if "masking" not in d:
                raise CompileError("PERSONAL_DATA_REFUSED", f"{ref} is personal data with no masking rule; it cannot be returned.")
            for col in e.find_all(exp.Column):
                self.personal_cols.add((alias, col.name))
            self.steps.append(f"mask {ref} with method {d['masking']['method']}")
            return _mask(d, e)
        return e

    def build(self) -> exp.Select:
        req, meas = self.req, self.meas
        time_col = self.dim_expr(f"{self.entity}.{meas['time_dimension']}", for_filter=False)
        grain = req.get("grain")
        if grain and grain not in self.metric.get("time_grains", GRAINS):
            raise CompileError("INVALID_GRAIN", f"{self.metric['name']} supports {self.metric.get('time_grains')}, not {grain}.")
        selects, group = [], []
        if grain:
            selects.append(exp.alias_(_trunc(grain, time_col.copy()), "metric_time", quoted=True))
            group.append(_trunc(grain, time_col.copy()))
        for ref in req.get("dimensions", []):
            e = self.dim_expr(ref, for_filter=False)
            selects.append(exp.alias_(e, ref.replace(".", "__"), quoted=True))
            group.append(e.copy())

        where = []
        if meas.get("filter"):
            where.append(exp.paren(_qualify(meas["filter"], "t0")))
            self.steps.append(f"{self.metric['name']}: keep rows where {meas['filter']} (part of the measure definition)")
        for f in self.metric.get("filters", []) + req.get("filters", []):
            where.append(self.filter_expr(f))
        rng = []
        if req.get("start"):
            rng.append(exp.GTE(this=time_col.copy(), expression=_date(req["start"])))
        if req.get("end"):
            rng.append(exp.LT(this=time_col.copy(), expression=_date(req["end"])))
        where += rng
        self.steps.append(
            f"{self.metric['name']}: time is {self.entity}.{meas['time_dimension']}"
            + (f", from {req.get('start')} up to but not including {req.get('end')}" if rng else "")
        )
        if meas.get("semi_additive") == "last":
            where.append(self.last_snapshot_filter(time_col, grain, rng))

        arg = _qualify(meas["expr"], "t0") if meas.get("expr") else None
        if meas["agg"] == "count":
            agg = exp.Count(this=arg or exp.Star())
        elif meas["agg"] == "count_distinct":
            agg = exp.Count(this=exp.Distinct(expressions=[arg]))
        else:
            agg = AGGS[meas["agg"]](this=arg)
        selects.append(exp.alias_(agg, self.metric["name"], quoted=True))

        q = exp.select(*selects).from_(_table(self.model.entities[self.entity]["source"]["table"], "t0"))
        for j in self.joins:
            q = q.join(j)
        if where:
            q = q.where(exp.and_(*where))
        if group:
            q = q.group_by(*group)
        self.assert_no_raw_personal(q)
        return q

    def filter_expr(self, f: dict) -> exp.Expression:
        col = self.dim_expr(f["dimension"], for_filter=True)
        op = f["op"]
        if op in OPS:
            return OPS[op](this=col, expression=_literal(f["value"]))
        if op in ("in", "not_in"):
            e = exp.In(this=col, expressions=[_literal(v) for v in f["value"]])
            return exp.Not(this=e) if op == "not_in" else e
        e = exp.Is(this=col, expression=exp.Null())
        return exp.Not(this=e) if op == "is_not_null" else e

    def last_snapshot_filter(self, time_col, grain, rng) -> exp.Expression:
        """Balances: keep only the latest snapshot date in each period."""
        src = self.model.entities[self.entity]["source"]["table"]
        inner_col = _qualify(self.model.dim(f"{self.entity}.{self.meas['time_dimension']}")[1].get("expr", self.meas["time_dimension"]), "s")
        sub = exp.select(exp.Max(this=inner_col.copy())).from_(_table(src, "s"))
        inner_rng = []
        for cond in rng:
            c = cond.copy()
            c.set("this", inner_col.copy())
            inner_rng.append(c)
        if inner_rng:
            sub = sub.where(exp.and_(*inner_rng))
        if grain:
            sub = sub.group_by(_trunc(grain, inner_col.copy()))
        self.steps.append(
            f"{self.metric['name']}: balance measure, so only the last snapshot date "
            + (f"in each {grain}" if grain else "in the time range") + " is used"
        )
        return exp.In(this=time_col.copy(), query=sub.subquery())

    def assert_no_raw_personal(self, q: exp.Select) -> None:
        """Defence in depth: a personal column may appear only under a mask or an aggregate."""
        for col in q.find_all(exp.Column):
            if (col.table, col.name) not in self.personal_cols:
                continue
            node, ok = col, False
            while node is not None and node is not q:
                if node.meta.get("masked") or isinstance(node, exp.AggFunc):
                    ok = True
                    break
                node = node.parent
            if not ok:
                raise CompileError("PERSONAL_DATA_REFUSED", f"Raw personal column {col.sql()} in output.")


def compile_request(model: Model, request: dict, dialect: str = "trino") -> dict:
    """request: {metric, dimensions?, filters?, grain?, start?, end?, limit?}"""
    metric = model.metrics.get(request["metric"])
    if metric is None:
        raise CompileError("UNKNOWN_METRIC", request["metric"])
    if metric["status"] != "approved":
        raise CompileError("NOT_APPROVED", f"{metric['name']} is {metric['status']}.")
    steps: list[str] = []
    leaves = model.leaves(metric["name"])
    keys = (["metric_time"] if request.get("grain") else []) + [d.replace(".", "__") for d in request.get("dimensions", [])]
    kcols = [exp.column(k, quoted=True) for k in keys]

    ctes = {leaf["name"]: LeafQuery(model, leaf, request, steps).build() for leaf in leaves}
    if metric["type"] == "simple":
        final = ctes[metric["name"]]
    else:
        # Combine separately aggregated inputs. UNION ALL + GROUP BY treats NULL
        # keys as equal and needs no null-safe join, so it is portable.
        names = list(ctes)
        union = None
        for name in names:
            part = exp.select(
                *kcols,
                *[exp.alias_(exp.column(n, quoted=True) if n == name else exp.Null(), n, quoted=True) for n in names],
            ).from_(exp.to_table(f"m_{name}"))
            union = part if union is None else exp.union(union, part, distinct=False)
        combined = exp.select(
            *kcols,
            *[exp.alias_(exp.Coalesce(this=exp.Max(this=exp.column(n, quoted=True)), expressions=[exp.Literal.number(0)]), n, quoted=True) for n in names],
        ).from_(exp.to_table("unioned"))
        if kcols:
            combined = combined.group_by(*[k.copy() for k in kcols])
        if metric["type"] == "ratio":
            value = exp.Div(
                this=exp.Cast(this=exp.column(metric["numerator"], quoted=True), to=exp.DataType.build("double")),
                expression=exp.Nullif(this=exp.column(metric["denominator"], quoted=True), expression=exp.Literal.number(0)),
            )
            steps.append(f"{metric['name']} = {metric['numerator']} / {metric['denominator']}; each side is aggregated separately, then combined on {keys or 'a single row'}; a zero denominator gives NULL")
        else:
            value = sqlglot.parse_one(metric["expr"], dialect="trino")
            for col in value.find_all(exp.Column):
                col.this.set("quoted", True)
            steps.append(f"{metric['name']} = {metric['expr']}")
        final = exp.select(*kcols, *[exp.column(n, quoted=True) for n in names], exp.alias_(value, metric["name"], quoted=True)).from_(exp.to_table("combined"))
        for name in names:
            final = final.with_(f"m_{name}", as_=ctes[name])
        final = final.with_("unioned", as_=union).with_("combined", as_=combined)
    if keys:
        final = final.order_by(*[exp.Ordered(this=k.copy(), nulls_first=False) for k in kcols])
    final = final.limit(request.get("limit", 1000))
    return {
        "sql": final.sql(dialect=dialect, pretty=True),
        "columns": keys + ([] if metric["type"] == "simple" else list(ctes)) + [metric["name"]],
        "explanation": steps,
    }
