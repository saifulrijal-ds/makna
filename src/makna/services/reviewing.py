"""What the review pages (WP-19B, WEB-3, GOV-1) need from the definition store, besides the two
decisions `Lifecycle.approve` and `Lifecycle.reject`: the review queue, one item with everything a
reviewer should see before deciding, and the audit log.

The types are here. The reads are methods of `Lifecycle` (`review_queue`, `review_item`,
`audit_events`) because they read the repository state the way the actions do. They are read-only:
no lock, no audit row, no `tool_call_log` row (they are not tools). The pure functions below
(`changed_rules`, `rule_uses`, `definition_marks`) work on parsed models only: never on text.

**D-17, the users of a rule.** A proposal that adds, changes or removes a rule of an entity lists
every measure and every metric that uses it. A *measure* uses a rule when its `rules` (or the
older `rule`) name it, in the proposal or in the official version, so a use that the proposal
removes is listed too. A *metric* uses it when it is an official metric that reads the rule through
a `RuleRef` filter, or through the measure it is built on, or, transitively, through the metrics
it is built from. `rules_by_metric` is the same reading as the indexer's `uses_rule` edges
(`indexer.object_edges`: a simple metric uses the rules of its measure and its rule filters, a
ratio or derived metric the rules of its inputs, found through `indexer.metric_inputs`) and a test
compares the two on the sample definitions. It differs in one way: it starts from the official
definitions, while the index also serves objects that have only a proposal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from makna.index.indexer import metric_inputs
from makna.model.definitions import (
    AnyDefinition,
    Dimension,
    Entity,
    Masking,
    MaskRedact,
    MaskTruncateDate,
    Metric,
    RuleRef,
)
from makna.model.expressions import ExpressionError, dimension_columns
from makna.services.editing import CheckIssue

MAX_QUEUE = 100
"""Entries of the review queue. More are counted (`ReviewQueue.total`) and not listed."""
MAX_QUEUE_ERRORS = 5
"""Definition errors shown on a queue row; the item page shows `MAX_ITEM_ISSUES`."""
MAX_ITEM_ISSUES = 50
MAX_REFERENCE_ISSUES = 20
MAX_AUTHORS = 20
MAX_RULES = 20
"""Changed rules listed for one proposal."""
MAX_RULE_USERS = 50
"""Measures, and metrics, listed for one rule (the totals are given)."""
MAX_MARKS = 50
MAX_TEXT = 200
"""A name, an expression or a message put into a mark is cut to this many characters."""
MAX_SUBJECT = 200
"""The commit subject shown for a change made outside Makna."""

DEFAULT_AUDIT_PAGE = 50
MAX_AUDIT_PAGE = 100

UNREADABLE_NOTE = (
    "This proposal cannot be read as a definition here (it is larger than 256 KiB, is not text, "
    "or does not parse), so there is no difference to show and it cannot be approved or "
    "rejected. It can only be withdrawn."
)


@dataclass(frozen=True)
class DecisionView:
    """`approval.may_decide` as a value: whether the caller may decide this object, and if not,
    the refusal code and the message that says why. `needs` lists what needs the governance team."""

    allowed: bool
    code: str | None = None
    message: str = ""
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutsideChange:
    """The commit behind a proposal that was made outside Makna. Git authorship is not
    authenticated: whoever can write the repository can put any name and email on a commit, so
    nothing here proves who made the change. Every text is untrusted."""

    commit: str
    author_name: str
    author_email: str
    subject: str
    """The first line of the commit's own message (as the committer wrote it)."""
    recorded: str | None
    """The reason Makna recorded when it noticed the commit (the `external_change` row), if it
    has noticed it yet."""


@dataclass(frozen=True)
class RuleUse:
    """A rule that the proposal adds, changes or removes, and who uses it (D-17)."""

    rule: str
    """`<entity>.<rule>`."""
    change: str
    """`added`, `changed` or `removed`."""
    measures: tuple[str, ...]
    """`<entity>.<measure>` of the measures that use it, in the proposal or the official version."""
    measures_total: int
    metrics: tuple[str, ...]
    """Names of the official metrics that use it, directly or through the metrics they combine."""
    metrics_total: int


@dataclass(frozen=True)
class Mark:
    """One fact a reviewer must look at that the YAML difference alone may not make obvious
    (I-4, I-4b, WP-15C M-4). `kind` is a fixed word; `text` holds names and expressions from the
    definitions, which are untrusted text."""

    kind: str
    text: str


