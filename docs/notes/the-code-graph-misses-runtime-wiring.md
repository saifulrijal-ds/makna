The code graph (graphify, D-36) sees what the parser sees: calls, imports and definitions. It misses wiring made at run time, so check those links by reading the code.

Measured on main afcc6c0 (7,940 nodes, 20,525 links, built in about 12 seconds):

- **MCP tools.** A tool is a `ToolSpec` row in `src/makna/mcp/server.py` that names its handler in a table. `graphify path "makna_search" "HybridSearch"` found no code path; its shortest path ran through the headings of `docs/ARCHITECTURE.md`. The real path is `_search` in `server.py`, then the service layer, then the search.
- **pytest fixtures.** A test receives a fixture by naming it as a parameter. `graphify explain "engine()"` (the fixture in `tests/integration/conftest.py`) lists the conftest and a few inferred calls, not the tests that take it.
- **Stale graph.** The graph is a snapshot of the last `uv run --group graph graphify update .`. After a large change, rebuild it before trusting what it says is missing.

Use the graph to narrow where to look (`graphify query`, `graphify path`, `graphify explain`, `graphify affected`), then read the lines it points to. Anything it says is absent, confirm with search.
