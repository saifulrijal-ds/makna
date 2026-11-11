Do not add the OpenMetadata Python SDK as a dependency: it is not under a permissive licence. Call the REST API with httpx.

Checked on 2026-11-11.

- The OpenMetadata server repository is Apache-2.0.
- The Python package `openmetadata-ingestion` (2.0.7.2 on PyPI) declares the "Collate Community License Agreement". `CLAUDE.md` requires the project owner's approval before adding a dependency with a non-permissive licence.
- The OpenMetadata connector (WP-21) therefore uses plain HTTP calls to `/api/v1/tables`, `/api/v1/glossaryTerms`, and related endpoints, with a bearer token from the environment.
- Before adding any dependency, read its licence on the package registry, not from memory. Licences change: MetricFlow moved to Apache-2.0 in late 2025, and `pygit2` is GPL-2.0 with a linking exception.
