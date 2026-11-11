Quote any description that holds a comma inside a `{...}` flow mapping, or a colon followed by a space in a plain value; the file then fails V01.

Seen while writing the WP-04 sample definitions (2026-11-11).

- In `values: [{value: x, description: Doctor, lawyer}]` the comma ends the key, so `lawyer` becomes a key with no value, and the loader reports "null is not allowed". Write `description: "Doctor, lawyer"`.
- In `caveats: Counts activities, not contracts: one late contract...` the second colon makes YAML read a nested mapping ("mapping values are not allowed here"). Quote the value, or use the `>-` block style.
- `makna validate` shows both as V01 with the line number, so run it after every few files.
