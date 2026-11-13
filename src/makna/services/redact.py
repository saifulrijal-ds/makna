"""Removing personal data that can be detected, before anything is stored (D-24, GOV-2).

`redact` is a pure function over text. It first **normalises**: Unicode NFKC (full-width digits
and `＠` become ordinary ones), zero-width and other invisible format characters are dropped,
and percent escapes (`%40`) and HTML character references (`&#64;`) are decoded once. The text it
returns is the redacted normalised text: the log does not need the original spelling.

Then it replaces, with `[REDACTED]`:

- email addresses: any word with an `@` in it, and the spelled-out forms `name at domain dot
  com`, `name (at) domain`, `name [at] domain`. The spelled-out form needs a domain-like part
  after "at" (a dot and letters), so "look at region" stays;
- phone-like numbers: international forms with a `+` (at least seven digits) and Indonesian forms
  starting with `0` (mobile and landline, with an optional bracketed area code);
- any run of 9 or more digits. Between digits a run may have up to three of: a space of any
  kind, `.`, `-`, `/`, `_`, `,`, or a dash character, so `4111 1111 1111 1111`,
  `4111 - 1111 - 1111 - 1111`, `4111/1111/1111/1111` and `4111–1111–1111–1111` are all caught.

ISO dates and times (`2026-06-30`, `2026-06-30 12:00`, `2026-06-30T12:00:00`) are spared before
the digit rule, so that text naming a time stays readable. A date is spared only when no digit
continues it, so a card number written in a date's shape is still caught. A name typed into free
text cannot be detected, so the tool-call log stays visible to approvers and admins only (D-24).
`clip` cuts text to a length without leaving the start of a number at the cut.
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from typing import Any
from urllib.parse import unquote

REDACTED = "[REDACTED]"

_SEPARATORS = r"[\W_]"  # a character that is neither a letter nor a digit
_EMAIL = re.compile(r"\S*@\S*")
_EMAIL_WORDS = re.compile(
    r"[\w.+-]+\s*(?:\(at\)|\[at\]|\bat\b)\s*[a-z0-9-]+"
    r"(?:\s*(?:\(dot\)|\[dot\]|\bdot\b|\.)\s*[a-z0-9-]+)*"
    r"\s*(?:\(dot\)|\[dot\]|\bdot\b|\.)\s*[a-z]{2,}",
    re.IGNORECASE,
)
# A plus sign, then at least seven digits with the usual separators.
_INTERNATIONAL = re.compile(rf"\+\s?\d(?:{_SEPARATORS}{{0,3}}\d){{6,}}")
# 0, an area or operator code of one to three digits (bracketed or not), then two groups.
_INDONESIAN = re.compile(r"(?<![\d])\(?0\d{1,3}\)?[\s.\-]?\d{3,4}(?:[\s.\-]?\d{2,5})?(?![\d])")
_DIGIT_RUN = re.compile(rf"\(?\d(?:{_SEPARATORS}{{0,3}}\d){{8,}}")
_DATE = re.compile(
    r"(?<!\d)"
    r"(?:19|20|21)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"(?:[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?)?"
    rf"(?!\d)(?!{_SEPARATORS}{{1,3}}\d)"
)
_AFTER_DIGIT = re.compile(rf"\d{_SEPARATORS}{{0,3}}$")
# Control characters, unpaired surrogates and every private-use code point: the log column cannot
# hold some, and the date markers below are private-use characters a caller must not type.
_UNSTORABLE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ue000-\uf8ff"
    "\U000f0000-\U000ffffd\U00100000-\U0010fffd]"
)
MAX_DECODE_PASSES = 5
_TAIL = re.compile(r"[\d\W_]+$")
_HOLD = "{}"
_HELD = re.compile("(\\d+)")

_PATTERNS = (_EMAIL, _EMAIL_WORDS, _INTERNATIONAL, _DIGIT_RUN, _INDONESIAN)


class TooDeep(ValueError):
    """The text still changed after the last allowed decoding pass."""


def _clean(text: str) -> str:
    """Control characters, unpaired surrogates and private-use characters replaced; invisible,
    format and combining characters (Unicode Cf, Mn, Me) and noncharacters removed."""
    text = _UNSTORABLE.sub("\ufffd", text)
    return "".join(
        c
        for c in text
        if unicodedata.category(c) not in ("Cf", "Mn", "Me")
        and not (0xFDD0 <= ord(c) <= 0xFDEF or ord(c) & 0xFFFE == 0xFFFE)
    )


def normalise(text: str) -> str:
    """Cleaned (see `_clean`), NFKC, percent escapes and HTML character references decoded until
    the text stops changing, and cleaned again after the last decoding, so `%00` cannot bring a
    NUL back. Raises `TooDeep` when the text is still changing after `MAX_DECODE_PASSES`."""
    text = _clean(text)
    for _ in range(MAX_DECODE_PASSES + 1):
        before = text
        text = _clean(unicodedata.normalize("NFKC", text))
        if "%" in text:
            text = _clean(unquote(text))
        if "&" in text:
            text = _clean(html.unescape(text))
        if text == before:
            return text
    raise TooDeep("still encoded after the last decoding pass")


def redact(text: str) -> str:
    """The normalised `text` with emails, phone-like numbers, and runs of 9 or more digits
    replaced. ISO dates and times are left alone."""
    try:
        text = normalise(text)
    except TooDeep:
        return REDACTED  # fail closed: encoded deeper than a reader should be asked to decode
    held: list[str] = []

    def hold(match: re.Match[str]) -> str:
        # Spared only when no digit leads into it through the separators the digit rule joins.
        if _AFTER_DIGIT.search(match.string[: match.start()]):
            return match.group(0)
        held.append(match.group(0))
        return _HOLD.format(len(held) - 1)

    text = _DATE.sub(hold, text)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _HELD.sub(lambda m: held[int(m.group(1))], text)


def clip(text: str, limit: int) -> str:
    """At most `limit` characters of `text`. When it is cut, the digits, separators and signs at
    the cut are dropped too, so that the first digits of a long number are not kept."""
    if len(text) <= limit:
        return text
    return _TAIL.sub("", text[:limit])


def redact_value(value: Any, limit: int = 500) -> Any:
    """`redact` applied to every string in a JSON-like value (strings are clipped first).

    Numbers, booleans and None are kept, except that a whole number of 9 or more digits, and a
    float that is not finite or has 9 or more whole digits, becomes `[REDACTED]`. Anything else
    (an object of another type) becomes `[REDACTED]`.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        # Clip, normalise (which can expand text), then clip again: the stored text stays small.
        try:
            return redact(clip(normalise(clip(value, limit)), limit))
        except TooDeep:
            return REDACTED
    if isinstance(value, int):
        return REDACTED if len(str(abs(value))) >= 9 else value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) >= 1e8:
            return REDACTED
        return value
    if isinstance(value, (list, tuple)):
        return [redact_value(item, limit) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(redact_value(str(key), 100)): redact_value(item, limit)
            for key, item in list(value.items())[:100]
        }
    return REDACTED
