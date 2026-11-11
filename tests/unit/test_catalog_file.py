"""WP-06: the `makna-catalog/v1` file connector, the sample export, and sensitivity mapping."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from data.catalog import render_export
from data.generator import TABLES, get_table
from makna.catalog.base import ConnectorError
from makna.catalog.file import FileConnector, parse_catalog
from makna.model.sensitivity import derive_from_tags, stricter

from ..catalog_support import EXPORT, TAG_MAP, export_data
from .generator_support import doc_catalog_exceptions, doc_columns


def _connector() -> FileConnector:
    return FileConnector(EXPORT)


# --- Acceptance 1 (file side): 9 datasets and the column count of section 3 --------------------


def test_the_sample_export_has_nine_datasets_and_the_columns_of_section_3() -> None:
    datasets = list(_connector().list_datasets())
    assert len(datasets) == 9
    expected = doc_columns()
    assert {d.name: [c.name for c in d.columns] for d in datasets} == {
        table: [name for name, _ in columns] for table, columns in expected.items()
    }
    assert sum(len(d.columns) for d in datasets) == sum(len(c) for c in expected.values())
    assert [c.ordinal for c in datasets[0].columns] == list(range(len(datasets[0].columns)))


def test_the_export_names_the_engine_tables_and_keys() -> None:
    for dataset in _connector().list_datasets():
        assert dataset.engine_fqn == f"lending.core.{dataset.name}"
        keys = {c.name for c in dataset.columns if c.is_primary_key}
        assert keys == set(get_table(dataset.name).primary_key)
        assert [o.name for o in dataset.owners] == ["data-governance"]


def test_the_five_personal_columns_carry_the_sensitive_tag_and_nothing_else_does() -> None:
    tagged = {
        (d.name, c.name): c.tags for d in _connector().list_datasets() for c in d.columns if c.tags
    }
    assert tagged == {
        ("customers", name): ["PII.Sensitive"] for name in get_table("customers").personal_columns
    }
    assert len(tagged) == 5


def test_the_export_has_eight_glossary_terms_with_synonyms() -> None:
    terms = list(_connector().list_glossary_terms())
    assert len(terms) == 8
    assert all(t.definition and t.synonyms for t in terms)
    assert len({t.catalog_id for t in terms}) == 8


# --- Acceptance 7 (Q11): section 6.1 exactly, and byte-identical regeneration ------------------


def test_the_committed_export_carries_the_section_6_1_descriptions_exactly() -> None:
    exceptions = doc_catalog_exceptions()
    by_name = {d.name: d for d in _connector().list_datasets()}
    for (table, column), expected in exceptions.items():
        dataset = by_name[table]
        actual = (
            dataset.description
            if column is None
            else next(c for c in dataset.columns if c.name == column).description
        )
        assert actual == expected, (table, column)
    # Every other object has its English description from the specification, never empty.
    for spec in TABLES:
        dataset = by_name[spec.name]
        if (spec.name, None) not in exceptions:
            assert dataset.description == spec.catalog_description != ""
        for entry in dataset.columns:
            if (spec.name, entry.name) not in exceptions:
                assert entry.description == spec.column(entry.name).catalog_description != ""


def test_regenerating_the_export_gives_a_byte_identical_file() -> None:
    assert render_export().encode("utf-8") == EXPORT.read_bytes()
    assert render_export() == render_export()


# --- Acceptance 5: a file that does not match the format is rejected, naming the field ----------


def _text(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False)


def _mutated(change: Any) -> str:
    data = copy.deepcopy(export_data())
    change(data)
    return _text(data)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda d: d.update(format="makna-catalog/v2"), "format"),
        (lambda d: d.pop("format"), "format"),
        (lambda d: d.pop("source"), "source"),
        (lambda d: d["source"].pop("name"), "source.name"),
        (lambda d: d["datasets"][2].pop("name"), "datasets[2].name"),
        (lambda d: d["datasets"][2].update(engine_fqn="customers"), "datasets[2].engine_fqn"),
        (lambda d: d["datasets"][1].update(surprise=1), "datasets[1].surprise"),
        (
            lambda d: d["datasets"][3]["columns"][4].update(data_type="varchar"),
            "datasets[3].columns[4].data_type",
        ),
        (
            lambda d: d["datasets"][3]["columns"][4].update(nullable="yes"),
            "datasets[3].columns[4].nullable",
        ),
        (
            lambda d: d["datasets"][0]["columns"][0].pop("name"),
            "datasets[0].columns[0].name",
        ),
        (
            lambda d: d["datasets"][0]["owners"].append({"name": "x", "type": "robot"}),
            "datasets[0].owners[1].type",
        ),
        (lambda d: d["glossary_terms"][5].pop("catalog_id"), "glossary_terms[5].catalog_id"),
        (lambda d: d.update(datasets="none"), "datasets"),
    ],
)
def test_a_file_that_does_not_match_the_format_is_rejected_naming_the_field(
    change: Any, field: str
) -> None:
    with pytest.raises(ConnectorError) as error:
        parse_catalog(_mutated(change), "export.yaml")
    assert field in str(error.value)
    assert str(error.value).startswith("export.yaml: ")


def test_duplicates_are_rejected_naming_the_field() -> None:
    def duplicate_dataset(d: dict[str, Any]) -> None:
        d["datasets"].append(copy.deepcopy(d["datasets"][0]))

    with pytest.raises(ConnectorError, match=r"datasets\[9\]\.engine_fqn"):
        parse_catalog(_mutated(duplicate_dataset))

    def duplicate_column(d: dict[str, Any]) -> None:
        d["datasets"][0]["columns"].append(copy.deepcopy(d["datasets"][0]["columns"][0]))

    with pytest.raises(ConnectorError, match=r"datasets\[0\]\.columns"):
        parse_catalog(_mutated(duplicate_column))


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "format: [unclosed", "just text"])
def test_unreadable_or_wrongly_shaped_files_are_rejected(text: str) -> None:
    with pytest.raises(ConnectorError):
        parse_catalog(text, "export.yaml")


def test_a_missing_file_is_a_connector_error(tmp_path: Path) -> None:
    with pytest.raises(ConnectorError, match="cannot read file"):
        FileConnector(tmp_path / "nope.yaml").check()


def test_json_is_accepted_and_the_ordinal_defaults_to_the_list_position() -> None:
    data = {
        "format": "makna-catalog/v1",
        "source": {"name": "tiny", "exported_at": "2026-06-30T00:00:00Z"},
        "datasets": [
            {
                "engine_fqn": "a.b.c",
                "name": "c",
                "columns": [
                    {"name": "x", "data_type": "string"},
                    {"name": "y", "data_type": "integer", "ordinal": 7},
                ],
            }
        ],
    }
    parsed = parse_catalog(json.dumps(data))
    dataset = parsed.datasets[0]
    assert dataset.description == "" and dataset.columns[0].nullable is True
    assert dataset.columns[0].ordinal is None  # the connector fills it in, the model does not


def test_the_connector_fills_the_ordinal(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {
                "format": "makna-catalog/v1",
                "source": {"name": "tiny"},
                "datasets": [
                    {
                        "engine_fqn": "a.b.c",
                        "name": "c",
                        "columns": [
                            {"name": "x", "data_type": "string"},
                            {"name": "y", "data_type": "integer", "ordinal": 7},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    connector = FileConnector(path)
    assert connector.name == "tiny" and connector.kind == "file"
    assert [c.ordinal for c in next(connector.list_datasets()).columns] == [0, 7]


# --- Acceptance 4 (mapping part): tags map to a derived sensitivity ---------------------------


def test_tag_mapping_takes_the_strictest_mapped_level() -> None:
    assert derive_from_tags(["PII.Sensitive"], TAG_MAP) == "personal"
    assert derive_from_tags(["Tier.Gold", "PII.NonSensitive"], TAG_MAP) == "confidential"
    assert derive_from_tags(["PII.NonSensitive", "PersonalData.Personal"], TAG_MAP) == "personal"
    assert derive_from_tags(["Tier.Gold"], TAG_MAP) is None
    assert derive_from_tags([], TAG_MAP) is None
    assert stricter("internal", "confidential") == "confidential"
    with pytest.raises(ValueError):
        stricter("internal", "secret")
