"""Read a definitions directory into a `DefinitionSet`.

Files are decoded with PyYAML's safe loader only, so nothing in a file can run code. The loader
goes further than the safe loader requires: it refuses YAML aliases, duplicate keys, and more than
one document per file, and it keeps dates as plain text (a YAML date is not a string).

A file that cannot be read, decoded, or validated against the schema is reported as V01 and kept
out of the cross-file checks. A directory whose name starts with a dot (such as `.git`) is skipped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from makna.definitions.issues import Issue
from makna.model.definitions import AnyDefinition, parse_definition

PROPOSAL_SUFFIX = ".draft.yaml"
OFFICIAL_SUFFIX = ".yaml"

PathKey = tuple[str | int, ...]


class _SafeLoader(yaml.SafeLoader):
    """SafeLoader without aliases, duplicate keys, or implicit timestamps."""

    yaml_implicit_resolvers = {
        first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def compose_node(self, parent: Node | None, index: Any) -> Node | None:
        if self.check_event(yaml.AliasEvent):
            raise yaml.composer.ComposerError(
                None, None, "YAML aliases are not allowed", self.get_mark()
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if isinstance(key_node, ScalarNode):
                key = self.construct_object(key_node, deep=True)
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicate key {key!r}", key_node.start_mark
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


@dataclass(frozen=True)
class LineMap:
    """Line numbers (1-based) of the keys and list items of one parsed file."""

    lines: dict[PathKey, int] = field(default_factory=dict)

    def line_of(self, path: PathKey) -> int | None:
        """The line of `path`, or of its nearest ancestor that has one."""
        for length in range(len(path), -1, -1):
            found = self.lines.get(path[:length])
            if found is not None:
                return found
        return None

    def line_of_loose(self, path: PathKey) -> int | None:
        """Like `line_of`, but skips path steps that match nothing (union tags in error paths)."""
        current: PathKey = ()
        line = self.lines.get(())
        for step in path:
            candidate = (*current, step)
            if candidate in self.lines:
                current = candidate
                line = self.lines[candidate]
        return line


def _collect_lines(node: Node, path: PathKey, out: dict[PathKey, int]) -> None:
    if isinstance(node, MappingNode):
        for key_node, value_node in node.value:
            if isinstance(key_node, ScalarNode):
                child = (*path, str(key_node.value))
                out[child] = key_node.start_mark.line + 1
                _collect_lines(value_node, child, out)
    elif isinstance(node, SequenceNode):
        for index, item in enumerate(node.value):
            child = (*path, index)
            out[child] = item.start_mark.line + 1
            _collect_lines(item, child, out)


def parse_yaml(text: str) -> tuple[Any, LineMap]:
    """Decode one YAML document with the safe loader. Raises `yaml.YAMLError`."""
    loader = _SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None, LineMap({})
        lines: dict[PathKey, int] = {(): node.start_mark.line + 1}
        _collect_lines(node, (), lines)
        return loader.construct_document(node), LineMap(lines)
    finally:
        loader.dispose()


@dataclass(frozen=True)
class DefinitionFile:
    """One file of the repository. `definition` is None when the file failed V01."""

    path: str
    is_proposal: bool
    definition: AnyDefinition | None
    lines: LineMap = field(default_factory=LineMap)

    def issue(self, code: str, message: str, where: PathKey = ()) -> Issue:
        return Issue(self.path, self.lines.line_of(where), code, message)


@dataclass
class DefinitionSet:
    """Everything read from one directory, plus the problems found while reading."""

    root: Path
    files: list[DefinitionFile] = field(default_factory=list)
    load_issues: list[Issue] = field(default_factory=list)

    def valid_files(self) -> list[DefinitionFile]:
        return [f for f in self.files if f.definition is not None]


def _describe(error: Any) -> str:
    message = str(error["msg"])
    message = message.removeprefix("Value error, ")
    loc = list(error["loc"])
    if loc and loc[0] in {"domain", "entity", "metric", "glossary_term"}:
        loc = loc[1:]  # the first step is the kind that selected the model
    path = ".".join(str(part) for part in loc if not _is_union_tag(part))
    return f"{path}: {message}" if path else message


def _is_union_tag(part: object) -> bool:
    return isinstance(part, str) and part in {"filter", "rule"}


def load_text(path: str, text: str) -> tuple[DefinitionFile, list[Issue]]:
    """Decode and schema-check one file's text. `path` is relative to the definitions root."""
    is_proposal = path.endswith(PROPOSAL_SUFFIX)
    try:
        data, lines = parse_yaml(text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        problem = getattr(error, "problem", None) or str(error)
        message = f"not readable as YAML: {str(problem).splitlines()[0]}"
        return DefinitionFile(path, is_proposal, None), [Issue(path, line, "V01", message)]
    if data is None:
        return DefinitionFile(path, is_proposal, None), [Issue(path, None, "V01", "file is empty")]
    try:
        definition = parse_definition(data)
    except ValidationError as error:
        issues = [
            Issue(path, lines.line_of_loose(tuple(e["loc"])), "V01", _describe(e))
            for e in error.errors(include_url=False)
        ]
        return DefinitionFile(path, is_proposal, None, lines), _dedupe(issues)
    return DefinitionFile(path, is_proposal, definition, lines), []


def _dedupe(issues: list[Issue]) -> list[Issue]:
    return list(dict.fromkeys(issues))


def load_definitions(root: Path | str) -> DefinitionSet:
    """Read every `*.yaml` file under `root` (skipping dot-directories) into a `DefinitionSet`."""
    base = Path(root)
    result = DefinitionSet(base)
    if not base.is_dir():
        result.load_issues.append(Issue(str(base), None, "V01", "not a directory"))
        return result
    paths: list[Path] = []
    for directory, subdirectories, filenames in os.walk(base):
        subdirectories[:] = sorted(d for d in subdirectories if not d.startswith("."))
        paths.extend(Path(directory) / n for n in sorted(filenames) if not n.startswith("."))
    for path in sorted(paths):
        relative = path.relative_to(base)
        name = relative.as_posix()
        if path.suffix == ".yml":
            result.load_issues.append(Issue(name, None, "V02", "use the extension .yaml"))
            continue
        if path.suffix != ".yaml":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            result.load_issues.append(Issue(name, None, "V01", f"cannot read file: {error}"))
            continue
        file, issues = load_text(name, text)
        result.files.append(file)
        result.load_issues.extend(issues)
    return result
