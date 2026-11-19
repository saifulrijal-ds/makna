"""A definition as the YAML the application writes.

The application never writes the text a curator sent. It parses it, checks it as a model, sets
the fields only the application may set (`status`, `version`, `deprecation`), and writes the
model back out in one canonical form: the keys in the order of the model, block style, plain
text, no comments, no anchors. What is committed is therefore exactly the model that was checked,
and the same model always gives the same bytes, so the file hash in an intent row is stable.
"""

from __future__ import annotations

import hashlib

import yaml

from makna.definitions.issues import Issue
from makna.definitions.loader import load_text
from makna.model.definitions import AnyDefinition, Deprecation, dump_definition

MAX_YAML_BYTES = 256 * 1024
"""The largest YAML text a curator may send (256 KiB; the sample's largest file is under 10 KiB)."""


class _Dumper(yaml.SafeDumper):
    """SafeDumper that never writes an alias, and writes a multi-line string as a block."""

    def ignore_aliases(self, data: object) -> bool:
        return True


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_representer)


def to_yaml(definition: AnyDefinition) -> str:
    """The canonical YAML text of `definition`."""
    return yaml.dump(
        dump_definition(definition),
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def with_state(
    definition: AnyDefinition,
    *,
    status: str,
    version: int,
    deprecation: Deprecation | None = None,
) -> AnyDefinition:
    """`definition` with the fields the application owns set: `status`, `version`, and
    `deprecation` (left out of the file when None). Whatever the input said is replaced."""
    copy = definition.model_copy(update={"status": status, "version": version})
    fields = set(copy.model_fields_set)
    copy.deprecation = deprecation
    if deprecation is None:
        fields.discard("deprecation")
    else:
        fields.add("deprecation")
    copy.__pydantic_fields_set__ = fields
    return copy


def parse_text(text: str) -> tuple[AnyDefinition | None, list[Issue]]:
    """Check one YAML text the way a file is checked (safe load, then the model). Returns the
    definition, or None and the problems (rule V01, with the line when it is known)."""
    file, issues = load_text("input.yaml", text)
    return file.definition, issues
