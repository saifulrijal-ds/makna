"""The catalog export of the sample domain (WP-06), generated from the table specification.

Run `uv run python -m data.catalog` to rewrite `data/catalog/lending.catalog.yaml`; add
`--check` to compare instead of writing.
"""

from .build import EXPORT_PATH, build_export, render_export

__all__ = ["EXPORT_PATH", "build_export", "render_export"]
