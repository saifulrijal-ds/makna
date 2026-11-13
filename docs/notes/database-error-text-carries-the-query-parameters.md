A database error's text holds the SQL parameters, so a service must log only the exception class and never put the text in a response or a log line.

Checked on 2026-11-13 (PostgreSQL 16.2, SQLAlchemy 2.0, psycopg 3): a failed `CAST(:v AS integer)` raised `DataError` whose `str()` contains `invalid input syntax for type integer: "<the value>"` and `[parameters: {'v': '<the value>'}]`. In the service layer the parameters are the caller's own words: a search query, an id, a filter value.

- `makna.services.service.Services._logged` turns any other exception into `ServiceError(INTERNAL)` with fixed text and writes `type(error).__name__` to the application log.
- A failed `tool_call_log` write is logged the same way (class only). The row's parameters are the redacted request, but the redaction cannot be trusted to have run when the write itself failed.
- This is the same rule as `engine-error-text-can-carry-a-personal-value.md`, for Makna's own database.
