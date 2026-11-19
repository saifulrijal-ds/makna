"""The words an agent reads: the server instructions and the four tool descriptions.

They are the text of `docs/ARCHITECTURE.md` section 12, word for word. They live in this one
module so a change is easy to find and impossible to make by accident: a snapshot test
(`tests/integration/mcp_tools_snapshot.txt`) compares them with a reviewed text file, so any
change of wording is a visible, reviewed change (MCP-2).
"""

from __future__ import annotations

INSTRUCTIONS = (
    "Makna holds the approved business definitions for this organization's data. To answer a "
    "question about a business number: 1) call makna_search with the user's words; 2) call "
    "makna_get_definition for the best candidate and check that its description really matches "
    "what the user asked; 3) call makna_compile_query with a structured request (the "
    "definition's example requests show the format), then run the SQL it returns, unchanged, "
    "with your own database tool. Never write SQL yourself for a number that Makna defines. If "
    "search confidence is low, read the descriptions of the hits before deciding. If no "
    "description matches what the user asked, tell the user that no approved definition "
    "exists; do not substitute a different metric. If search reports several candidates, ask "
    "the user which one they mean."
)

SEARCH_DESCRIPTION = (
    "Find approved metrics, business terms, entities, and dimensions by meaning. Use this first "
    "for any question about a business number or term, in English or Indonesian. Returns up to "
    "`limit` ranked hits and a `confidence` of high, medium, or low. If confidence is low, read "
    "the descriptions of the hits before deciding. If no hit's description matches the "
    "question, no approved definition exists: say so, and do not use a different metric "
    "instead. If `ambiguous_between` is present, ask the user to choose."
)

GET_DEFINITION_DESCRIPTION = (
    "Get the full approved definition of one object by id (from makna_search). For a metric this "
    "returns its meaning, formula in words, official date, caveats, the dimensions you may group "
    "or filter by, their allowed values, and the allowed time grains. Read this before compiling "
    "a query, and use only the dimensions it lists. If the id is unknown, the error lists close "
    "ids; if none fits, search again with different words."
)

LIST_RELATED_DESCRIPTION = (
    "List objects related to one object: the inputs of a metric, metrics built from it, metrics "
    "on the same entity, the rules it uses (or, for a rule, the metrics that use it), the "
    "business terms that explain it, and its replacement if deprecated. Use it to find a nearby "
    "metric when the first one is not quite what the user asked for. Returns up to 20 related "
    "objects with one-line descriptions. An empty list means nothing is related: go back to "
    "makna_search."
)

COMPILE_QUERY_DESCRIPTION = (
    "Turn a structured request into SQL for the query engine, using only approved joins and "
    "definitions. Use the metric name and the dimension refs exactly as given by "
    "makna_get_definition. Dates are ISO dates; time_end is exclusive; resolve words like "
    '"last month" to dates before calling. Returns the SQL, the result columns, and an '
    "explanation of how the number is built that you can show to the user. On an error, read "
    "the code and the details (they list what is allowed), fix the request, and call again."
)
