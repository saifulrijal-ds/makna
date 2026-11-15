The SDK's `@tool` decorator ignores unknown fields and puts the rejected value into the error text, so Makna's tools are written out and checked by the services.

Checked on 2026-11-15 with `mcp` 2.3.0 and `MCPServer`.

- A tool's input model is built from its Python signature with pydantic's default `extra="ignore"`: an argument the tool does not have is dropped without a word. For a compile request that means a misspelt `time_end` gives SQL for the wrong period.
- An argument of the wrong type comes back as `Error executing tool <name>: 1 validation error ... input_value='<what was sent>'`, so whatever the agent typed is in the answer (and in `isError` text a client may log).
- An exception raised in the tool is safe in the answer (`Error executing tool <name>`), but the SDK logs it with `logger.exception`, and the traceback holds the exception text. A database error's text carries the SQL parameters (see `database-error-text-carries-the-query-parameters.md`).
- A tool that raises `ToolError` gets `Error executing tool <name>: ` put before its message, so the text is no longer the JSON error of section 12.
- So `makna.mcp.server.MaknaServer` overrides `list_tools` and `call_tool` (both public), advertises written-out schemas with `additionalProperties: false`, refuses unknown fields by name only, hands the arguments to the services (which check every value and never repeat one), and catches every other exception before the SDK sees it.
