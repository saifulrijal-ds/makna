"""Mark each test with its tier, taken from the directory it lives in."""

from __future__ import annotations

from pathlib import Path

import pytest

TIERS = ("unit", "integration", "engine")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.path)).parts
        for tier in TIERS:
            if "tests" in parts and tier in parts[parts.index("tests") + 1 :]:
                item.add_marker(getattr(pytest.mark, tier))
