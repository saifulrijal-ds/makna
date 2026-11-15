Uvicorn at log level `trace` logs the whole ASGI scope of every request, headers included, so a bearer token would reach the log.

Checked on 2026-11-15 with uvicorn 0.54 (`uvicorn/middleware/message_logger.py`).

- At `info` and `debug`, uvicorn logs the request line (method, path, status) and no header; nothing of the SDK logs a header or a token either (a test collects every log line of a full flow at debug level, with a real server, and looks for the token).
- At `trace`, `MessageLoggerMiddleware` logs `Started scope=...` for each request, and the scope holds `Authorization`. `makna serve` starts uvicorn with its default level and has no option to change it; a deployment must not set `--log-level trace` (or `UVICORN_LOG_LEVEL=trace` for the `uvicorn` command) on a server that takes API tokens.
- `makna.mcp.http` also drops the `Authorization` header from the scope it passes to the SDK, so the SDK and the tools never hold it.
