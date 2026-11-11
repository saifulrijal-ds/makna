"""A problem found in a definition file."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Issue:
    """One validation problem. `code` is V01 to V19 (ARCHITECTURE section 5.3).

    `file` is the path relative to the definitions directory. `line` is 1-based and is None
    when the problem has no single place in the file.
    """

    file: str
    line: int | None
    code: str
    message: str

    def format(self, prefix: str = "") -> str:
        place = f"{prefix}{self.file}"
        if self.line is not None:
            place += f":{self.line}"
        return f"{place}: {self.code} {self.message}"