@dataclass(frozen=True)
class ReviewEntry:
    """One proposal that is in review, as the queue lists it. Keyed by the object's path (its
    id), never by the name inside the file (review I-5)."""

    object_id: str
    kind: str
    name: str
    unreadable: bool
    """True when the proposal cannot be read as a definition: no difference, no decision."""
    is_new: bool
    """True when there is no official version yet."""
    version: int | None
    """The version of the official object (None for a new one)."""
    proposal_commit: str | None
    """The commit the proposal's content came from: what the form sends back as the expected
    commit, so that a reviewer decides on what was shown."""
    authors: tuple[str, ...]
    """Emails (lower case) of everyone who authored a commit since the last approval."""
    authors_total: int
    submitted_by: str | None
    """The actor of the `submit` that put the proposal in review (a label, untrusted)."""
    reason: str | None
    """The reason given at submit (untrusted). None for a change made outside Makna."""
    outside: OutsideChange | None
    lines_added: int | None
    lines_removed: int | None
    """Lines of the canonical form added and removed against the official version (a changed
    line counts as one of each); both None when the texts are too long to compare."""
    error_count: int
    errors: tuple[CheckIssue, ...]
    needs_governance: bool
    loosenings: tuple[str, ...]
    governance_team: str
    owner_change: tuple[str, str] | None
    """`(official owner team, proposed owner team)` when the proposal changes the owner."""
    rule_uses: tuple[RuleUse, ...]
    rule_uses_total: int
    decision: DecisionView
    """Whether this caller may decide the object (`approval.may_decide` for approve)."""


@dataclass(frozen=True)
class ReviewQueue:
    entries: tuple[ReviewEntry, ...]
    total: int
    """How many proposals are in review; `entries` holds at most `MAX_QUEUE`."""
    governance_team: str


@dataclass(frozen=True)
class ServiceRefusal:
    """A refusal of a service call, as a value: its code, its fixed message, and for a refusal
    because of definition errors the issues (at most 20)."""

    code: str
    message: str
    issues: tuple[CheckIssue, ...] = ()


@dataclass(frozen=True)
class ReviewItem:
    """One proposal with what the review page shows: the entry, the two texts to compare, the
    gate that approval would run, and the decisions this caller may make."""

    entry: ReviewEntry
    official_text: str | None
    """The official version in the canonical form of a proposal (status `draft`, the official
    version number): the left side of the difference. None for a new object."""
    proposal_text: str | None
    """The proposal in the same form: the right side. None when it cannot be read."""
    marks: tuple[Mark, ...]
    marks_total: int
    reference_issues: tuple[str, ...]
    """`"R01 column:..."`-style issues the proposed definition would have against the catalog."""
    reference_total: int
    approve_refusal: ServiceRefusal | None
    """Why approval would be refused now (definition errors, reference issues, the gate on the
    official files, the size), or None when it would go ahead. The Approve button is shown only
    when this is None and the caller may decide."""
    reject_decision: DecisionView
    can_withdraw: bool
    """True when `Lifecycle.withdraw` would allow this caller (the editor has the form)."""


@dataclass(frozen=True)
class AuditRow:
    """One `audit_event` row. `actor_label`, `reason` and `object_id` are untrusted text (an
    `external:` actor is whatever a commit's author field said); `failed` marks an attempt that
    was given up, which did not happen; `pending` an intent that no commit has completed yet."""

    id: int
    at: datetime
    actor_label: str
    action: str
    object_id: str
    from_status: str | None
    to_status: str | None
    reason: str | None
    commit_sha: str | None
    failed: bool
    pending: bool


@dataclass(frozen=True)
class AuditPage:
    rows: tuple[AuditRow, ...]
    older: int | None
    """The `before` value that gives the next, older page; None when this is the last one."""


# --- the models ------------------------------------------------------------------------------


def changed_rules(official: AnyDefinition | None, proposal: AnyDefinition) -> list[tuple[str, str]]:
    """`(rule name, "added" | "changed" | "removed")` for each rule of an entity that the
    proposal adds, changes or removes, sorted by name. Compared as parsed models. Empty for
    anything that is not an entity."""
    if not isinstance(proposal, Entity):
        return []
    before = {r.name: r for r in official.rules or []} if isinstance(official, Entity) else {}
    after = {r.name: r for r in proposal.rules or []}
    found: list[tuple[str, str]] = []
    for name in sorted(set(before) | set(after)):
        if name not in before:
            found.append((name, "added"))
        elif name not in after:
            found.append((name, "removed"))
        elif before[name].model_dump(mode="json") != after[name].model_dump(mode="json"):
            found.append((name, "changed"))
    return found


