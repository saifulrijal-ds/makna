PostgreSQL resolves `t.name` to the function call `name(t)` when `t` has no column `name`, so a column name can return a whole row; only the catalog reference check (R02) protects against it.

Checked by the safety review of WP-10 (2026-11-12, PostgreSQL 16.2): `"t0"."to_jsonb"` on the customers table returned every column, including the personal ones, as JSON. The same holds for `to_json`, `row_to_json`, `concat`, `record_out`, `quote_literal`, and others.

- `governance.verify` compares column names with the personal columns of the model, so it does not see this: the reference is to a column named `to_jsonb`, which is not personal.
- What stops it is R02: a column used in a dimension that is not in the synced catalog table makes the compiler refuse the request (`BROKEN_REFERENCE`) on any entity the plan touches. A deployment with no synced catalog does not have that protection.
- No deny-list of function names is kept; the catalog is the single source for which columns exist.
