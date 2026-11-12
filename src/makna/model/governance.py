"""The governance policy (D-29): which personal-data rules apply to a compile.

A policy is plain data. The server configuration builds one (`makna.config`) and the caller hands
it to the compiler; a request never carries one. The compiler imports this module and reads no
configuration.

There are three modes:

- `strict` (the default): every rule applies, as DR-5 describes. Only `min_group_size` can be
  set, to 2 or more.
- `custom`: the adopter chooses each rule. Every rule defaults to its strict value.
- `off`: every rule is relaxed.

The four rules:

- `min_group_size`: groups built from fewer than this many records of the entity holding a
  personal dimension are left out. 0 turns the rule off; otherwise at least 2, because 1 keeps
  every group and would switch the rule off without any sign.
- `allow_personal_filters`: when true, filters on personal dimensions (a time range on a personal
  official date counts) are allowed.
- `apply_masks`: when false, a personal dimension that has a masking rule returns the raw value.
- `allow_unmasked_personal`: when true, a personal dimension with no masking rule may be selected;
  it returns the raw value.

An inconsistent policy cannot exist: the constructors `strict`, `custom`, and `off` build the
three modes, and the type refuses any combination that does not belong to its mode, however it
is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

GovernanceMode = Literal["strict", "custom", "off"]
MODES: tuple[str, ...] = get_args(GovernanceMode)

DEFAULT_MIN_GROUP_SIZE = 10
MIN_GROUP_SIZE_FLOOR = 2

# The rule names, in the order they are listed in warnings and explanations.
RULES: tuple[str, ...] = (
    "min_group_size",
    "allow_personal_filters",
    "apply_masks",
    "allow_unmasked_personal",
)


def _is_whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class GovernancePolicy:
    """The personal-data rules of one deployment. Build it with `strict`, `custom`, or `off`."""

    mode: GovernanceMode
    min_group_size: int
    allow_personal_filters: bool
    apply_masks: bool
    allow_unmasked_personal: bool

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, not {self.mode!r}")
        if not _is_whole(self.min_group_size):
            raise ValueError(f"min_group_size must be a whole number, not {self.min_group_size!r}")
        for name in ("allow_personal_filters", "apply_masks", "allow_unmasked_personal"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be true or false, not {getattr(self, name)!r}")
        if self.mode == "off":
            self._require(
                self.min_group_size == 0
                and self.allow_personal_filters
                and not self.apply_masks
                and self.allow_unmasked_personal,
                "mode off relaxes every rule: min_group_size 0, allow_personal_filters true, "
                "apply_masks false, allow_unmasked_personal true",
            )
        elif self.mode == "strict":
            self._require(
                self.min_group_size >= MIN_GROUP_SIZE_FLOOR,
                f"min_group_size must be at least {MIN_GROUP_SIZE_FLOOR} in mode strict, "
                f"not {self.min_group_size}: a group of one is a single record",
            )
            self._require(
                not self.allow_personal_filters
                and self.apply_masks
                and not self.allow_unmasked_personal,
                "mode strict sets no custom rule: allow_personal_filters false, apply_masks "
                "true, allow_unmasked_personal false",
            )
        else:
            self._require(
                self.min_group_size == 0 or self.min_group_size >= MIN_GROUP_SIZE_FLOOR,
                f"min_group_size must be 0 (rule off) or at least {MIN_GROUP_SIZE_FLOOR} in mode "
                f"custom, not {self.min_group_size}: a group of one is a single record",
            )

    @staticmethod
    def _require(ok: bool, message: str) -> None:
        if not ok:
            raise ValueError(message)

    @classmethod
    def strict(cls, min_group_size: int = DEFAULT_MIN_GROUP_SIZE) -> GovernancePolicy:
        """DR-5 as built. `min_group_size` is at least 2."""
        return cls("strict", min_group_size, False, True, False)

    @classmethod
    def custom(
        cls,
        *,
        min_group_size: int = DEFAULT_MIN_GROUP_SIZE,
        allow_personal_filters: bool = False,
        apply_masks: bool = True,
        allow_unmasked_personal: bool = False,
    ) -> GovernancePolicy:
        """The rules the adopter names; each one defaults to its strict value. `min_group_size`
        is 0 (rule off) or at least 2."""
        return cls(
            "custom", min_group_size, allow_personal_filters, apply_masks, allow_unmasked_personal
        )

    @classmethod
    def off(cls) -> GovernancePolicy:
        """Every rule relaxed."""
        return cls("off", 0, True, False, True)

    def relaxed(self) -> tuple[tuple[str, int | bool], ...]:
        """The rules this policy relaxes, as `(name, value)` in the order of `RULES`.

        A rule is relaxed when it is off: `min_group_size` 0, `allow_personal_filters` true,
        `apply_masks` false, `allow_unmasked_personal` true. A minimum other than 0 is not
        relaxed, whatever its size.
        """
        found: list[tuple[str, int | bool]] = []
        if self.min_group_size == 0:
            found.append(("min_group_size", 0))
        if self.allow_personal_filters:
            found.append(("allow_personal_filters", True))
        if not self.apply_masks:
            found.append(("apply_masks", False))
        if self.allow_unmasked_personal:
            found.append(("allow_unmasked_personal", True))
        return tuple(found)

    def may_return_raw(self, has_mask: bool) -> bool:
        """True if a personal dimension may be returned unmasked: one with a masking rule when
        `apply_masks` is false, one without when `allow_unmasked_personal` is true."""
        return not self.apply_masks if has_mask else self.allow_unmasked_personal


STRICT = GovernancePolicy.strict()
