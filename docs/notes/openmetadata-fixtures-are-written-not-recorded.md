Fixtures of the OpenMetadata connector were written from the 2.0.7 JSON schemas, not recorded, so only a real server proves the shapes.

- The schemas and the resource classes are readable at `raw.githubusercontent.com/open-metadata/OpenMetadata/2.0.7-release/...` even without a server; the list answer is `{data, paging: {total, after, before}}` and `owners` (plural, a list of references) replaced `owner`.
- A create body for owners needs the entity `id`, so a seeder must `PUT` teams and users first and use the ids the server returns.
- Test the connector's mapping against a generator that shares no code with it, and keep error messages free of response bodies: a canary string in the fixture body catches a leak.
- Manual criterion 4 of WP-21 (real server, seeded, synced) is open until the owner runs it.
