"""Definition errors V01 to V18 (ARCHITECTURE section 5.3).

`validate` takes a `DefinitionSet` and returns every problem as an `Issue`. V01 problems are
found while loading; a file that failed V01 is left out of the other checks, so one broken
file does not produce a cascade of unrelated errors in its own checks (files that refer to it
do report that the reference does not resolve).

How references are resolved. A name resolves to the official file when there is one, else to
the proposal. The file under check is always seen as itself: a proposal is checked against the
content of the proposal, so a draft change to an entity is validated with its own new content
and the rest of the repository as it stands.

Not here:

- V19 (every example request compiles) needs the compiler (WP-08). `validate` accepts
  `extra_checks`: functions that take the `DefinitionSet` and return more issues. WP-08 passes
  its V19 check there, and `makna validate` adds it to the call.
- Reference issues R01 to R04 compare definitions with the catalog (WP-06).
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from makna.definitions.issues import Issue
from makna.definitions.loader import DefinitionFile, DefinitionSet
from makna.model.definitions import (
    AnyDefinition,
    Dimension,
    Domain,
    Entity,
    Filter,
    GlossaryTerm,
    Measure,
    Metric,
    RuleRef,
)

ExtraCheck = Callable[[DefinitionSet], list[Issue]]

KINDS = ("domain", "entity", "metric", "glossary_term")
KIND_FOLDERS = {"entity": "entities", "metric": "metrics", "glossary_term": "glossary"}
ALL_GRAINS = ("day", "week", "month", "quarter", "year")
FINE_GRAINS = ("day", "week")
NOT_AGGREGATE_AGGS = ("count", "count_distinct")
DIALECTS = ("postgres", "duckdb")
DERIVED_NODES = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Paren, exp.Neg, exp.Nullif, exp.Coalesce)


# --------------------------------------------------------------------------------------
# Expressions


class _ExprError(Exception):
    pass


@lru_cache(maxsize=4096)
def _parse(sql: str) -> exp.Condition:
    """Parse one SQL expression and print it for both dialects, or raise `_ExprError`."""
    try:
        tree = sqlglot.parse_one(sql, into=exp.Condition)
    except SqlglotError as error:
        first = str(error).splitlines()[0]
        raise _ExprError(f"does not parse as one SQL expression ({first})") from None
    if isinstance(tree, exp.Block):
        raise _ExprError("holds more than one statement")
    for dialect in DIALECTS:
        try:
            tree.sql(dialect=dialect, unsupported_level=ErrorLevel.RAISE)
        except SqlglotError as error:
            raise _ExprError(f"cannot be written for {dialect} ({error})") from None
    return tree


def _forbidden(tree: exp.Condition) -> str | None:
    """Name the first subquery, aggregate, or window function in `tree`, if any."""
    for node in tree.walk():
        if isinstance(node, exp.Window):
            return "a window function"
        if isinstance(node, exp.AggFunc):
            return f"an aggregate ({node.sql_name()})"
        if isinstance(node, exp.Query):
            return "a subquery"
    return None


def _columns(sql: str | None) -> set[str]:
    """Lower-case names of the columns an expression reads (empty if it does not parse)."""
    if sql is None:
        return set()
    try:
        tree = _parse(sql)
    except _ExprError:
        return set()
    return {c.name.lower() for c in tree.find_all(exp.Column)}


# --------------------------------------------------------------------------------------
# The repository as seen from one file


@dataclass
class _Repository:
    official: dict[tuple[str, str], list[DefinitionFile]] = field(default_factory=dict)
    proposals: dict[tuple[str, str], list[DefinitionFile]] = field(default_factory=dict)

    @classmethod
    def build(cls, files: Iterable[DefinitionFile]) -> _Repository:
        repo = cls()
        for f in files:
            d = f.definition
            assert d is not None
            table = repo.proposals if f.is_proposal else repo.official
            table.setdefault((d.kind, d.name), []).append(f)
        return repo


class _View:
    """Name lookups for the file under check: itself, then official files, then proposals."""

    def __init__(self, repo: _Repository, current: DefinitionFile) -> None:
        self.repo = repo
        self.current = current

    def get(self, kind: str, name: str) -> AnyDefinition | None:
        own = self.current.definition
        if own is not None and own.kind == kind and own.name == name:
            return own
        for table in (self.repo.official, self.repo.proposals):
            found = table.get((kind, name))
            if found:
                return found[0].definition
        return None

    def entity(self, name: str) -> Entity | None:
        found = self.get("entity", name)
        return found if isinstance(found, Entity) else None

    def metric(self, name: str) -> Metric | None:
        found = self.get("metric", name)
        return found if isinstance(found, Metric) else None

    def reachable(self, start: str) -> set[str]:
        """Entities reachable from `start` along relationships, including `start`."""
        seen: set[str] = set()
        todo = [start]
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            entity = self.entity(name)
            if entity is not None:
                todo.extend(r.to for r in entity.relationships or [])
        return seen

    def resolve_measure(self, ref: str) -> tuple[Entity, Measure] | None:
        entity_name, _, measure_name = ref.partition(".")
        entity = self.entity(entity_name)
        if entity is None:
            return None
        for measure in entity.measures or []:
            if measure.name == measure_name:
                return entity, measure
        return None


def _effective_sensitivity(entity: Entity, dimension: Dimension) -> str:
    return dimension.sensitivity or entity.sensitivity


def _is_personal(entity: Entity, dimension: Dimension) -> bool:
    return _effective_sensitivity(entity, dimension) == "personal"


def _dimension(entity: Entity, name: str) -> Dimension | None:
    return next((d for d in entity.dimensions if d.name == name), None)


def _metric_inputs(metric: Metric) -> list[str]:
    if metric.type == "ratio":
        return [n for n in (metric.numerator, metric.denominator) if n is not None]
    if metric.type == "derived":
        return list(metric.inputs or [])
    return []


# --------------------------------------------------------------------------------------
# Checks for one file


class _Checker:
    def __init__(self, repo: _Repository, file: DefinitionFile) -> None:
        assert file.definition is not None
        self.file = file
        self.d: AnyDefinition = file.definition
        self.view = _View(repo, file)
        self.repo = repo
        self.issues: list[Issue] = []

    def add(self, code: str, message: str, *where: str | int) -> None:
        self.issues.append(self.file.issue(code, message, tuple(where)))

    def run(self) -> list[Issue]:
        self.v02_path_matches_content()
        self.v03_unique_names()
        d = self.d
        if isinstance(d, (Entity, Metric, GlossaryTerm)):
            self.v04_domain_exists(d)
        if isinstance(d, Entity):
            self.entity_checks(d)
        if isinstance(d, Metric):
            self.metric_checks(d)
        self.v14_deprecation()
        return self.issues

    # V02 ----------------------------------------------------------------------------

    def v02_path_matches_content(self) -> None:
        d, f = self.d, self.file
        suffix = ".draft.yaml" if f.is_proposal else ".yaml"
        if isinstance(d, Domain):
            expected = f"{d.name}/domain{suffix}"
        else:
            expected = f"{d.domain}/{KIND_FOLDERS[d.kind]}/{d.name}{suffix}"
        if PurePosixPath(f.path) != PurePosixPath(expected):
            self.add("V02", f"path should be {expected} for this content, not {f.path}", "name")
        allowed = ("draft", "in_review") if f.is_proposal else ("approved", "deprecated")
        if d.status not in allowed:
            what = "a proposal file (.draft.yaml)" if f.is_proposal else "an official file"
            self.add("V02", f"status {d.status} is not allowed in {what}", "status")

    # V03 ----------------------------------------------------------------------------

    def v03_unique_names(self) -> None:
        d = self.d
        table = self.repo.proposals if self.file.is_proposal else self.repo.official
        others = table.get((d.kind, d.name), [])
        if len(others) > 1:
            where = ", ".join(sorted(o.path for o in others if o is not self.file))
            self.add("V03", f"{d.kind} name {d.name} is also used by {where}", "name")
        if isinstance(d, Entity):
            parts: dict[str, Sequence[object]] = {
                "dimensions": d.dimensions,
                "measures": d.measures or [],
                "relationships": d.relationships or [],
                "rules": d.rules or [],
            }
            for section, items in parts.items():
                counts = Counter(getattr(i, "name") for i in items)  # noqa: B009
                seen: set[str] = set()
                for index, item in enumerate(items):
                    name = getattr(item, "name")  # noqa: B009
                    if counts[name] > 1 and name in seen:
                        self.add("V03", f"{section} name {name} is used twice", section, index)
                    seen.add(name)

    # V04 ----------------------------------------------------------------------------

    def v04_domain_exists(self, d: Entity | Metric | GlossaryTerm) -> None:
        if self.view.get("domain", d.domain) is None:
            self.add("V04", f"domain {d.domain} does not exist", "domain")

    # Entity: V05, V06, V07, V11, V12, V16, V17, V18 -----------------------------------

    def entity_checks(self, e: Entity) -> None:
        personal_columns: set[str] = set()
        rules = {r.name for r in e.rules or []}
        for i, dim in enumerate(e.dimensions):
            if dim.expr is not None:
                self.check_expression(dim.expr, f"dimension {dim.name}", "dimensions", i, "expr")
            self.v11_masking_fits(dim, i)
            if _is_personal(e, dim):
                personal_columns |= _columns(dim.expr if dim.expr is not None else dim.name)
                if dim.values:
                    self.add(
                        "V17",
                        f"personal dimension {dim.name} lists values, which would be shown "
                        "and indexed for search",
                        "dimensions",
                        i,
                        "values",
                    )
        for i, rule in enumerate(e.rules or []):
            self.check_expression(rule.expr, f"rule {rule.name}", "rules", i, "expr")
            overlap = sorted(_columns(rule.expr) & personal_columns)
            if overlap:
                self.add(
                    "V18",
                    f"rule {rule.name} reads personal column {', '.join(overlap)}",
                    "rules",
                    i,
                    "expr",
                )
        for i, rel in enumerate(e.relationships or []):
            if self.view.entity(rel.to) is None:
                self.add(
                    "V07",
                    f"relationship {rel.name} points to entity {rel.to}, which does not exist",
                    "relationships",
                    i,
                    "to",
                )
        for i, measure in enumerate(e.measures or []):
            self.measure_checks(e, measure, i, personal_columns, rules)

    def check_expression(self, sql: str, what: str, *where: str | int) -> None:
        try:
            tree = _parse(sql)
        except _ExprError as error:
            self.add("V05", f"{what}: expression {error}", *where)
            return
        banned = _forbidden(tree)
        if banned is not None:
            self.add("V05", f"{what}: expression contains {banned}", *where)

    def v11_masking_fits(self, dim: Dimension, index: int) -> None:
        if dim.masking is None:
            return
        fits = {"truncate_date": "time", "bucket": "numeric", "redact": "categorical"}
        wanted = fits[dim.masking.method]
        if dim.type != wanted:
            self.add(
                "V11",
                f"masking {dim.masking.method} needs a {wanted} dimension, "
                f"but {dim.name} is {dim.type}",
                "dimensions",
                index,
                "masking",
            )

    def measure_checks(
        self, e: Entity, m: Measure, index: int, personal_columns: set[str], rules: set[str]
    ) -> None:
        what = f"measure {m.name}"
        if m.expr is not None:
            self.check_expression(m.expr, what, "measures", index, "expr")
        if m.filter is not None:
            self.check_expression(m.filter, what, "measures", index, "filter")
        time_dimension = _dimension(e, m.time_dimension)
        if time_dimension is None or time_dimension.type != "time":
            found = "does not exist" if time_dimension is None else f"is {time_dimension.type}"
            self.add(
                "V06",
                f"{what}: time_dimension {m.time_dimension} must be a time dimension of "
                f"{e.name}, but it {found}",
                "measures",
                index,
                "time_dimension",
            )
        elif _is_personal(e, time_dimension):
            self.add(
                "V16",
                f"{what}: time_dimension {m.time_dimension} is personal",
                "measures",
                index,
                "time_dimension",
            )
        if m.agg not in NOT_AGGREGATE_AGGS:
            read = sorted((_columns(m.expr) | _columns(m.filter)) & personal_columns)
            if read:
                self.add(
                    "V12",
                    f"{what}: {m.agg} reads personal column {', '.join(read)}; "
                    "only count or count_distinct may",
                    "measures",
                    index,
                    "agg",
                )
        if m.rule is not None and m.rule not in rules:
            self.add(
                "V18",
                f"{what}: rule {m.rule} is not a rule of {e.name}",
                "measures",
                index,
                "rule",
            )

    # Metric: V05, V08, V09, V10, V13, V15, V18 -----------------------------------------

    def metric_checks(self, m: Metric) -> None:
        resolved = None
        if m.type == "simple":
            assert m.measure is not None
            resolved = self.view.resolve_measure(m.measure)
            if resolved is None:
                self.add("V08", self.unresolved_measure_message(m.measure), "measure")
            self.v09_join_paths(m, resolved)
            self.metric_filters(m, resolved)
        else:
            self.composite_inputs(m)
        self.v13_dependencies_approved(m)
        self.v15_grains(m)

    def unresolved_measure_message(self, ref: str) -> str:
        entity_name, _, measure_name = ref.partition(".")
        if self.view.entity(entity_name) is None:
            return f"measure {ref}: entity {entity_name} does not exist"
        return f"measure {ref}: entity {entity_name} has no measure {measure_name}"

    def composite_inputs(self, m: Metric) -> None:
        for field_name in ("numerator", "denominator"):
            ref = getattr(m, field_name)
            if ref is not None and self.view.metric(ref) is None:
                self.add("V08", f"{field_name} {ref} is not a metric", field_name)
        if m.type == "derived":
            for i, ref in enumerate(m.inputs or []):
                if self.view.metric(ref) is None:
                    self.add("V08", f"input {ref} is not a metric", "inputs", i)
            self.derived_expression(m)
        if self.on_cycle(m):
            self.add("V08", f"metric {m.name} depends on itself through its inputs")

    def on_cycle(self, m: Metric) -> bool:
        seen: set[str] = set()
        todo = _metric_inputs(m)
        while todo:
            name = todo.pop()
            if name == m.name:
                return True
            if name in seen:
                continue
            seen.add(name)
            other = self.view.metric(name)
            if other is not None:
                todo.extend(_metric_inputs(other))
        return False

    def derived_expression(self, m: Metric) -> None:
        assert m.expr is not None
        try:
            tree = _parse(m.expr)
        except _ExprError as error:
            self.add("V05", f"derived expression {error}", "expr")
            return
        inputs = set(m.inputs or [])
        problems: list[str] = []

        def visit(node: exp.Expr) -> None:
            if isinstance(node, exp.Column):
                if node.table or node.name not in inputs:
                    problems.append(f"{node.sql()} is not one of the inputs")
            elif isinstance(node, exp.Literal):
                if node.is_string:
                    problems.append(f"the text {node.sql()} is not allowed")
            elif not isinstance(node, DERIVED_NODES):
                problems.append(f"{type(node).__name__.upper()} is not allowed")
            else:
                for child in node.iter_expressions():
                    visit(child)

        visit(tree)
        for problem in dict.fromkeys(problems):
            self.add(
                "V08",
                f"derived expression may use only inputs, numbers, + - * /, parentheses, "
                f"NULLIF, and COALESCE: {problem}",
                "expr",
            )

    def v09_join_paths(self, m: Metric, resolved: tuple[Entity, Measure] | None) -> None:
        if resolved is None or m.join_paths is None:
            return
        start = resolved[0].name
        for target, chain in m.join_paths.items():
            problem = self.chain_problem(start, target, chain)
            if problem is not None:
                self.add("V09", f"join_paths {target}: {problem}", "join_paths", target)

    def chain_problem(self, start: str, target: str, chain: list[str]) -> str | None:
        current = start
        for step in chain:
            entity_name, _, relationship_name = step.partition(".")
            if entity_name != current:
                return f"step {step} starts at {entity_name} but the chain is at {current}"
            entity = self.view.entity(entity_name)
            relationship = None
            if entity is not None:
                relationship = next(
                    (r for r in entity.relationships or [] if r.name == relationship_name), None
                )
            if relationship is None:
                return f"step {step}: {entity_name} has no relationship {relationship_name}"
            current = relationship.to
        if current != target:
            return f"the chain ends at {current}, not at {target}"
        return None

    def metric_filters(self, m: Metric, resolved: tuple[Entity, Measure] | None) -> None:
        start = resolved[0].name if resolved is not None else None
        reachable = self.view.reachable(start) if start is not None else None
        for i, item in enumerate(m.filters or []):
            if isinstance(item, RuleRef):
                self.rule_reference(item, i, reachable)
            else:
                self.structured_filter(item, i, reachable, start)

    def rule_reference(self, ref: RuleRef, index: int, reachable: set[str] | None) -> None:
        entity_name, _, rule_name = ref.rule.partition(".")
        entity = self.view.entity(entity_name)
        where = ("filters", index, "rule")
        if entity is None or rule_name not in {r.name for r in entity.rules or []}:
            self.add("V18", f"rule {ref.rule} does not exist", *where)
        elif reachable is not None and entity_name not in reachable:
            self.add(
                "V18", f"rule {ref.rule}: {entity_name} is not reachable from the measure", *where
            )

    def structured_filter(
        self, flt: Filter, index: int, reachable: set[str] | None, start: str | None
    ) -> None:
        where = ("filters", index, "dimension")
        entity_name, _, dimension_name = flt.dimension.partition(".")
        entity = self.view.entity(entity_name)
        if entity is None:
            self.add(
                "V10", f"filter on {flt.dimension}: entity {entity_name} does not exist", *where
            )
            return
        if reachable is not None and entity_name not in reachable:
            self.add(
                "V10",
                f"filter on {flt.dimension}: {entity_name} is not reachable from {start}",
                *where,
            )
        dimension = _dimension(entity, dimension_name)
        if dimension is None:
            self.add("V10", f"filter on {flt.dimension}: no such dimension", *where)
            return
        if _is_personal(entity, dimension):
            self.add("V10", f"filter on {flt.dimension}: a personal dimension", *where)
            return
        problem = _value_problem(dimension, flt)
        if problem is not None:
            self.add("V10", f"filter on {flt.dimension}: {problem}", "filters", index, "value")

    def v13_dependencies_approved(self, m: Metric) -> None:
        if m.status not in ("approved", "in_review"):
            return
        entities: dict[str, None] = {}
        metrics: dict[str, None] = {}
        if m.type == "simple":
            assert m.measure is not None
            entities[m.measure.partition(".")[0]] = None
            for item in m.filters or []:
                ref = item.rule if isinstance(item, RuleRef) else item.dimension
                entities[ref.partition(".")[0]] = None
            for target, chain in (m.join_paths or {}).items():
                entities[target] = None
                for step in chain:
                    entities[step.partition(".")[0]] = None
        for name in _metric_inputs(m):
            metrics[name] = None
        for kind, names in (("entity", entities), ("metric", metrics)):
            for name in names:
                found = self.view.get(kind, name)
                if found is not None and found.status != "approved":
                    self.add("V13", f"depends on {kind} {name}, which is {found.status}")

    def v15_grains(self, m: Metric) -> None:
        if m.default_grain is not None and m.time_grains is not None:
            if m.default_grain not in m.time_grains:
                self.add(
                    "V15",
                    f"default_grain {m.default_grain} is not in time_grains",
                    "default_grain",
                )
        offered = m.time_grains if m.time_grains is not None else list(ALL_GRAINS)
        fine = [g for g in offered if g in FINE_GRAINS]
        if fine and self.has_balance(m, set()):
            how = "offers" if m.time_grains is not None else "offers by default"
            self.add(
                "V15",
                f"a balance (semi_additive) measure is among the inputs, but the metric {how} "
                f"{' and '.join(fine)}; set time_grains to month, quarter, or year",
                "time_grains",
            )

    def has_balance(self, m: Metric, seen: set[str]) -> bool:
        if m.name in seen:
            return False
        seen.add(m.name)
        if m.type == "simple":
            assert m.measure is not None
            resolved = self.view.resolve_measure(m.measure)
            return resolved is not None and resolved[1].semi_additive == "last"
        for name in _metric_inputs(m):
            other = self.view.metric(name)
            if other is not None and self.has_balance(other, seen):
                return True
        return False

    # V14 ----------------------------------------------------------------------------

    def v14_deprecation(self) -> None:
        d = self.d
        if d.status == "deprecated" and d.deprecation is None:
            self.add("V14", "a deprecated object needs deprecation.reason", "status")
        if d.deprecation is not None and d.deprecation.replaced_by is not None:
            ref = d.deprecation.replaced_by
            kind, _, name = ref.partition(":")
            if kind not in KINDS or not name or self.view.get(kind, name) is None:
                self.add(
                    "V14",
                    f"replaced_by {ref} does not name an existing object (kind:name)",
                    "deprecation",
                    "replaced_by",
                )


def _value_problem(dimension: Dimension, flt: Filter) -> str | None:
    if flt.op in ("is_null", "is_not_null"):
        return None
    if dimension.type == "boolean" and flt.op in (">", ">=", "<", "<="):
        return f"operator {flt.op} does not fit a boolean dimension"
    values = flt.value if isinstance(flt.value, list) else [flt.value]
    allowed = [v.value for v in dimension.values or []]
    for value in values:
        if not _type_fits(dimension.type, value):
            return f"value {value!r} does not fit a {dimension.type} dimension"
        if allowed and not any(
            isinstance(value, bool) == isinstance(a, bool) and value == a for a in allowed
        ):
            return f"value {value!r} is not one of the dimension's values"
    return None


def _type_fits(dimension_type: str, value: object) -> bool:
    if dimension_type == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if dimension_type == "numeric":
        return isinstance(value, (int, float))
    if dimension_type == "time":
        if not isinstance(value, str):
            return False
        try:
            datetime.datetime.fromisoformat(value)
        except ValueError:
            return False
        return True
    return isinstance(value, (str, int, float))  # categorical


# --------------------------------------------------------------------------------------


def _ordered(issues: Iterable[Issue]) -> list[Issue]:
    unique = dict.fromkeys(issues)
    return sorted(unique, key=lambda i: (i.file, i.line or 0, i.code, i.message))


def validate(definitions: DefinitionSet, extra_checks: Sequence[ExtraCheck] = ()) -> list[Issue]:
    """All definition errors of `definitions`, sorted by file and line."""
    valid = definitions.valid_files()
    repo = _Repository.build(valid)
    issues: list[Issue] = list(definitions.load_issues)
    for file in valid:
        issues.extend(_Checker(repo, file).run())
    for check in extra_checks:
        issues.extend(check(definitions))
    return _ordered(issues)


def codes(issues: Iterable[Issue]) -> set[str]:
    return {i.code for i in issues}


__all__ = ["ExtraCheck", "Issue", "codes", "validate"]
