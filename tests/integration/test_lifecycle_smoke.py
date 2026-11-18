from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import URL

from . import lifecycle_support as sup


def test_draft_to_approved(empty_database: URL, tmp_path: Path) -> None:
    world = sup.build_world(empty_database, tmp_path / "defs")
    curator, approver = world.caller("curator"), world.caller("approver")
    r1 = world.life.create_proposal(
        curator, "metric:contract_count_total", sup.metric_text(), "first draft"
    )
    print(r1)
    r2 = world.life.submit(curator, "metric:contract_count_total", "ready")
    r3 = world.life.approve(approver, "metric:contract_count_total", "looks right")
    print(r2, r3)
    print(sup.git(world.repo, "log", "--format=%an|%cn|%s%n%b"))
    for row in sup.audit_rows(world.engine):
        print(row["action"], row["object_id"], row["commit_sha"] and row["commit_sha"][:8])