def _measure_rules(entity: Entity | None) -> dict[str, set[str]]:
    if entity is None:
        return {}
    return {m.name: set(m.rule_names) for m in entity.measures or []}


def rules_by_metric(
    entities: Mapping[str, Entity],
    metrics: Mapping[str, Metric],
    *,
    measure_rules: Mapping[str, Mapping[str, set[str]]] | None = None,
) -> dict[str, set[str]]:
    """For each metric name, the rule ids (`rule:<entity>.<rule>`) it uses, itself or through the
    metrics it is built from: the reading the indexer's `uses_rule` edges give (see the module
    text). `measure_rules` replaces the rules of some entities' measures (`entity -> measure ->
    rule names`), which is how a proposal's rules are added to the official ones."""
    direct: dict[str, set[str]] = {}
    for name, metric in metrics.items():
        used: set[str] = set()
        if metric.type == "simple" and metric.measure:
            entity_name, _, measure_name = metric.measure.partition(".")
            if measure_rules is not None and entity_name in measure_rules:
                rules = measure_rules[entity_name].get(measure_name, set())
            else:
                rules = _measure_rules(entities.get(entity_name)).get(measure_name, set())
            used |= {f"rule:{entity_name}.{rule}" for rule in rules}
            for item in metric.filters or []:
                if isinstance(item, RuleRef):
                    used.add(f"rule:{item.rule}")
        direct[name] = used
    inputs = {
        name: [i.removeprefix("metric:") for i in metric_inputs(m) if i.startswith("metric:")]
        for name, m in metrics.items()
    }
    result: dict[str, set[str]] = {}
    for name in metrics:
        seen: set[str] = set()
        stack = [name]
        reached: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen or current not in metrics:
                continue
            seen.add(current)
            reached |= direct[current]
            stack.extend(inputs[current])
        result[name] = reached
    return result


def rule_uses(
    officials: Mapping[str, AnyDefinition],
    official: AnyDefinition | None,
    proposal: AnyDefinition,
) -> tuple[tuple[RuleUse, ...], int]:
    """The D-17 listing for a proposal: for each rule it adds, changes or removes (at most
    `MAX_RULES`, with the count of all), the measures and the metrics that use it, each list cut
    at `MAX_RULE_USERS` with its total. `officials` is every official definition by id. The
    measures of the entity are read from the proposal and from the official version; the metrics
    from the official definitions, with the entity's measures as the union of both."""
    changes = changed_rules(official, proposal)
    if not changes or not isinstance(proposal, Entity):
        return (), 0
    entity = proposal.name
    before = _measure_rules(official if isinstance(official, Entity) else None)
    after = _measure_rules(proposal)
    merged = {
        name: before.get(name, set()) | after.get(name, set()) for name in set(before) | set(after)
    }
    entities = {o.name: o for o in officials.values() if isinstance(o, Entity)}
    metrics = {o.name: o for o in officials.values() if isinstance(o, Metric)}
    used = rules_by_metric(entities, metrics, measure_rules={entity: merged})
    listed: list[RuleUse] = []
    for rule, change in changes[:MAX_RULES]:
        measures = sorted(f"{entity}.{m}" for m, names in merged.items() if rule in names)
        users = sorted(name for name, rules in used.items() if f"rule:{entity}.{rule}" in rules)
        listed.append(
            RuleUse(
                f"{entity}.{rule}",
                change,
                tuple(measures[:MAX_RULE_USERS]),
                len(measures),
                tuple(users[:MAX_RULE_USERS]),
                len(users),
            )
        )
    return tuple(listed), len(changes)


# --- what to look at beyond the YAML difference ---------------------------------------------


