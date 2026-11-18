"""The freshness check (ARCHITECTURE sections 6.3 and 13, WEB-1): how new is the data behind a
measure.

`refresh` asks the target engine, once for each approved measure, for the newest value of the
measure's time dimension (`SELECT MAX(<time dimension>)`, built as a sqlglot tree by the
connector) and stores the answer in `data_freshness`. This is the one module of `makna.services`,
`makna.mcp` and `makna.web` that calls the engine (D-21;
`tests/unit/test_engine_not_called_by_interfaces.py`).
The pages read the stored rows (`makna.services.discovery`) and never the engine.

A row is keyed by the measure, `measure:<entity>.<measure>`. These ids are not objects of the
index, so no foreign key and no page refers to them; the metric page derives its own freshness
from the rows of the measures its metric (and the metrics it is built from) reads.

An engine failure for one measure never stops the others. It is reported by the measure's id and
the error's class only: the text of an engine error can hold a value of the data
(`docs/notes/engine-error-text-can-carry-a-personal-value.md`). A measure that fails keeps its
earlier row, whose `checked_at` shows how old it is.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import sqlglot
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlglot import exp

from makna.compiler.snapshot import Model
from makna.engine.base import EngineConnector
from makna.index.tables import data_freshness
from makna.model.definitions import Entity, Measure
from makna.services.snapshot import SnapshotCache

logger = logging.getLogger(__name__)

MEASURE_PREFIX = "measure:"


def keep_engine_text_out_of_terminal() -> None:
    """For a command line or a job with no log handler of its own: the connector logs the text of
    an engine error (it can hold a value of the data), and Python's last-resort handler would
    print it. A handler on `makna.engine` stops that; only the measure and the error class are
    reported, by `refresh`."""
    logging.getLogger("makna.engine").addHandler(logging.NullHandler())


def measure_id(entity: str, measure: str) -> str:
    """The `data_freshness.object_id` of a measure."""
    return f"{MEASURE_PREFIX}{entity}.{measure}"


@dataclass(frozen=True)
class MeasureCheck:
    """What to ask the engine for one measure."""

    object_id: str
    table: str
    """The entity's `source.table`, as the catalog writes it (`catalog.schema.table`)."""
    time_expr: exp.Expr
    """The measure's time dimension as a sqlglot tree, columns unqualified and quoted."""


@dataclass(frozen=True)
class Failure:
    object_id: str
    error_class: str
    """The class name of the exception, never its text."""


@dataclass(frozen=True)
class RefreshResult:
    checked: int = 0
    """Measures the engine was asked about."""
    stored: int = 0
    """Rows written, a row with no date (an empty table) included."""
    unchanged: int = 0
    """Measures whose stored date was the same and was left as it was (`record_unchanged` off)."""
    empty: tuple[str, ...] = ()
    """Measures whose table has no rows, or whose time dimension is NULL everywhere."""
    failures: tuple[Failure, ...] = ()


def time_expression(entity: Entity, measure: Measure) -> exp.Expr:
    """The tree of the measure's time dimension: the dimension's `expr`, or the column of its own
    name. Raises `sqlglot.errors.SqlglotError` when the expression does not parse and `ValueError`
    when the entity has no such dimension."""
    dimension = next((d for d in entity.dimensions if d.name == measure.time_dimension), None)
    if dimension is None:
        raise ValueError("the measure's time dimension is not a dimension of its entity")
    if dimension.expr is None:
        return exp.column(dimension.name, quoted=True)
    tree = sqlglot.parse_one(dimension.expr, into=exp.Condition).copy()
    for column in tree.find_all(exp.Column):
        column.set("table", None)
        column.this.set("quoted", True)
    return tree


def measure_checks(model: Model) -> tuple[list[MeasureCheck], list[Failure]]:
    """One check for every measure of every served entity (approved, or deprecated and still
    served), in the order of entity and measure name; and the measures whose time dimension
    cannot be read, which are failures already."""
    checks: list[MeasureCheck] = []
    failures: list[Failure] = []
    for name in sorted(model.entities):
        entity = model.entities[name]
        for measure in sorted(entity.measures or [], key=lambda m: m.name):
            identifier = measure_id(entity.name, measure.name)
            try:
                tree = time_expression(entity, measure)
            except (sqlglot.errors.SqlglotError, ValueError) as error:
                failures.append(Failure(identifier, type(error).__name__))
                continue
            checks.append(MeasureCheck(identifier, entity.source.table, tree))
    return checks, failures


def refresh(
    database: Engine,
    connector: EngineConnector,
    *,
    cache: SnapshotCache | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    record_unchanged: bool = True,
    on_failure: Callable[[Failure], None] | None = None,
) -> RefreshResult:
    """Check every approved measure on the target engine and store what it answers.

    `database` is the application database (it holds `data_freshness` and the definitions);
    `connector` is the read-only engine connector. A measure that fails is reported to
    `on_failure` and in the result, and the next one is checked. With `record_unchanged` off, a
    measure whose date is the one already stored is not written again (`checked_at` stays), so a
    run that finds nothing new writes nothing: what the stack's `init` needs to stay idempotent.
    """
    snapshot, _ = (cache or SnapshotCache(database)).current()
    checks, unreadable = measure_checks(snapshot.model)
    failures: list[Failure] = list(unreadable)
    for failure in unreadable:
        if on_failure is not None:
            on_failure(failure)
    known = {} if record_unchanged else _stored_dates(database)
    empty: list[str] = []
    stored = unchanged = 0
    for check in checks:
        try:
            latest = connector.latest_value(check.table, check.time_expr)
        except Exception as error:  # EngineError, or anything a driver raises; the text is dropped
            failure = Failure(check.object_id, type(error).__name__)
            logger.warning(
                "freshness: check failed: object=%s error=%s",
                failure.object_id,
                failure.error_class,
            )
            failures.append(failure)
            if on_failure is not None:
                on_failure(failure)
            continue
        if latest is None:
            empty.append(check.object_id)
        if check.object_id in known and known[check.object_id] == latest:
            unchanged += 1
            continue
        _store(database, check.object_id, latest, now())
        stored += 1
    return RefreshResult(
        checked=len(checks),
        stored=stored,
        unchanged=unchanged,
        empty=tuple(empty),
        failures=tuple(failures),
    )


def _stored_dates(database: Engine) -> dict[str, date | None]:
    with database.connect() as connection:
        rows = connection.execute(select(data_freshness.c.object_id, data_freshness.c.latest_value))
        return {row.object_id: row.latest_value for row in rows}


def _store(database: Engine, object_id: str, latest: object, checked_at: datetime) -> None:
    statement = insert(data_freshness).values(
        object_id=object_id, latest_value=latest, checked_at=checked_at
    )
    statement = statement.on_conflict_do_update(
        index_elements=[data_freshness.c.object_id],
        set_={"latest_value": statement.excluded.latest_value, "checked_at": checked_at},
    )
    with database.begin() as connection:
        connection.execute(statement)
