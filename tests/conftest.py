"""Mark each test with its tier, taken from the directory it lives in."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

TIERS = ("unit", "integration", "engine")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.path)).parts
        for tier in TIERS:
            if "tests" in parts and tier in parts[parts.index("tests") + 1 :]:
                item.add_marker(getattr(pytest.mark, tier))


@pytest.fixture(autouse=True, scope="session")
def _never_download_a_model() -> Iterator[None]:
    """No test downloads an embedding model (WP-07 acceptance 5): fastembed goes offline."""
    import os

    before = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    yield
    if before is None:
        os.environ.pop("HF_HUB_OFFLINE", None)
    else:
        os.environ["HF_HUB_OFFLINE"] = before
