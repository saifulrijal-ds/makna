"""From a question to a request, by rules (ARCHITECTURE section 8.4, WEB-4).

`resolve` takes the question, the date it counts as today, the result of a search (approved
metrics and glossary terms), and the approved model snapshot. It never calls a language model and
never reads the clock or the database: the same inputs give the same `Resolution`.

Order of the rules, as section 8.4 numbers them:

1. The candidates are the metric hits. A glossary term that covers the question's words better
   than every metric hit does adds its related metrics.
2. `no_match` when the band of the search is `low`, or there is no candidate.
3. `ambiguous` when the second candidate is close to the first (`is_ambiguous`: the same rule the
   search service uses for `ambiguous_between`). Nothing is chosen and no request is built.
4. Otherwise the first candidate is the metric; the time, the grain, the dimensions, and the
   filters come from the words of the question, in this order, each phrase using its words once.
5. Words that nothing used, that are not stop words, and that the metric's names did not match are
   `unresolved_terms`.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from makna.compiler.plan import available_dimensions
from makna.compiler.snapshot import Model
from makna.model.definitions import Dimension, Metric
from makna.model.governance import GovernancePolicy
from makna.model.request import QueryError, QueryRequest, RequestFilter
from makna.resolve.grammar import find_grain, find_times
from makna.search.confidence import is_ambiguous
from makna.search.hybrid import Hit, SearchResult
from makna.search.query import STOP_WORDS, tokens
from makna.search.terms import GROUP_CUES, TREND_WORDS

Status = Literal["resolved", "ambiguous", "no_match"]
MAX_DESCRIPTION = 200
MIN_VALUE_LENGTH = 3
"""A declared value shorter than this (a grade `A`, a code `1`) is never matched in a question."""
MAX_LEAD_WORDS = 3
"""The first sentence of a value's description is read as a second name for it when it is this
short ("Syariah. Contract under sharia principles." names the value `sharia`)."""
MAX_PHRASE_WORDS = 6


@dataclass(frozen=True)
class ResolvedHit:
    """One search hit as the resolver saw it."""

    id: str
    kind: str
    label: str
    score: float
    coverage: float
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    """A metric the question may mean."""

    id: str
    label: str
    description: str
    """One line: what the metric means."""
    score: float
    coverage: float
    matched_terms: tuple[str, ...]
    pre_fusion_score: float = 0.0
    """The score the ambiguity rule compares (`Hit.pre_fusion_score`)."""
    via: str | None = None
    """The glossary term that brought the metric in, if it was not a metric hit itself."""

    @property
    def name(self) -> str:
        return self.id.partition(":")[2]


@dataclass(frozen=True)
class Interpretation:
    """What one phrase of the question was read as."""

    kind: Literal["time", "grain", "dimension", "filter"]
    words: str
    result: str


@dataclass(frozen=True)
class Resolution:
    status: Status
    question: str
    today: datetime.date
    confidence: str
    """The band of the search: `high`, `medium`, or `low`."""
    hits: tuple[ResolvedHit, ...]
    candidates: tuple[Candidate, ...]
    ambiguous_between: tuple[str, ...]
    """The ids of the first two candidates when `status` is `ambiguous`."""
    request: QueryRequest | None
    interpretation: tuple[Interpretation, ...]
    unresolved_terms: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def chosen(self) -> Candidate | None:
        return self.candidates[0] if self.status == "resolved" and self.candidates else None


# --- candidates -----------------------------------------------------------------------------


def _one_line(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_DESCRIPTION else flat[: MAX_DESCRIPTION - 1].rstrip() + "…"


def _label(hit: Hit, model: Model) -> str:
    kind, _, name = hit.object_id.partition(":")
    if kind == "metric" and name in model.metrics:
        return model.metrics[name].label
    return hit.names.split(" ; ")[0]


def _approved_metric(model: Model, name: str) -> Metric | None:
    found = model.metrics.get(name)
    return found if found is not None and found.status == "approved" else None


def _candidates(
    found: SearchResult, model: Model, glossary_related: Mapping[str, Sequence[str]]
) -> list[Candidate]:
    """The metric hits, then the related metrics of a glossary term that explains the question
    better than any metric hit does (its coverage is higher than every metric hit's)."""
    chosen: list[Candidate] = []
    seen: set[str] = set()
    for hit in found.hits:
        kind, _, name = hit.object_id.partition(":")
        if kind != "metric" or _approved_metric(model, name) is None:
            continue
        seen.add(name)
        chosen.append(
            Candidate(
                hit.object_id,
                _label(hit, model),
                _one_line(hit.description),
                hit.score,
                hit.coverage,
                hit.matched_terms,
                hit.pre_fusion_score,
            )
        )
    best = max((c.coverage for c in chosen), default=0.0)
    for hit in found.hits:
        if hit.kind != "glossary_term" or hit.coverage <= best:
            continue
        for related in glossary_related.get(hit.object_id, ()):
            kind, _, name = related.partition(":")
            metric = _approved_metric(model, name) if kind == "metric" else None
            if metric is None or name in seen:
                continue
            seen.add(name)
            chosen.append(
                Candidate(
                    related,
                    metric.label,
                    _one_line(metric.description),
                    hit.score,
                    hit.coverage,
                    hit.matched_terms,
                    hit.pre_fusion_score,
                    via=hit.object_id,
                )
            )
    chosen.sort(key=lambda c: -c.score)
    return chosen


# --- phrases --------------------------------------------------------------------------------


def _same(word: str, name: str) -> bool:
    """A word of the question is a word of a name, or its plural ("regions", "branches")."""
    return word == name or (len(name) > 2 and word in (name + "s", name + "es"))


def _at(words: Sequence[str], i: int, phrase: tuple[str, ...], taken: set[int]) -> bool:
    if i + len(phrase) > len(words):
        return False
    return all(i + k not in taken and _same(words[i + k], phrase[k]) for k in range(len(phrase)))


def _longest[T](
    words: Sequence[str], i: int, table: Mapping[tuple[str, ...], T], taken: set[int]
) -> tuple[tuple[str, ...], T] | None:
    """The longest phrase of `table` that the words from `i` on spell, if any."""
    for phrase in sorted(table, key=lambda p: -len(p)):
        if _at(words, i, phrase, taken):
            return phrase, table[phrase]
    return None


def _phrase(text: str) -> tuple[str, ...]:
    return tuple(tokens(text))[:MAX_PHRASE_WORDS]


def _speakable_dimensions(
    model: Model, metric: Metric, policy: GovernancePolicy
) -> tuple[list[tuple[str, Dimension]], str | None]:
    """The dimensions of `metric` that the resolver may match: available, and not personal (a
    question names one by accident, and a request the resolver builds should not group or filter
    by one; the compiler would still decide). Plus a note if they could not be worked out."""
    try:
        options = available_dimensions(model, metric, governance=policy).options
    except QueryError:
        return [], f"The dimensions of {metric.name} could not be worked out, so none was matched."
    speakable: list[tuple[str, Dimension]] = []
    for option in options:
        found = model.dimension(option.ref)
        if not option.available or found is None:
            continue
        entity, dimension = found
        if model.effective_sensitivity(entity, dimension) != "personal":
            speakable.append((option.ref, dimension))
    return speakable, None


def _other_dimensions(
    model: Model, speakable: Sequence[tuple[str, Dimension]]
) -> list[tuple[str, Dimension]]:
    """The non-personal dimensions of the model that the metric does not offer."""
    offered = {ref for ref, _ in speakable}
    others: list[tuple[str, Dimension]] = []
    for entity in model.entities.values():
        for dimension in entity.dimensions:
            ref = f"{entity.name}.{dimension.name}"
            if ref not in offered and model.effective_sensitivity(entity, dimension) != "personal":
                others.append((ref, dimension))
    return others


def _dimension_phrases(
    speakable: Sequence[tuple[str, Dimension]],
) -> dict[tuple[str, ...], list[str]]:
    """Phrase to the refs of the dimensions that carry it, from the name, the label, and every
    synonym of each dimension."""
    table: dict[tuple[str, ...], list[str]] = {}
    for ref, dimension in speakable:
        names = [dimension.name.replace("_", " "), dimension.label or ""]
        for synonyms in (dimension.synonyms or {}).values():
            names += synonyms
        for name in names:
            phrase = _phrase(name)
            if phrase and ref not in table.setdefault(phrase, []):
                table[phrase].append(ref)
    return table


def _value_phrases(
    speakable: Sequence[tuple[str, Dimension]],
) -> dict[tuple[str, ...], list[tuple[str, str | int | float | bool]]]:
    """Phrase to `(dimension ref, declared value)` for the declared values of the dimensions.

    A value is known by its text (`car_backed` is "car backed") and by the first sentence of its
    description when that is at most three words ("Syariah. Contract under sharia principles.").
    A value of fewer than three characters is never matched.
    """
    table: dict[tuple[str, ...], list[tuple[str, str | int | float | bool]]] = {}
    for ref, dimension in speakable:
        for declared in dimension.values or ():
            texts = [str(declared.value).replace("_", " ")]
            lead = (declared.description or "").partition(".")[0]
            if lead and len(tokens(lead)) <= MAX_LEAD_WORDS:
                texts.append(lead)
            for text in texts:
                phrase = _phrase(text)
                if phrase and len(" ".join(phrase)) >= MIN_VALUE_LENGTH:
                    entry = (ref, declared.value)
                    if entry not in table.setdefault(phrase, []):
                        table[phrase].append(entry)
    return table


# --- reading a question ---------------------------------------------------------------------


@dataclass
class _Reading:
    """What the words of one question were read as, with the word positions each phrase used."""

    words: list[str]
    taken: set[int]
    interpretation: list[Interpretation]
    notes: list[str]
    time_start: str | None = None
    time_end: str | None = None
    grain: str | None = None
    dimensions: list[str] | None = None
    filters: list[RequestFilter] | None = None

    def use(self, first: int, last: int) -> str:
        self.taken.update(range(first, last))
        return " ".join(self.words[first:last])


def _read_time_and_grain(reading: _Reading, today: datetime.date, metric: Metric | None) -> None:
    """The time range and the grain. Both are read before a metric is known to be chosen, so the
    words show up in the answer for `ambiguous` and `no_match` too; the metric's default grain
    (for "trend") is only known when there is a metric."""
    found = find_times(reading.words, today)
    if found:
        match = found[0]
        phrase = reading.use(match.first, match.last)
        reading.time_start, reading.time_end = match.start.isoformat(), match.end.isoformat()
        reading.interpretation.append(
            Interpretation("time", phrase, f"{reading.time_start} up to {reading.time_end}")
        )
        if match.note:
            reading.notes.append(f"{phrase}: {match.note} is used.")
        for other in found[1:]:
            reading.notes.append(
                f"Only the first time phrase is used; "
                f'"{" ".join(reading.words[other.first : other.last])}" was left out.'
            )
    grain, trend_without_default = find_grain(
        reading.words, reading.taken, metric.default_grain if metric else None
    )
    # A trend word is answered by whatever grain the question gave, so it is not left over.
    reading.taken.update(i for i, w in enumerate(reading.words) if w in TREND_WORDS)
    if grain is not None:
        phrase = reading.use(grain.first, grain.last)
        reading.grain = grain.grain
        suffix = " (the metric's default grain)" if grain.trend else ""
        reading.interpretation.append(Interpretation("grain", phrase, grain.grain + suffix))
    elif trend_without_default and metric is not None:
        reading.notes.append(
            "The question asks for a trend, but the metric has no default grain: say the grain."
        )


def _read_dimensions(
    reading: _Reading,
    table: Mapping[tuple[str, ...], list[str]],
    elsewhere: Mapping[tuple[str, ...], list[str]],
    metric: str,
) -> None:
    """Dimensions: the phrases after a cue ("by", "per", "each", "tiap"), one or more.

    A phrase that names a dimension the metric cannot be cut by (`elsewhere`) is not guessed at: it
    stays unresolved, with a note that says so.
    """
    refs: list[str] = []
    words = reading.words
    for i, word in enumerate(words):
        if word not in GROUP_CUES or i in reading.taken:
            continue
        j = i + 1
        used: list[int] = []
        while j < len(words):
            if words[j] in ("and", "dan") and j + 1 < len(words) and used:
                j += 1
            found = _longest(words, j, table, reading.taken)
            if found is None:
                other = _longest(words, j, elsewhere, reading.taken)
                if other is not None and not used:
                    reading.notes.append(
                        f'"{" ".join(other[0])}" names {", ".join(sorted(other[1]))}, '
                        f"which {metric} cannot be cut by."
                    )
                break
            phrase, options = found
            if len(options) > 1:
                reading.notes.append(
                    f'"{" ".join(phrase)}" could mean {", ".join(sorted(options))}: '
                    "name the one you want."
                )
                break
            if options[0] not in refs:
                refs.append(options[0])
            shown = " ".join(words[j : j + len(phrase)])
            reading.interpretation.append(
                Interpretation("dimension", f"{word} {shown}" if not used else shown, options[0])
            )
            used += [j + k for k in range(len(phrase))]
            reading.taken.update(used[-len(phrase) :])
            j += len(phrase)
        if used:
            reading.taken.add(i)
    reading.dimensions = refs


def _read_filters(
    reading: _Reading,
    table: Mapping[tuple[str, ...], list[tuple[str, str | int | float | bool]]],
    explained: frozenset[str],
) -> None:
    """Filters: the phrases that spell a declared value. A word the metric's own names matched
    is the metric's, not a value ("approved" in "approved applications")."""
    words = reading.words
    # A phrase that two dimensions carry is not guessed.
    free = {i for i, word in enumerate(words) if word in explained}
    found_filters: list[RequestFilter] = []
    i = 0
    while i < len(words):
        found = _longest(words, i, table, reading.taken | free)
        if found is None:
            i += 1
            continue
        phrase, options = found
        if len(options) > 1:
            where = ", ".join(sorted({ref for ref, _ in options}))
            reading.notes.append(
                f'"{" ".join(phrase)}" is a declared value of more than one dimension '
                f"({where}): name the dimension."
            )
            i += len(phrase)
            continue
        ref, value = options[0]
        shown = " ".join(words[i : i + len(phrase)])
        reading.use(i, i + len(phrase))
        found_filters.append(RequestFilter(dimension=ref, op="=", value=value))
        reading.interpretation.append(Interpretation("filter", shown, f"{ref} = {value}"))
        i += len(phrase)
    reading.filters = found_filters


def _unresolved(reading: _Reading, explained: frozenset[str]) -> tuple[str, ...]:
    left: dict[str, None] = {}
    for i, word in enumerate(reading.words):
        if i in reading.taken or word in STOP_WORDS or len(word) < 2 or word in explained:
            continue
        left.setdefault(word)
    return tuple(left)


# --- the resolver ---------------------------------------------------------------------------


def resolve(
    question: str,
    today: datetime.date,
    *,
    found: SearchResult,
    model: Model,
    policy: GovernancePolicy,
    glossary_related: Mapping[str, Sequence[str]] | None = None,
) -> Resolution:
    """Resolve `question`, counting `today` as today.

    `found` is the result of a search over approved metrics and glossary terms for the question;
    `model` is the approved snapshot; `glossary_related` gives the `related` ids of each glossary
    term that may be among the hits. `policy` decides which dimensions are available (D-29).
    """
    if isinstance(today, datetime.datetime):
        today = today.date()
    hits = tuple(
        ResolvedHit(h.object_id, h.kind, _label(h, model), h.score, h.coverage, h.matched_terms)
        for h in found.hits
    )
    candidates = tuple(_candidates(found, model, glossary_related or {}))
    band = found.confidence.band
    words = tokens(question)
    reading = _Reading(words, set(), [], [])

    def result(
        status: Status,
        request: QueryRequest | None = None,
        explained: frozenset[str] = frozenset(),
        ambiguous: tuple[str, ...] = (),
    ) -> Resolution:
        return Resolution(
            status,
            question,
            today,
            band,
            hits,
            candidates,
            ambiguous,
            request,
            tuple(reading.interpretation),
            _unresolved(reading, explained),
            tuple(reading.notes),
        )

    if band == "low" or not candidates:
        _read_time_and_grain(reading, today, None)
        reading.notes.insert(
            0,
            "No approved metric clearly matches the question. Read the hits; do not use a "
            "different metric instead.",
        )
        return result("no_match")
    first = candidates[0]
    if len(candidates) > 1 and is_ambiguous(
        first.pre_fusion_score,
        first.coverage,
        candidates[1].pre_fusion_score,
        candidates[1].coverage,
    ):
        _read_time_and_grain(reading, today, None)
        explained = frozenset(first.matched_terms) | frozenset(candidates[1].matched_terms)
        reading.notes.insert(
            0,
            "Two metrics fit the question equally well. Ask which one is meant; nothing is chosen.",
        )
        return result("ambiguous", explained=explained, ambiguous=(first.id, candidates[1].id))

    metric = model.metrics[first.name]
    explained = frozenset(first.matched_terms)
    _read_time_and_grain(reading, today, metric)
    speakable, note = _speakable_dimensions(model, metric, policy)
    if note:
        reading.notes.append(note)
    elsewhere = _dimension_phrases(_other_dimensions(model, speakable))
    _read_dimensions(reading, _dimension_phrases(speakable), elsewhere, metric.name)
    _read_filters(reading, _value_phrases(speakable), explained)
    request = QueryRequest(
        metric=first.name,
        dimensions=tuple(reading.dimensions or ()),
        filters=tuple(reading.filters or ()),
        grain=reading.grain,
        time_start=reading.time_start,
        time_end=reading.time_end,
    )
    return result("resolved", request, explained)
