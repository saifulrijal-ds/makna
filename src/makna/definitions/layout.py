"""Where a definition lives in the repository (ARCHITECTURE section 5.2), in both directions.

A path is built only from an object id (`kind:name`, both matching the name pattern) and a domain
name that matches the same pattern, so no text a caller sends can name a file, a folder, or a
`..`. `parse_path` is the reverse and is used on paths that come out of git, which are not
trusted: a path that does not have exactly the shape of a definition file is not one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from makna.definitions.loader import OFFICIAL_SUFFIX, PROPOSAL_SUFFIX
from makna.definitions.validate import KIND_FOLDERS
from makna.model.definitions import NAME_PATTERN

KINDS = ("domain", "entity", "metric", "glossary_term")
_NAME = re.compile(NAME_PATTERN)
_FOLDER_KINDS = {folder: kind for kind, folder in KIND_FOLDERS.items()}


@dataclass(frozen=True)
class ObjectRef:
    kind: str
    name: str

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.name}"


def parse_object_id(object_id: str) -> ObjectRef | None:
    """`kind:name` as an `ObjectRef`, or None when it is not the id of a definition object."""
    kind, _, name = object_id.partition(":")
    if kind not in KINDS or _NAME.fullmatch(name) is None:
        return None
    return ObjectRef(kind, name)


def object_path(ref: ObjectRef, domain: str, *, proposal: bool) -> str:
    """The repository path of an object's official file or proposal file.

    `domain` is the object's domain (for a domain object, its own name). Raises `ValueError` for
    anything that does not match the name pattern, so a path never holds other characters.
    """
    if ref.kind not in KINDS or _NAME.fullmatch(ref.name) is None:
        raise ValueError("not an object id")
    if _NAME.fullmatch(domain) is None:
        raise ValueError("not a domain name")
    suffix = PROPOSAL_SUFFIX if proposal else OFFICIAL_SUFFIX
    if ref.kind == "domain":
        if domain != ref.name:
            raise ValueError("a domain's own domain is its name")
        return f"{domain}/domain{suffix}"
    return f"{domain}/{KIND_FOLDERS[ref.kind]}/{ref.name}{suffix}"


@dataclass(frozen=True)
class PathInfo:
    ref: ObjectRef
    domain: str
    is_proposal: bool


def parse_path(path: str) -> PathInfo | None:
    """The object a repository path holds, or None for any path that is not exactly
    `<domain>/domain.yaml`, `<domain>/<folder>/<name>.yaml`, or the `.draft.yaml` of either."""
    parts = path.split("/")
    is_proposal = path.endswith(PROPOSAL_SUFFIX)
    if not is_proposal and not path.endswith(OFFICIAL_SUFFIX):
        return None
    suffix = PROPOSAL_SUFFIX if is_proposal else OFFICIAL_SUFFIX
    if len(parts) == 2 and _NAME.fullmatch(parts[0]) and parts[1] == f"domain{suffix}":
        return PathInfo(ObjectRef("domain", parts[0]), parts[0], is_proposal)
    if len(parts) == 3 and _NAME.fullmatch(parts[0]) and parts[1] in _FOLDER_KINDS:
        name = parts[2].removesuffix(suffix)
        if _NAME.fullmatch(name):
            return PathInfo(ObjectRef(_FOLDER_KINDS[parts[1]], name), parts[0], is_proposal)
    return None
