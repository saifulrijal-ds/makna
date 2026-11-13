"""WP-12 criterion 7 (a change is seen without a restart) and the rebuild rules of the snapshot
cache: refresh on `definitions_version`, on a catalog sync and on a change of the open reference
issues; one rebuild under concurrent requests, with the old snapshot serving meanwhile."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

import makna.services.snapshot as snapshot_module
from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer
from makna.model.request import QueryError
from makna.search.embedding import HashProvider
from makna.services.snapshot import SnapshotCache

from ..catalog_support import DEFINITIONS, TAG_MAP, export_with_tags, sample_objects
from . import service_support as sup
from .service_support import Users

METRIC = "metric:disbursement_amount"


def edit(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert old in content
    path.write_text(content.replace(old, new, 1), encoding="utf-8")


def index_one(engine: Engine, directory: Path, object_id: str) -> indexer.IndexResult:
    objects = definition_objects(load_definitions(directory))
    target = next(o for o in objects if o.id == object_id)
    return indexer.index_object(engine, HashProvider(), target, objects)


def test_after_index_object_the_next_get_definition_returns_the_new_content(
    fresh_service_world: tuple[Engine, Users], tmp_path: Path
) -> None:
    engine, users = fresh_service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    before = svc.get_definition(caller, METRIC)
    assert before.time is not None and "quarter" in before.time.grains
    assert svc.compile_query(caller, {"metric": "disbursement_amount", "grain": "quarter"})
    cached = svc.snapshots.peek()
    assert cached is not None

    directory = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, directory)
    path = directory / "lending" / "metrics" / "disbursement_amount.yaml"
    edit(path, "default_grain: month", "time_grains: [month, year]\ndefault_grain: month")
    edit(path, "Uses the disbursement date", "Uses the NEW disbursement date")
    result = index_one(engine, directory, METRIC)
    assert result.objects_changed == 1 and result.documents_changed == 1

    after = svc.get_definition(caller, METRIC)  # same service object, no restart
    assert after.caveats is not None and "NEW disbursement date" in after.caveats
    assert after.time is not None and after.time.grains == ["month", "year"]
    now = svc.snapshots.peek()
    assert now is not None and now.key != cached.key
    assert now.model.metrics["disbursement_amount"].time_grains == ["month", "year"]
    with pytest.raises(QueryError) as raised:  # compile plans against the new snapshot too
        svc.compile_query(caller, {"metric": "disbursement_amount", "grain": "quarter"})
    assert raised.value.code == "INVALID_GRAIN"
    assert svc.compile_query(caller, {"metric": "disbursement_amount", "grain": "year"})


def test_a_changed_entity_is_seen_by_the_next_call(
    fresh_service_world: tuple[Engine, Users], tmp_path: Path
) -> None:
    engine, users = fresh_service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    old = svc.get_definition(caller, "dimension:branch.region_name")
    directory = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, directory)
    edit(
        directory / "lending" / "entities" / "branch.yaml",
        "Sales region the branch belongs to.",
        "Sales region (renamed) the branch belongs to.",
    )
    index_one(engine, directory, "entity:branch")
    new = svc.get_definition(caller, "dimension:branch.region_name")
    assert "renamed" in new.description and "renamed" not in old.description
    # the metric's own dimension list is computed again, since the stored one is out of date
    metric = svc.get_definition(caller, "metric:npf_ratio")
    region = next(d for d in metric.dimensions if d.ref == "branch.region_name")
    assert region.description is not None and "renamed" in region.description


def test_index_object_moves_definitions_version_only_when_something_changed(
    fresh_service_world: tuple[Engine, Users], tmp_path: Path
) -> None:
    engine, _ = fresh_service_world

    def version() -> str:
        with engine.connect() as connection:
            return str(
                connection.execute(
                    text("SELECT value FROM index_state WHERE key = 'definitions_version'")
                ).scalar_one()
            )

    start = version()
    directory = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, directory)
    assert index_one(engine, directory, METRIC).total_changes == 0
    assert version() == start
    path = directory / "lending" / "metrics" / "disbursement_amount.yaml"
    edit(path, "booking amount", "booking sum")
    assert index_one(engine, directory, METRIC).total_changes > 0
    second = version()
    assert second != start
    edit(path, "booking sum", "booking total")
    index_one(engine, directory, METRIC)
    assert version() not in (start, second)
    edit(path, "booking total", "booking amount")
    index_one(engine, directory, METRIC)
    assert version() not in (start, second)  # going back is a new version too, never a repeat


def test_a_catalog_sync_refreshes_the_snapshot(
    fresh_service_world: tuple[Engine, Users], tmp_path: Path
) -> None:
    engine, users = fresh_service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    request = {"metric": "new_customer_count", "dimensions": ["customer.city"]}
    assert svc.compile_query(caller, request)
    first = svc.snapshots.peek()
    assert (
        first is not None
        and first.model.derived_sensitivity.get(("lending.core.customers", "city")) is None
    )
    tagged = export_with_tags(tmp_path / "t.yaml", {("customers", "city"): ["PII.Sensitive"]})
    entities, terms = sample_objects()
    sync_catalog(
        engine, FileConnector(tagged), tag_map=TAG_MAP, entities=entities, glossary_terms=terms
    )
    with pytest.raises(QueryError) as raised:  # no index build, no restart: the next call knows
        svc.compile_query(caller, request)
    assert raised.value.code == "BROKEN_REFERENCE"  # R03: the definition declares city internal
    second = svc.snapshots.peek()
    assert second is not None and second.key != first.key
    assert second.model.derived_sensitivity[("lending.core.customers", "city")] == "personal"


def test_the_snapshot_holds_only_approved_and_deprecated_official_content(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    svc.compile_query(users.caller("curator"), {"metric": "npf_ratio"})
    snapshot = svc.snapshots.peek()
    assert snapshot is not None
    assert "write_off_amount" not in snapshot.model.metrics  # a draft, for every role
    assert "npl_ratio" in snapshot.model.metrics  # deprecated: the compiler says so
    assert {"contract", "customer", "branch"} <= set(snapshot.model.entities)
    assert (
        snapshot.model.derived_sensitivity[("lending.core.customers", "national_id")] == "personal"
    )
    assert all(level for level in snapshot.model.derived_sensitivity.values())


def test_the_derived_map_is_keyed_by_lower_case_fqn_and_column_and_holds_only_levels(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    with engine.connect() as connection:
        found = indexer.catalog_sensitivity(connection)
    assert found[("lending.core.customers", "full_name")] == "personal"
    assert all(key == (key[0].lower(), key[1].lower()) for key in found)
    assert ("lending.core.branches", "region_name") not in found


# --- the cache itself ---


def test_a_second_call_with_the_same_key_reuses_the_snapshot(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = service_world
    cache = SnapshotCache(engine)
    built: list[int] = []
    real = snapshot_module._build

    def counting(connection, key):  # type: ignore[no-untyped-def]
        built.append(1)
        return real(connection, key)

    monkeypatch.setattr(snapshot_module, "_build", counting)
    first, _ = cache.current()
    second, _ = cache.current()
    assert first is second and len(built) == 1


def test_concurrent_requests_rebuild_once_and_the_old_snapshot_serves_meanwhile(
    fresh_service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine, _ = fresh_service_world
    cache = SnapshotCache(engine)
    old, _ = cache.current()
    # change the index so that the key moves
    directory = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, directory)
    edit(
        directory / "lending" / "metrics" / "disbursement_amount.yaml",
        "booking amount",
        "booking sum",
    )
    index_one(engine, directory, METRIC)

    entered, release = threading.Event(), threading.Event()
    builds: list[int] = []
    real = snapshot_module._build

    def slow(connection, key):  # type: ignore[no-untyped-def]
        builds.append(1)
        entered.set()
        assert release.wait(10)
        return real(connection, key)

    monkeypatch.setattr(snapshot_module, "_build", slow)
    results: dict[str, object] = {}

    def rebuild() -> None:
        results["rebuilt"], _ = cache.current()

    thread = threading.Thread(target=rebuild)
    thread.start()
    assert entered.wait(10)
    # while the first thread rebuilds, other callers get the old snapshot at once
    for _ in range(5):
        served, _ = cache.current()
        assert served is old
    assert len(builds) == 1
    release.set()
    thread.join(10)
    fresh, _ = cache.current()
    assert results["rebuilt"] is fresh and fresh is not old and len(builds) == 1


def test_the_first_build_waits_for_the_one_in_progress(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = service_world
    cache = SnapshotCache(engine)
    entered, release = threading.Event(), threading.Event()
    builds: list[int] = []
    real = snapshot_module._build

    def slow(connection, key):  # type: ignore[no-untyped-def]
        builds.append(1)
        entered.set()
        assert release.wait(10)
        return real(connection, key)

    monkeypatch.setattr(snapshot_module, "_build", slow)
    got: list[object] = []
    threads = [threading.Thread(target=lambda: got.append(cache.current()[0])) for _ in range(4)]
    threads[0].start()
    assert entered.wait(10)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(10)
    assert len(builds) == 1 and len(got) == 4 and all(item is got[0] for item in got)
