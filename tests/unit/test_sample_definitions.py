"""WP-04: the sample definitions of the lending domain (DAT-3, SEM-2).

The expectations come from `docs/SAMPLE_DOMAIN.md` (sections 3 and 5), parsed here, so a change to
the document that the definitions do not follow is caught. Only what the document states in
prose rather than in a table is written out below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
import sqlglot
import yaml
from sqlglot import exp

from data.generator import TABLES, get_table
from makna.cli.main import main
from makna.definitions.loader import load_definitions, load_text
from makna.definitions.validate import validate
from makna.model.definitions import (
    Dimension,
    Entity,
    Filter,
    GlossaryTerm,
    MaskTruncateDate,
    Measure,
    Metric,
    Relationship,
    Rule,
    RuleRef,
)

from .generator_support import ROOT, connect, doc_relationships, scalar, table_cells

DEFINITIONS = ROOT / "semantic" / "definitions"
EXAMPLES = ROOT / "semantic" / "schema" / "examples" / "lending"
LENDING = DEFINITIONS / "lending"


# --------------------------------------------------------------------------------------
# What the definitions say


@dataclass(frozen=True)
class Loaded:
    entities: dict[str, Entity]
    official_metrics: dict[str, Metric]
    draft_metrics: dict[str, Metric]
    glossary: dict[str, GlossaryTerm]


@cache
def loaded() -> Loaded:
    entities: dict[str, Entity] = {}
    official: dict[str, Metric] = {}
    drafts: dict[str, Metric] = {}
    glossary: dict[str, GlossaryTerm] = {}
    for file in load_definitions(DEFINITIONS).files:
        definition = file.definition
        if isinstance(definition, Entity):
            entities[definition.name] = definition
        elif isinstance(definition, Metric):
            (drafts if file.is_proposal else official)[definition.name] = definition
        elif isinstance(definition, GlossaryTerm):
            glossary[definition.name] = definition
    return Loaded(entities, official, drafts, glossary)


def approved_metrics() -> dict[str, Metric]:
    return {n: m for n, m in loaded().official_metrics.items() if m.status == "approved"}


def columns_of(sql: str) -> set[str]:
    return {c.name for c in sqlglot.parse_one(sql, into=exp.Condition).find_all(exp.Column)}


def condition(sql: str) -> exp.Expr:
    return sqlglot.parse_one(sql, into=exp.Condition)


def table_name(entity: Entity) -> str:
    return entity.source.table.split(".")[-1]


# --------------------------------------------------------------------------------------
# What the document says (section 5)


@dataclass(frozen=True)
class DocMetric:
    name: str
    starred: bool
    type: str
    built_from: str
    time: str
    branch: str
    owner_team: str


def _section_5() -> list[str]:
    """Every line of section 5, up to the next top-level heading."""
    lines = (ROOT / "docs" / "SAMPLE_DOMAIN.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## 5. Metrics"))
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        out.append(line)
    return out


@cache
def doc_metrics() -> tuple[DocMetric, ...]:
    out: list[DocMetric] = []
    team = ""
    for line in _section_5():
        heading = re.match(r"### .*owner team `([\w-]+)`", line)
        if heading:
            team = heading.group(1)
        elif line.startswith("### "):
            team = ""
        if not team or not re.match(r"\| \d+ \|", line):
            continue
        cells = table_cells(line)
        name_cell = cells[1]
        out.append(
            DocMetric(
                name=name_cell.split("`")[1],
                starred="★" in name_cell,
                type=cells[2],
                built_from=cells[4],
                time=cells[5],
                branch=cells[6].strip("`"),
                owner_team=team,
            )
        )
    return tuple(out)


def doc_rules() -> list[tuple[str, str, str]]:
    """The rules table: (entity, rule, condition)."""
    out = []
    in_rules = False
    for line in _section_5():
        if line.startswith("### Rules"):
            in_rules = True
        elif line.startswith("### "):
            in_rules = False
        if in_rules and line.startswith("| `"):
            cells = table_cells(line)
            entity, rule = cells[0].split("`")[1].split(".")
            out.append((entity, rule, cells[1].strip("`")))
    return out


def doc_glossary_terms() -> list[str]:
    line = next(x for x in _section_5() if x.startswith("At least these eight"))
    return re.findall(r"`(\w+)`", line)


def doc_governance_fixtures() -> dict[str, str]:
    out = {}
    in_table = False
    for line in _section_5():
        if line.startswith("### Governance fixtures"):
            in_table = True
        elif line.startswith("### "):
            in_table = False
        if in_table and line.startswith("| `"):
            cells = table_cells(line)
            out[cells[0].strip("`")] = cells[1]
    return out


def doc_entity_names() -> list[str]:
    line = next(
        x
        for x in (ROOT / "docs" / "SAMPLE_DOMAIN.md").read_text(encoding="utf-8").splitlines()
        if x.startswith("Entity names in the semantic model")
    )
    return re.findall(r"`(\w+)`", line)


# The document names the official date of a metric in words. One of them differs from the
# dimension name that the schema examples already use.
TIME_WORDS = {"registration date": "registered_date"}


def expected_time_dimension(time: str) -> str:
    return TIME_WORDS.get(time, time.replace(" ", "_"))


def expected_entity(built_from: str) -> str:
    """The entity a simple metric's measure sits on, from the 'Built from' cell."""
    first = re.search(r"`([^`]+)`", built_from)
    assert first, built_from
    token = first.group(1)
    if "." in token:  # `contract_snapshot.outstanding_principal`
        return token.split(".")[0]
    if token in doc_entity_names():  # count of `application` rows
        return token
    column = re.findall(r"\w+", token)[0]  # sum of `principal_paid + interest_paid`
    tables = [t for t in TABLES if column in t.column_names]
    assert len(tables) == 1, (column, [t.name for t in tables])
    return tables[0].entity