def _cut(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _level(entity: Entity, dimension: Dimension) -> str:
    return dimension.sensitivity or entity.sensitivity


def _describe(masking: Masking | None) -> str:
    if masking is None:
        return "none"
    if isinstance(masking, MaskRedact):
        return "redact"
    if isinstance(masking, MaskTruncateDate):
        return f"truncate_date({masking.grain})"
    return f"bucket({masking.width:g})"


def _dimension_reads(dimension: Dimension) -> frozenset[str]:
    try:
        return frozenset(dimension_columns(dimension))
    except ExpressionError:
        return frozenset({f"?{dimension.name}"})


def _expr_key(dimension: Dimension) -> str:
    return (dimension.expr or dimension.name).strip().lower()


def definition_marks(official: AnyDefinition | None, proposal: AnyDefinition) -> list[Mark]:
    """The facts of a proposal that a reviewer must see next to the YAML difference, from the
    parsed models of `official` and `proposal` (not from text). For an entity: the dimensions
    removed or renamed (I-4), each dimension whose `expr`, `masking` or effective sensitivity
    changed, each new or changed dimension that reads the same column as a personal one (I-4b),
    and the relationships added, removed or changed (they act as roles, WP-15C M-4). For any
    object: a changed `owner.team`. A new object has nothing to compare, so it has no marks."""
    marks: list[Mark] = []
    if official is None:
        return marks
    if official.owner.team != proposal.owner.team:
        marks.append(
            Mark(
                "owner",
                f"owner team changes from {_cut(official.owner.team)} to "
                f"{_cut(proposal.owner.team)}",
            )
        )
    if not isinstance(official, Entity) or not isinstance(proposal, Entity):
        return marks
    old = {d.name: d for d in official.dimensions}
    new = {d.name: d for d in proposal.dimensions}
    added = [d for name, d in new.items() if name not in old]
    for name, dimension in old.items():
        if name in new:
            continue
        twin = next((d for d in added if _expr_key(d) == _expr_key(dimension)), None)
        if twin is not None:
            added.remove(twin)
            marks.append(
                Mark(
                    "dimension_renamed",
                    f"dimension {_cut(name)} was renamed to {_cut(twin.name)}",
                )
            )
        else:
            marks.append(Mark("dimension_removed", f"dimension {_cut(name)} was removed"))
    for name, after in new.items():
        before = old.get(name)
        if before is None:
            continue
        if (before.expr or "") != (after.expr or ""):
            marks.append(
                Mark(
                    "dimension_expr",
                    f"dimension {_cut(name)}: expr changed from "
                    f"{_cut(before.expr or '(its column)')} to "
                    f"{_cut(after.expr or '(its column)')}",
                )
            )
        if _describe(before.masking) != _describe(after.masking):
            marks.append(
                Mark(
                    "dimension_masking",
                    f"dimension {_cut(name)}: masking changed from {_describe(before.masking)} "
                    f"to {_describe(after.masking)}",
                )
            )
        level_before, level_after = _level(official, before), _level(proposal, after)
        if level_before != level_after:
            marks.append(
                Mark(
                    "dimension_sensitivity",
                    f"dimension {_cut(name)}: sensitivity changed from {level_before} "
                    f"to {level_after}",
                )
            )
    personal: dict[str, set[str]] = {}
    for entity, dimensions in ((official, old), (proposal, new)):
        for dimension in dimensions.values():
            if _level(entity, dimension) == "personal":
                for column in _dimension_reads(dimension):
                    personal.setdefault(column, set()).add(dimension.name)
    for name, dimension in new.items():
        before = old.get(name)
        if before is not None and _dimension_reads(before) == _dimension_reads(dimension):
            continue  # not new, and reads what it read: nothing changed here
        for column in sorted(_dimension_reads(dimension)):
            others = sorted(personal.get(column, set()) - {name})
            if others:
                marks.append(
                    Mark(
                        "same_column_as_personal",
                        f"dimension {_cut(name)} reads the column {_cut(column)}, which the "
                        f"personal dimension {_cut(others[0])} reads too",
                    )
                )
    before_rel = {r.name: r for r in official.relationships or []}
    after_rel = {r.name: r for r in proposal.relationships or []}
    for name in sorted(set(before_rel) | set(after_rel)):
        if name not in before_rel:
            marks.append(Mark("relationship_added", f"relationship {_cut(name)} was added"))
        elif name not in after_rel:
            marks.append(Mark("relationship_removed", f"relationship {_cut(name)} was removed"))
        elif before_rel[name].model_dump(mode="json", by_alias=True) != after_rel[name].model_dump(
            mode="json", by_alias=True
        ):
            marks.append(Mark("relationship_changed", f"relationship {_cut(name)} was changed"))
    return marks
