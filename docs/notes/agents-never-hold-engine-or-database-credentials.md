An agent process must hold only a Makna token: with a database URL or engine password it can bypass every rule Makna enforces.

The design originally passed engine credentials from the MCP client and gave the stdio MCP server the database URL (review R-2, R-3). Now `makna mcp` is a token-only bridge to `/mcp`, and a token that may run queries carries its owner's engine credentials stored encrypted on the server (`docs/ARCHITECTURE.md` section 12). Masking in generated SQL is not access control on the engine: a real deployment must still enable engine authentication.
