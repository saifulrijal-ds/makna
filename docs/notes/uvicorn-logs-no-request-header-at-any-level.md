Uvicorn 0.54 logs no request header at any level, trace included, so a bearer token does not reach its log.

Checked on 2026-11-15 with uvicorn 0.54 (`uvicorn/middleware/message_logger.py`) and by the safety review of WP-13.

- At `info` and `debug`, uvicorn logs the request line (method, path, status) and no header. At `trace`, `MessageLoggerMiddleware` logs `Started scope=...` for each request, but `message_with_placeholders` replaces `headers`, `body`, `bytes`, and `text` with placeholders, so the token is not in it either. A test collects every line of a full flow at debug level, with a real server, and looks for the token.
- The request line does hold the query string, so a client that sends its token as `?api_key=` leaves it in the access log (and is refused with a 401). Tokens go only in the `Authorization` header.
- Other versions of uvicorn may differ; as a precaution, do not run a server that takes API tokens at `trace` level after an upgrade without looking at its output once.
- `makna.mcp.http` also drops the `Authorization` header from the scope it passes to the SDK, so the SDK and the tools never hold it.
