"""One definition object as the indexer sees it: the official file and the proposal, if any.

The definition store (`makna.definitions`) builds these from files; the indexer (`makna.index`)
reads them. Neither imports the other, so this shared vocabulary lives in the model package.
"""

from __future__ import annotations

from dataclasses import dataclass

from makna.model.definitions import AnyDefinition


@dataclass(frozen=True)
class DefinitionObject:
    """The files of one object (`metric:npf_ratio`): its official file and its proposal file."""

    official: AnyDefinition | None = None
    proposal: AnyDefinition | None = None
    official_path: str | None = None
    proposal_path: str | None = None

    def __post_init__(self) -> None:
        if self.official is None and self.proposal is None:
            raise ValueError("a definition object needs an official file or a proposal")

    @property
    def served(self) -> AnyDefinition:
        """The version search serves: the official file, else the proposal."""
        found = self.official if self.official is not None else self.proposal
        assert found is not None
        return found

    @property
    def kind(self) -> str:
        return self.served.kind

    @property
    def name(self) -> str:
        return self.served.name

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.name}"