def expected_branch_link(cell: str) -> str:
    return {
        "booking": "contract.booking_branch",
        "servicing": "contract.servicing_branch",
    }.get(cell, cell)


def simple_inputs(metric: Metric) -> list[Metric]:
    """The simple metrics a metric is built from (itself, if it is simple)."""
    if metric.type == "simple":
        return [metric]
    assert metric.numerator and metric.denominator
    names = [metric.numerator, metric.denominator]
    return [leaf for n in names for leaf in simple_inputs(loaded().official_metrics[n])]


# --------------------------------------------------------------------------------------
# Criterion 1


def test_validate_exits_zero_on_the_sample_definitions(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(DEFINITIONS)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_the_loader_finds_no_problem_and_every_file_is_read() -> None:
    definitions = load_definitions(DEFINITIONS)
    assert definitions.load_issues == []
    assert all(f.definition is not None for f in definitions.files)
    assert [i.format() for i in validate(definitions)] == []
    # 1 domain + 9 entities + 23 approved + 1 deprecated + 1 draft metric + 8 glossary terms
    assert len(definitions.files) == 1 + 9 + 23 + 1 + 1 + 8


def test_the_file_set_follows_the_plan() -> None:
    assert sorted(p.name for p in (LENDING / "entities").glob("*.yaml")) == sorted(
        f"{n}.yaml" for n in doc_entity_names()
    )
    assert len(doc_entity_names()) == 9
    proposals = sorted(p.name for p in (LENDING / "metrics").glob("*.draft.yaml"))
    assert proposals == ["write_off_amount.draft.yaml"]
    assert (LENDING / "metrics" / "npl_ratio.yaml").is_file()
    assert not (LENDING / "metrics" / "write_off_amount.yaml").exists()
    assert (LENDING / "domain.yaml").is_file()


def test_the_governance_fixtures_have_the_state_the_document_gives() -> None:
    fixtures = doc_governance_fixtures()
    assert set(fixtures) == {"write_off_amount", "npl_ratio"}
    assert "ai:sample-assistant" in fixtures["write_off_amount"]
    draft = loaded().draft_metrics["write_off_amount"]
    assert draft.status == "draft"
    assert draft.drafted_by == "ai:sample-assistant"
    deprecated = loaded().official_metrics["npl_ratio"]
    assert deprecated.status == "deprecated"
    assert deprecated.deprecation is not None
    assert deprecated.deprecation.replaced_by == "metric:npf_ratio"
    synonyms = {s for words in deprecated.synonyms.values() for s in words}
    assert {"NPL", "kredit macet"} <= synonyms
    assert loaded().official_metrics["npf_ratio"].status == "approved"


# --------------------------------------------------------------------------------------
# Criterion 2


def test_the_document_lists_23_metrics() -> None:
    rows = doc_metrics()
    assert len(rows) == 23
    assert len({r.name for r in rows}) == 23
    assert {r.type for r in rows} == {"simple", "ratio"}


def test_the_approved_metrics_are_exactly_the_23_of_section_5() -> None:
    assert set(approved_metrics()) == {r.name for r in doc_metrics()}
    assert len(approved_metrics()) == 23


@pytest.mark.parametrize("row", doc_metrics(), ids=lambda r: r.name)
def test_metric_type_owner_and_inputs_follow_the_table(row: DocMetric) -> None:
    metric = approved_metrics()[row.name]
    assert metric.type == row.type
    assert metric.owner.team == row.owner_team
    if row.type == "ratio":
        names = re.search(r"`(\w+) / (\w+)`", row.built_from)
        assert names, row.built_from
        assert (metric.numerator, metric.denominator) == names.groups()


@pytest.mark.parametrize(
    "row", [r for r in doc_metrics() if r.type == "simple"], ids=lambda r: r.name
)
def test_simple_metric_measure_entity_and_time_dimension_follow_the_table(row: DocMetric) -> None:
    metric = approved_metrics()[row.name]
    assert metric.measure is not None
    entity_name, measure_name = metric.measure.split(".")
    assert entity_name == expected_entity(row.built_from)
    entity = loaded().entities[entity_name]
    measure = next(m for m in entity.measures or [] if m.name == measure_name)
    assert measure.time_dimension == expected_time_dimension(row.time)
    time_dimension = next(d for d in entity.dimensions if d.name == measure.time_dimension)
    assert time_dimension.type == "time"


def test_balance_metrics_are_semi_additive_and_the_flow_metrics_are_not() -> None:
    for row in doc_metrics():
        if row.type != "simple":
            continue
        entity_name, measure_name = approved_metrics()[row.name].measure.split(".")  # type: ignore[union-attr]
        measure = next(
            m for m in loaded().entities[entity_name].measures or [] if m.name == measure_name
        )
        assert (measure.semi_additive == "last") == (row.time == "snapshot date"), row.name


# --------------------------------------------------------------------------------------
# Criterion 3


def all_metrics() -> list[Metric]:
    return [*loaded().official_metrics.values(), *loaded().draft_metrics.values()]


def test_every_metric_entity_and_glossary_term_has_english_and_indonesian_synonyms() -> None:
    objects: list[Metric | Entity | GlossaryTerm] = [
        *all_metrics(),
        *loaded().entities.values(),
        *loaded().glossary.values(),
    ]
    assert len(objects) == 25 + 9 + 8
    for obj in objects:
        assert obj.synonyms["en"], obj.name
        assert obj.synonyms["id"], obj.name


def test_the_schema_enforces_the_two_languages() -> None:
    text = (LENDING / "metrics" / "approval_rate.yaml").read_text(encoding="utf-8")
    without_indonesian = re.sub(r"  id: \[.*\]\n", "", text)
    assert without_indonesian != text
    _, issues = load_text("lending/metrics/approval_rate.yaml", without_indonesian)
    assert [i.code for i in issues] == ["V01"]
    assert "'id'" in issues[0].message


def test_every_metric_has_two_example_questions() -> None:
    for metric in all_metrics():
        assert len(metric.example_questions or []) >= 2, metric.name


def test_every_approved_metric_has_an_example_request() -> None:
    # Draft and deprecated metrics are refused by the compiler, so a request for them could
    # never pass V19; D-18 asks for examples on approved metrics.
    for name, metric in approved_metrics().items():
        assert metric.example_requests, name
        for example in metric.example_requests:
            assert example.question.strip(), name
    for metric in (
        loaded().draft_metrics["write_off_amount"],
        loaded().official_metrics["npl_ratio"],
    ):
        assert not metric.example_requests


def test_every_dimension_measure_rule_and_relationship_has_a_description() -> None:
    for entity in loaded().entities.values():
        parts: list[Dimension | Measure | Rule | Relationship] = [
            *entity.dimensions,
            *(entity.measures or []),
            *(entity.rules or []),
            *(entity.relationships or []),
        ]
        assert parts
        for part in parts:
            assert len(part.description.strip()) >= 10, (entity.name, part.name)


def test_every_metric_has_a_description_a_label_and_a_format() -> None:
    for metric in all_metrics():
        assert len(metric.description) >= 20, metric.name
        assert metric.label.strip(), metric.name
        assert metric.format is not None


def test_the_legacy_entities_give_every_dimension_synonyms_in_both_languages() -> None:
    for name in ("application", "collection_action"):
        for dimension in loaded().entities[name].dimensions:
            assert dimension.synonyms is not None, (name, dimension.name)
            assert dimension.synonyms.get("en"), (name, dimension.name)
            assert dimension.synonyms.get("id"), (name, dimension.name)


# --------------------------------------------------------------------------------------
# Criterion 4


@pytest.mark.parametrize("row", doc_metrics(), ids=lambda r: r.name)
def test_branch_link_is_pinned_as_the_table_says(row: DocMetric) -> None:
    metric = approved_metrics()[row.name]
    expected = expected_branch_link(row.branch)
    if metric.type == "simple":
        assert metric.join_paths is not None, row.name
        pinned = metric.join_paths["branch"]
        assert pinned[-1] == expected, row.name
        if row.branch in ("booking", "servicing"):
            assert pinned[-1].endswith(f"contract.{row.branch}_branch")
    else:
        # A ratio cannot carry join_paths (schema); every input pins the link the row names.
        assert metric.join_paths is None
        inputs = simple_inputs(metric)
        assert len(inputs) >= 2
        for leaf in inputs:
            assert leaf.join_paths is not None, (row.name, leaf.name)
            assert leaf.join_paths["branch"][-1] == expected, (row.name, leaf.name)


def test_sales_metrics_use_the_booking_branch_and_portfolio_metrics_the_servicing_branch() -> None:
    by_name = {r.name: r for r in doc_metrics()}
    booking = {n for n, r in by_name.items() if r.branch == "booking"}
    servicing = {n for n, r in by_name.items() if r.branch == "servicing"}
    assert booking == {"disbursement_amount", "disbursed_contract_count", "average_ticket_size"}
    assert len(servicing) == 15  # every portfolio and collection metric


# --------------------------------------------------------------------------------------
# Criterion 5

PERSONAL_COLUMNS = ("full_name", "national_id", "phone_number", "email", "birth_date")


def test_customers_has_exactly_the_five_personal_columns() -> None:
    assert set(get_table("customers").personal_columns) == set(PERSONAL_COLUMNS)


def test_each_personal_column_is_read_by_a_personal_dimension() -> None:
    customer = loaded().entities["customer"]
    personal = [d for d in customer.dimensions if d.sensitivity == "personal"]
    for column in PERSONAL_COLUMNS:
        readers = [d for d in personal if columns_of(d.expr or d.name) == {column}]
        assert len(readers) == 1, column
    # no other dimension of the entity reads a personal column (V20 holds this too)
    for dimension in customer.dimensions:
        if dimension.sensitivity != "personal":
            assert not columns_of(dimension.expr or dimension.name) & set(PERSONAL_COLUMNS)


def test_only_birth_date_is_masked_and_only_by_truncating_to_a_year() -> None:
    customer = loaded().entities["customer"]
    for dimension in customer.dimensions:
        if dimension.sensitivity != "personal":
            assert dimension.masking is None, dimension.name
            continue
        column = next(iter(columns_of(dimension.expr or dimension.name)))
        if column == "birth_date":
            # the only allowed mask is a date truncated to a year (a month would be finer)
            assert isinstance(dimension.masking, MaskTruncateDate)
            assert dimension.masking.grain == "year"
        else:
            assert dimension.masking is None, dimension.name
    # the example dimension keeps the year grain
    year = next(d for d in customer.dimensions if d.name == "birth_year")
    assert isinstance(year.masking, MaskTruncateDate)


def test_personal_dimensions_list_no_values() -> None:
    for dimension in loaded().entities["customer"].dimensions:
        if dimension.sensitivity == "personal":
            assert dimension.values is None, dimension.name


# --------------------------------------------------------------------------------------
# Criterion 6


def test_the_seven_starred_metrics_are_the_example_metrics() -> None:
    starred = {r.name for r in doc_metrics() if r.starred}
    examples = {p.stem for p in (EXAMPLES / "metrics").glob("*.yaml")}
    assert len(starred) == 7
    assert starred == examples


@pytest.mark.parametrize(
    "example", sorted((EXAMPLES / "metrics").glob("*.yaml")), ids=lambda p: p.stem
)
def test_example_metric_files_are_copied_unchanged(example: Path) -> None:
    copy = LENDING / "metrics" / example.name
    assert copy.read_bytes() == example.read_bytes()


def test_the_starred_glossary_term_and_the_domain_are_copied_unchanged() -> None:
    assert (LENDING / "glossary" / "npf.yaml").read_bytes() == (
        EXAMPLES / "glossary" / "npf.yaml"
    ).read_bytes()
    assert (LENDING / "domain.yaml").read_bytes() == (EXAMPLES / "domain.yaml").read_bytes()


# --------------------------------------------------------------------------------------
# Criterion 7 (rules, D-17)


def test_the_rules_of_the_document_exist_with_the_stated_condition() -> None:
    rules = doc_rules()
    assert {(e, r) for e, r, _ in rules} == {
        ("contract_snapshot", "non_performing"),
        ("contract_snapshot", "past_due_30"),
        ("contract_snapshot", "write_off_position"),
        ("contract", "restructured"),
    }
    declared = {(e.name, r.name) for e in loaded().entities.values() for r in e.rules or []}
    assert declared == {(e, r) for e, r, _ in rules}
    for entity_name, rule_name, expected in rules:
        entity = loaded().entities[entity_name]
        rule = next(r for r in entity.rules or [] if r.name == rule_name)
        assert condition(rule.expr) == condition(expected), (entity_name, rule_name)


def test_every_npf_and_par_measure_uses_a_rule() -> None:
    expected = {
        "npf_outstanding_principal": "non_performing",
        "par30_outstanding_principal": "past_due_30",
    }
    seen = set()
    for entity in loaded().entities.values():
        for measure in entity.measures or []:
            if measure.name.startswith(("npf", "par")):
                assert measure.rule == expected[measure.name], measure.name
                seen.add(measure.name)
    assert seen == set(expected)


def test_the_restructured_metric_filters_on_the_contract_rule() -> None:
    metric = approved_metrics()["restructured_outstanding_principal"]
    assert metric.measure == "contract_snapshot.outstanding_principal"
    assert metric.filters is not None
    assert [f for f in metric.filters] == [RuleRef(rule="contract.restructured")]
    # the rule sits on contract, which the snapshot reaches through its one relationship
    snapshot = loaded().entities["contract_snapshot"]
    assert [(r.name, r.to) for r in snapshot.relationships or []] == [("contract", "contract")]


def test_no_measure_or_metric_filter_repeats_a_condition_that_a_rule_names() -> None:
    rule_trees = [
        (entity.name, rule.name, condition(rule.expr))
        for entity in loaded().entities.values()
        for rule in entity.rules or []
    ]
    assert len(rule_trees) == 4
    for entity in loaded().entities.values():
        for measure in entity.measures or []:
            if measure.filter is None:
                continue
            tree = condition(measure.filter)
            nodes = list(tree.walk())
            for rule_entity, rule_name, rule_tree in rule_trees:
                assert rule_tree not in nodes, (entity.name, measure.name, rule_entity, rule_name)
    # structured metric filters: none reads a column that a rule of the same entity reads
    for metric in all_metrics():
        for item in metric.filters or []:
            if not isinstance(item, Filter):
                continue
            entity_name, dimension_name = item.dimension.split(".")
            entity = loaded().entities[entity_name]
            dimension = next(d for d in entity.dimensions if d.name == dimension_name)
            read = columns_of(dimension.expr or dimension.name)
            for rule in entity.rules or []:
                assert not read & columns_of(rule.expr), (metric.name, item.dimension, rule.name)


# --------------------------------------------------------------------------------------
# Section 3 and 5: dimensions that must exist, relationships, and the physical columns

MUST_EXIST = [
    # (entity, dimension, type, expression over the physical columns)
    ("application", "submitted_date", "time", "CAST(tgl_pengajuan AS DATE)"),
    ("application", "decision_date", "time", "tgl_putusan"),
    ("application", "decision_status", "categorical", "sts_putusan"),
    ("application", "channel", "categorical", "saluran"),
    ("application", "risk_grade", "categorical", "grade_risiko"),
    ("collection_action", "action_date", "time", "tgl_aksi"),
    ("collection_action", "action_type", "categorical", "jns_aksi"),
    ("collection_action", "outcome", "categorical", "hasil"),
    ("customer", "gender", "categorical", "gender"),
    ("customer", "income_band", "categorical", "income_band"),
    ("customer", "city", "categorical", "city"),
    ("customer", "email", "categorical", "email"),
    ("branch", "branch_code", "categorical", "branch_code"),
    ("branch", "is_active", "boolean", "is_active"),
    ("contract", "annual_rate_pct", "numeric", "annual_rate_pct"),
    ("contract", "closed_date", "time", "closed_date"),
    ("contract", "restructured_date", "time", "restructured_date"),
]


@pytest.mark.parametrize(("entity_name", "name", "dimension_type", "sql"), MUST_EXIST)
def test_dimensions_that_must_exist(
    entity_name: str, name: str, dimension_type: str, sql: str
) -> None:
    entity = loaded().entities[entity_name]
    dimension = next((d for d in entity.dimensions if d.name == name), None)
    assert dimension is not None
    assert dimension.type == dimension_type
    assert condition(dimension.expr or dimension.name) == condition(sql)


def test_the_nine_entities_of_the_document_exist_on_the_nine_tables() -> None:
    assert set(loaded().entities) == set(doc_entity_names())
    for entity in loaded().entities.values():
        spec = get_table(table_name(entity))
        assert entity.source.table == f"lending.core.{spec.name}"
        assert spec.entity == entity.name
        assert list(entity.primary_key) == list(spec.primary_key)


def test_every_relationship_of_section_3_10_is_declared_with_the_right_keys() -> None:
    declared: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for entity in loaded().entities.values():
        for relationship in entity.relationships or []:
            keys = [(k.from_, k.to) for k in relationship.keys]
            declared[f"{entity.name}.{relationship.name}"] = (relationship.to, keys)
    tables = {t.name: t for t in TABLES}
    expected = {}
    for _, column, ref_table, name in doc_relationships():
        ref = tables[ref_table]
        expected[name] = (ref.entity, [(column, ref.primary_key[0])])
    assert len(expected) == 14
    assert declared == expected


# The cheap early guard until the catalog reference check (R02, WP-06) exists.
def test_every_physical_column_an_entity_reads_exists_in_its_table() -> None:
    tables = {t.name: t for t in TABLES}
    for entity in loaded().entities.values():
        own = set(get_table(table_name(entity)).column_names)
        reads: list[tuple[str, str, set[str]]] = [("primary_key", "", set(entity.primary_key))]
        for dimension in entity.dimensions:
            reads.append(
                ("dimension", dimension.name, columns_of(dimension.expr or dimension.name))
            )
        for measure in entity.measures or []:
            reads.append(
                ("measure expr", measure.name, columns_of(measure.expr) if measure.expr else set())
            )
            reads.append(
                (
                    "measure filter",
                    measure.name,
                    columns_of(measure.filter) if measure.filter else set(),
                )
            )
        for rule in entity.rules or []:
            reads.append(("rule", rule.name, columns_of(rule.expr)))
        for relationship in entity.relationships or []:
            reads.append(
                ("relationship from", relationship.name, {k.from_ for k in relationship.keys})
            )
            target = tables[table_name(loaded().entities[relationship.to])]
            missing_target = {k.to for k in relationship.keys} - set(target.column_names)
            assert not missing_target, (entity.name, relationship.name, missing_target)
        for kind, name, columns in reads:
            assert columns <= own, (entity.name, kind, name, sorted(columns - own))
        # every measure that is not a plain count reads something
        for measure in entity.measures or []:
            assert measure.agg == "count" or measure.expr, measure.name


def test_the_legacy_entities_use_clean_names_for_dimensions() -> None:
    for entity_name, table in (
        ("application", "loan_applications"),
        ("collection_action", "collection_actions"),
    ):
        legacy = {c.name for c in get_table(table).columns if c.semantic_name}
        entity = loaded().entities[entity_name]
        assert not {d.name for d in entity.dimensions} & legacy
        assert not {m.name for m in entity.measures or []} & legacy
        # each abbreviated column is read by some dimension or measure or relationship key
        readers = set()
        for dimension in entity.dimensions:
            readers |= columns_of(dimension.expr or dimension.name)
        for measure in entity.measures or []:
            readers |= columns_of(measure.filter) if measure.filter else set()
        for relationship in entity.relationships or []:
            readers |= {k.from_ for k in relationship.keys}
        assert {"sts_putusan", "saluran", "grade_risiko"} <= readers or entity_name != "application"
        assert {"jns_aksi", "hasil", "tgl_aksi"} <= readers or entity_name != "collection_action"


# --------------------------------------------------------------------------------------
# Glossary and the rest of section 5


def test_the_glossary_has_the_terms_of_the_document_with_related_objects() -> None:
    terms = doc_glossary_terms()
    assert len(terms) == 8
    assert set(loaded().glossary) == set(terms)
    kinds = {
        "metric": set(loaded().official_metrics),
        "entity": set(loaded().entities),
        "glossary_term": set(loaded().glossary),
    }
    for term in loaded().glossary.values():
        assert term.related, term.name
        for ref in term.related:
            kind, name = ref.split(":")
            assert name in kinds[kind], (term.name, ref)


def test_the_documented_indonesian_synonyms_are_present() -> None:
    expected = {
        "days_past_due": {"hari keterlambatan", "tunggakan"},
        "collectibility": {"kolektibilitas", "kol"},
        "disbursement": {"pencairan"},
        "restructuring": {"restrukturisasi"},
        "outstanding": {"baki debet", "sisa pokok"},
    }
    for name, words in expected.items():
        assert words <= set(loaded().glossary[name].synonyms["id"]), name
    assert "DPD" in loaded().glossary["days_past_due"].synonyms["en"]
    assert "PAR" in loaded().glossary["portfolio_at_risk"].synonyms["en"]


# --------------------------------------------------------------------------------------
# What each simple metric's measure computes (SAMPLE_DOMAIN section 5, "Built from")

# (metric, measure, agg, expr, row filter, rule). Each row restates the "Built from" cell of the
# metric's row in SAMPLE_DOMAIN section 5. A filter on a legacy table uses its physical column
# (for example `sts_putusan` is the dimension `decision_status`).
MEASURE_TABLE: list[tuple[str, str, str, str | None, str | None, str | None]] = [
    # 1 count of `application` rows
    ("application_count", "application.submitted_applications", "count", None, None, None),
    # 2 count of `application` where `decision_status IN ('approved','rejected')`
    (
        "decided_application_count",
        "application.decided_applications",
        "count",
        None,
        "sts_putusan IN ('approved','rejected')",
        None,
    ),
    # 3 count of `application` where `decision_status = 'approved'`
    (
        "approved_application_count",
        "application.approved_applications",
        "count",
        None,
        "sts_putusan = 'approved'",
        None,
    ),
    # 5 sum of `principal_amount` where `disbursement_date` is not null (cancelled excluded, Q2)
    (
        "disbursement_amount",
        "contract.disbursed_principal",
        "sum",
        "principal_amount",
        "disbursement_date IS NOT NULL",
        None,
    ),
    # 6 count of `contract` where `disbursement_date` is not null
    (
        "disbursed_contract_count",
        "contract.disbursed_contracts",
        "count",
        None,
        "disbursement_date IS NOT NULL",
        None,
    ),
    # 8 count of `customer` rows
    ("new_customer_count", "customer.new_customers", "count", None, None, None),
    # 9 sum of `outstanding_principal`
    (
        "outstanding_principal",
        "contract_snapshot.outstanding_principal",
        "sum",
        "outstanding_principal",
        None,
        None,
    ),
    # 10 count of `contract_snapshot` rows
    ("active_contract_count", "contract_snapshot.active_contracts", "count", None, None, None),
    # 11 sum of `outstanding_principal` where `days_past_due > 90` (rule non_performing, D-17)
    (
        "npf_outstanding_principal",
        "contract_snapshot.npf_outstanding_principal",
        "sum",
        "outstanding_principal",
        None,
        "non_performing",
    ),
    # 13 sum of `outstanding_principal` where `days_past_due > 30` (rule past_due_30, D-17)
    (
        "par30_outstanding_principal",
        "contract_snapshot.par30_outstanding_principal",
        "sum",
        "outstanding_principal",
        None,
        "past_due_30",
    ),
    # 15 `contract_snapshot.outstanding_principal` with the metric filter
    #    {rule: contract.restructured}
    (
        "restructured_outstanding_principal",
        "contract_snapshot.outstanding_principal",
        "sum",
        "outstanding_principal",
        None,
        None,
    ),
    # 17 sum of `installment_amount` where `is_current` (replaced rows excluded, Q4)
    ("due_amount", "installment.due_amount", "sum", "installment_amount", "is_current", None),
    # 18 sum of `principal_paid + interest_paid` where not reversed (Q6)
    (
        "collected_amount",
        "payment.collected_amount",
        "sum",
        "principal_paid + interest_paid",
        "NOT is_reversed",
        None,
    ),
    # 20 sum of `penalty_paid` where not reversed (Q6)
    ("penalty_income", "payment.penalty_received", "sum", "penalty_paid", "NOT is_reversed", None),
    # 21 count of `collection_action` rows
    ("collection_action_count", "collection_action.collection_actions", "count", None, None, None),
    # 22 count of `collection_action` where `outcome = 'promise_to_pay'`
    (
        "promise_to_pay_count",
        "collection_action.promise_to_pay_actions",
        "count",
        None,
        "hasil = 'promise_to_pay'",
        None,
    ),
]


def normalised(sql: str | None) -> str | None:
    return None if sql is None else condition(sql).sql(dialect="postgres")


def test_the_table_covers_the_16_simple_metrics() -> None:
    simple = {r.name for r in doc_metrics() if r.type == "simple"}
    assert len(MEASURE_TABLE) == 16
    assert {row[0] for row in MEASURE_TABLE} == simple


@pytest.mark.parametrize(
    ("metric_name", "measure_ref", "agg", "expr", "flt", "rule"), MEASURE_TABLE
)
def test_each_simple_metric_computes_what_its_row_says(
    metric_name: str,
    measure_ref: str,
    agg: str,
    expr: str | None,
    flt: str | None,
    rule: str | None,
) -> None:
    metric = approved_metrics()[metric_name]
    assert metric.measure == measure_ref
    entity_name, measure_name = measure_ref.split(".")
    measure = next(
        m for m in loaded().entities[entity_name].measures or [] if m.name == measure_name
    )
    assert measure.agg == agg
    assert normalised(measure.expr) == normalised(expr)
    assert normalised(measure.filter) == normalised(flt)
    assert measure.rule == rule
    # metric-level filters: only restructured_outstanding_principal has one, a rule reference
    if metric_name == "restructured_outstanding_principal":
        assert metric.filters == [RuleRef(rule="contract.restructured")]
    else:
        assert not metric.filters


# --------------------------------------------------------------------------------------
# Golden questions must not leak from the definitions (docs/EVALS.md section 2.3)


def _plain(text: str) -> str:
    return " ".join(text.lower().split())


def golden_questions() -> list[str]:
    out = []
    for line in (ROOT / "docs" / "EVALS.md").read_text(encoding="utf-8").splitlines():
        if re.match(r"\| G\d+ \|", line):
            out.append(table_cells(line)[1])
        elif m := re.match(r'\s*question: "(.+)"\s*(#.*)?$', line):
            out.append(m.group(1))
    return out


def definition_questions() -> list[str]:
    out: list[str] = []
    for base in (ROOT / "semantic" / "definitions", ROOT / "semantic" / "schema" / "examples"):
        for path in sorted(base.rglob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if data.get("kind") != "metric":
                continue
            out += data.get("example_questions", [])
            out += [e["question"] for e in data.get("example_requests", [])]
    return out


def test_golden_questions_are_parsed() -> None:
    questions = golden_questions()
    assert len(questions) >= 20
    assert "How much did we disburse in June 2026?" in questions
    assert len(definition_questions()) > 80


def test_no_golden_question_equals_an_example_question() -> None:
    examples = {_plain(q) for q in definition_questions()}
    clashes = [q for q in golden_questions() if _plain(q) in examples]
    assert clashes == []


# --------------------------------------------------------------------------------------
# The draft write-off metric selects exactly the write-off row (Q8)


def test_the_draft_write_off_metric_uses_the_write_off_rule() -> None:
    draft = loaded().draft_metrics["write_off_amount"]
    assert draft.measure == "contract_snapshot.written_off_outstanding_principal"
    assert not draft.filters
    assert draft.time_grains == ["month"]
    measure = next(
        m
        for m in loaded().entities["contract_snapshot"].measures or []
        if m.name == "written_off_outstanding_principal"
    )
    assert measure.rule == "write_off_position"
    assert measure.semi_additive == "last"


def test_the_write_off_rule_selects_exactly_one_row_per_written_off_contract(
    data_small: Path,
) -> None:
    rule = next(
        r
        for r in loaded().entities["contract_snapshot"].rules or []
        if r.name == "write_off_position"
    )
    # Q8: a contract more than 210 days past due at a month end is written off at that month end
    assert condition(rule.expr) == condition("days_past_due > 210")
    con = connect(data_small)
    on_rule = f"""SELECT count(*) FROM contract_monthly_snapshots WHERE {rule.expr}"""
    # the write-off row: the contract's status is written_off and the row is at its closing date
    write_off_rows = """SELECT count(*) FROM contract_monthly_snapshots s
        JOIN contracts c USING (contract_id)
        WHERE c.contract_status = 'written_off' AND s.snapshot_date = c.closed_date"""
    both = f"""SELECT count(*) FROM contract_monthly_snapshots s
        JOIN contracts c USING (contract_id)
        WHERE c.contract_status = 'written_off' AND s.snapshot_date = c.closed_date
        AND s.{rule.expr}"""
    written_off = "SELECT count(*) FROM contracts WHERE contract_status = 'written_off'"
    assert scalar(con, written_off) > 0
    assert scalar(con, on_rule) == scalar(con, write_off_rows) == scalar(con, both)
    assert scalar(con, write_off_rows) == scalar(con, written_off)
