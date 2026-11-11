Parse a definition's SQL fragment with `sqlglot.parse_one(sql, into=exp.Condition)`; plain `parse_one` accepts statements, aliases, and `a; b`.

Checked on 2026-11-11 with sqlglot 30.31.0.

- `parse_one("a b")` silently reads an alias, and `parse_one("SELECT 1")` returns a statement. With `into=exp.Condition` both raise `ParseError`; `a; b` gives an `exp.Block`, which the validator rejects.
- In this version the base class of expression nodes is `exp.Expr`, not `exp.Expression` (type hints must say `exp.Expr`).
- Print with `unsupported_level=ErrorLevel.RAISE` to learn that a construct cannot be written for a dialect; the default only logs a warning.
