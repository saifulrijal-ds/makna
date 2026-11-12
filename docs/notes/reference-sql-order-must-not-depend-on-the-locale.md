Reference SQL for the golden set sorts text with `COLLATE "C"` and casts `date_trunc` back to `DATE`, so its CSV is the same on any PostgreSQL.

Seen while writing WP-14 (2026-11-12). The expected CSVs are compared as exact text, row by row.

- A plain `ORDER BY region_name` follows the database locale. The local test server uses `C`; the pgvector image of the dev stack uses its own default. Values such as `dpd_1_30` and `dpd_31_60` or `car_backed` can sort differently under another locale, which would fail the engine test for no data reason. `ORDER BY x COLLATE "C"` fixes the order.
- In `ORDER BY`, an output alias cannot be used inside an expression (`ORDER BY alias COLLATE "C"` fails); order by the input column, or select from a CTE where the name is a real column.
- `date_trunc('month', some_date)` returns `timestamptz`, whose text depends on the session time zone. Write `CAST(date_trunc('month', d) AS DATE)` for `metric_time`.
- Name tables `core.<table>`, not `lending.core.<table>`: PostgreSQL accepts the three-part name only when the database is called `lending`, and a local copy of the data may have another name.
