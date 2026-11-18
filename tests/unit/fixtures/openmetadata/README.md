# OpenMetadata fixtures (CON-2)

Bodies of `GET /api/v1/tables` and `GET /api/v1/glossaryTerms` for the synthetic lending sample
catalog (`data/catalog/lending.catalog.yaml`). Nothing here is OpenMetadata's demo data or real data.

**These files were written from the documented shapes and were not recorded from a live
server.** No OpenMetadata server could be run where they were made (no Docker). Manual
criterion 4 of WP-21 (a real server, the sample catalog seeded, a sync) is where they meet one;
until the owner has done it, treat the shapes as unverified.

Release: OpenMetadata **2.0.7** (tag `2.0.7-release`). Sources, read at that tag from
`github.com/open-metadata/OpenMetadata`:

| Shape | Source |
|---|---|
| table, column, constraint and data type names, `tableConstraints`, `owners`, `tags` | `openmetadata-spec/.../json/schema/entity/data/table.json` |
| tag label (`tagFQN`, `source`, `labelType`, `state`) | `.../type/tagLabel.json` |
| owner and service references (`id`, `type`, `name`, `fullyQualifiedName`) | `.../type/entityReference.json`, `entityReferenceList.json` |
| glossary term, `synonyms`, `relatedTerms` | `.../entity/data/glossaryTerm.json`, `.../type/termRelation.json` |
| list answer `{data, paging}` and the `after` cursor | `.../type/paging.json`; query parameters from `TableResource.java` and `GlossaryTermResource.java` |
| ids, cursors, `updatedAt` | invented here (deterministic UUIDs, base64 of a small JSON object, one fixed date) |

Read in the server source at the tag (`TableRepository`, `FullyQualifiedName`, `DatabaseUtil`),
not on a live server:

- `service`, `database` and `databaseSchema` references are always set in a list answer
  (`setFields` runs `setDefaultFields`), so the four-part-name fallback is rarely used.
- `tableConstraints` is returned only when it is in `fields`; the connector asks for it and the
  fake server of the tests drops it otherwise.
- A name part is quoted in a full name only if it holds a `.` or a `"`; spaces are not quoted.
- Create requests refuse a VARCHAR column without `dataLength`, and more than one column with
  the `PRIMARY_KEY` constraint (a composite key goes in `tableConstraints`); the fixtures follow
  both, so `contract_monthly_snapshots` carries a table-level key.

Still unverified: that a real list answer has exactly these keys and values (for example the
`dataTypeDisplay` text for a varchar, which a real server writes as `varchar(255)`), what
`fields=owners` returns for a user (the reference has no e-mail), that a database service may be
created without `connection`, and the seeder's handling of `relatedTerms` and term tags (it does not
load them, so criterion 4's glossary comparison will differ on those two fields).

## Files

- `tables_page_*.json`, `glossary_terms_page_*.json`: generated from the export by
  `uv run python -m tests.openmetadata_fixtures` (tables four to a page, terms five to a page).
  A unit test fails if they differ from what the script writes.
- `hand_*.json`: written by hand, not generated. `hand_tables_owners_and_tags.json` (user and team
  owners, duplicate, suggested and glossary tags, a composite key, an unknown type),
  `hand_tables_unmapped_service.json`, `hand_tables_unmappable_names.json` (a space, a dot, a repeated column), `hand_glossary_term_with_synonyms.json`,
  `hand_error_401.json` and `hand_error_500.json` (bodies with a marker text that no error
  message may show). The timeout and the redirect are produced by the test's fake transport.

The contract test compares three mappings of one source: export to OpenMetadata JSON (the
generator), OpenMetadata JSON to models (the connector), export to models (the file connector).
`catalog_id` (an entity id here) and `updated_at` (the export has none) are left out of the
comparison.
