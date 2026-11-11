The official Python MCP SDK version 2 renamed `FastMCP` to `MCPServer`; version 1 imports fail.

Checked on 2026-11-11 with `mcp` 2.3.0.

- `from mcp.server.fastmcp import FastMCP` raises `ModuleNotFoundError` with a message pointing to the migration guide.
- Use `from mcp.server.mcpserver import MCPServer`.
- Other APIs changed too. Read the migration guide (`https://py.sdk.modelcontextprotocol.io/v2/migration/`) before writing server or client code, and do not copy version 1 examples from memory or from older blog posts.
- Do not "fix" an import error by pinning `mcp<2`. The design uses version 2 (DR-7 in `docs/DECISIONS.md`).
- The separate PyPI package `fastmcp` (4.x, Apache-2.0) is a different project. Makna uses the official `mcp` package (P-5).
