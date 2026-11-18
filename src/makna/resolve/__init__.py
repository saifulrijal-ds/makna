"""Resolver: turns a question in words into a request, by rules and without a language model
(ARCHITECTURE section 8.4). `today` is always a parameter: nothing here reads the clock."""

from makna.resolve.grammar import GrainMatch, TimeMatch, find_grain, find_times, parse_time
from makna.resolve.resolver import (
    Candidate,
    Interpretation,
    Resolution,
    ResolvedHit,
    resolve,
)

__all__ = [
    "Candidate",
    "GrainMatch",
    "Interpretation",
    "Resolution",
    "ResolvedHit",
    "TimeMatch",
    "find_grain",
    "find_times",
    "parse_time",
    "resolve",
]
