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


@pytest.fixture(autouse=True, scope="session")
def _never_download_a_model() -> None:
    """No test downloads an embedding model (WP-07 acceptance 5): fastembed goes offline."""
    import os

    os.environ["HF_HUB_OFFLINE"] = "1"
