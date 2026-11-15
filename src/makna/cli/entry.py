"""The `makna` console script: a launcher that loads as little as the command needs.

`makna mcp` is the stdio bridge for an agent's machine and must hold no service or database
code (ARCHITECTURE section 12), but `makna.cli.main` imports all of it at its top, for the
commands that need it. So this module looks at the command first: for `makna mcp` alone it
imports and runs the bridge, and for anything else (`makna mcp --help` too, which the
command-line parser prints) it imports `makna.cli.main` and runs that.
"""

from __future__ import annotations

import sys


def main() -> int:
    if sys.argv[1:] == ["mcp"]:
        from makna.mcp.bridge import main as bridge_main

        return bridge_main()
    from makna.cli.main import main as cli_main

    return cli_main()
