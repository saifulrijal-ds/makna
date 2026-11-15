The SDK's Streamable HTTP app, mounted under `/mcp`, answers `/mcp` with a redirect and refuses any `Host` that is not localhost, so Makna routes the endpoint itself.

Checked on 2026-11-15 with `mcp` 2.3.0, Starlette, and FastAPI.

- `Mount("/mcp", app=mcp.streamable_http_app())` serves `/mcp/`; `/mcp` gets a 307 to `/mcp/`, which a client may not follow for a `POST`. `streamable_http_app()` also builds the session manager (`mcp.session_manager`). `makna.mcp.http` uses that manager with `StreamableHTTPASGIApp` as the endpoint of two plain routes (`/mcp` and `/mcp/`) and enters `session_manager.run()` in the application's lifespan. The manager runs once per instance, so every application builds its own server.
- The app turns DNS rebinding protection on for the default host `127.0.0.1` with the patterns `127.0.0.1:*`, `localhost:*`, and `[::1]:*`. Any other `Host` (and a bare `localhost` with no port, which some in-process test clients send) is `421`; an `Origin` outside the list is `403`; no `Origin` passes. Pass `TransportSecuritySettings` to name more hosts; Makna reads them from `[mcp] allowed_hosts`.
- The SDK's own auth (`auth=`, `token_verifier=`) wants an issuer URL and adds discovery and registration endpoints. Makna's tokens are opaque and checked in the database, so a small ASGI middleware does the check instead.
- The 2026 protocol era (`MCP-Protocol-Version: 2026-07-28`, sent by the SDK's own client) answers `GET` and `DELETE` with `405`; the 2025 era (stateless) opens a standalone event stream on `GET` that stays open until the client leaves.
