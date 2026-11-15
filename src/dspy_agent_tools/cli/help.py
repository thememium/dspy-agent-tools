"""Parse command-line help text into usage, commands, flags, and arguments."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_OVERSTRIKE = re.compile(r".\x08")
_MAN_HEADER = re.compile(r"^[A-Z][A-Z0-9-]+\(\d+\)\s*$", re.MULTILINE)
_HEADER = re.compile(r"^\s*([A-Z][A-Za-z0-9 _/()-]{1,60}):\s*$")
_ALLCAPS = re.compile(r"^\s*([A-Z][A-Z0-9 _/()-]{1,60})\s*$")
_COMMAND = re.compile(
    r"^\s{2,}(?P<name>[A-Za-z0-9][\w.:-]*)(?:,\s*[A-Za-z0-9][\w-]*)?"
    r"\s{2,}(?P<desc>\S.*)$"
)
_FLAG = re.compile(r"^\s+(?P<head>-.*?)(?:(?:\s{2,}|\t+)(?P<desc>\S.*))?$")
_OPTIONAL_VALUE = re.compile(r"\[=(<[^>]+>|[^\]]*)\]$")
_DEFAULT = re.compile(r"\((?:default|Default):?\s*([^)]+)\)")
_POSSIBLE = re.compile(r"\[possible values:\s*([^\]]+)\]", re.IGNORECASE)
_BRACED_CHOICES = re.compile(r"\{([^{}]+)\}")
_ONE_OF = re.compile(r"One of:\s*\(([^)]+)\)", re.IGNORECASE)
_PIPE_CHOICES = re.compile(r"\((\w+(?:\|\w+)+)\)")
_USAGE = re.compile(r"^\s*usage:\s*(.*)$", re.IGNORECASE)
_USAGE_HEADER = re.compile(r"^\s*USAGE\s*$")
_OR_LINE = re.compile(r"^\s*or:", re.IGNORECASE)
_VALUE_NAME = re.compile(
    r"(?:^|[-_])(?:limit|max|min|count|size|depth|timeout|format|output|"
    r"file|path|query|ref|state|label|repo|since|until|before|after|number|"
    r"page|offset|filter|type|mode|level|width|height)$",
    re.IGNORECASE,
)

_META_COMMANDS = {
    "__complete",
    "__completeNoDesc",
    "completion",
    "completions",
    "help",
    "help-all",
    "version",
}

_INT_TYPES = {
    "int",
    "int32",
    "int64",
    "integer",
    "number",
    "float",
    "port",
    "uint",
    "uint16",
    "uint32",
    "uint64",
}
_ARRAY_TYPES = {"array", "list", "slice", "strings"}
_OBJECT_TYPES = {"map", "object"}


def _clean(text: str) -> str:
    """Remove terminal escapes, carriage returns, and overstrike."""
    text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return _OVERSTRIKE.sub("", text)


def _is_man_page(text: str) -> bool:
    """Recognize a pager/man rendering that is not usable as help."""
    return "\x08" in text or "\n.TH " in text or bool(_MAN_HEADER.search(text))


def _sections(text: str) -> list[tuple[str | None, list[str]]]:
    """Split help into ``(header, body)`` blocks, preserving order."""
    sections: list[tuple[str | None, list[str]]] = []
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        match = _ALLCAPS.match(line) or _HEADER.match(line)
        if match:
            if body:
                sections.append((current, body))
            current = match.group(1).strip()
            body = []
        else:
            body.append(line)
    if body:
        sections.append((current, body))
    return sections


def _classify(header: str | None) -> str | None:
    if not header or len(header.split()) > 4:
        return None
    upper = header.upper()
    if "COMMAND" in upper:
        return "commands"
    if "OPTION" in upper or "FLAG" in upper:
        return "flags"
    if "ARGUMENT" in upper or "POSITIONAL" in upper:
        return "arguments"
    return None


def _usage(text: str) -> str:
    """Extract the usage block from help text."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _USAGE.match(line)
        if match:
            block = [match.group(1).strip()] if match.group(1).strip() else []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if not stripped:
                    break
                if _HEADER.match(stripped) or _ALLCAPS.match(stripped):
                    break
                if following.startswith((" ", "\t")) or _OR_LINE.match(following):
                    block.append(stripped)
                else:
                    break
            return "\n".join(block)
        if _USAGE_HEADER.match(line):
            block = []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if not stripped:
                    break
                if _HEADER.match(stripped) or _ALLCAPS.match(stripped):
                    break
                block.append(stripped)
            return "\n".join(block)
    return ""


def _description(text: str) -> str:
    """Return the first prose line of a help page.

    Usage continuations, flag rows, and section headers are skipped. Unindented
    prose is preferred; indented prose is used only when nothing else exists.
    """
    candidates: list[str] = []
    in_usage = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            in_usage = False
            continue
        if _USAGE.match(stripped):
            in_usage = True
            continue
        if _USAGE_HEADER.match(line):
            in_usage = True
            continue
        if in_usage:
            if line.startswith((" ", "\t")):
                continue
            in_usage = False
        if _ALLCAPS.match(stripped) or stripped.startswith("-"):
            continue
        header = _HEADER.match(stripped)
        if (
            header
            and len(header.group(1).split()) <= 4
            and any(
                word in header.group(1).upper()
                for word in (
                    "ALIAS",
                    "ARGUMENT",
                    "COMMAND",
                    "ENVIRONMENT",
                    "EXAMPLE",
                    "FLAG",
                    "OPTION",
                    "USAGE",
                )
            )
        ):
            continue
        if line != line.lstrip():
            candidates.append(stripped)
            continue
        return stripped
    return candidates[0] if candidates else ""


def _choices(description: str) -> list[str] | None:
    match = _POSSIBLE.search(description)
    if match is None:
        match = _BRACED_CHOICES.search(description)
    if match is None:
        match = _ONE_OF.search(description)
    if match is None:
        match = _PIPE_CHOICES.search(description)
    if match is None:
        return None
    values = [
        value.strip().strip("'\"")
        for value in re.split(r"[,|]", match.group(1))
        if value.strip()
    ]
    return values or None


@dataclass(slots=True)
class _Flag:
    names: list[str]
    kind: str
    default: str | None
    choices: list[str] | None
    repeatable: bool
    description: str

    def record(self) -> dict[str, Any]:
        return {
            "names": list(self.names),
            "type": self.kind,
            "default": self.default,
            "choices": list(self.choices) if self.choices else None,
            "repeatable": self.repeatable,
            "description": self.description,
        }

    def line(self) -> str:
        meta = [self.kind]
        if self.choices:
            meta.append("choices: " + "|".join(self.choices))
        if self.default is not None:
            meta.append(f"default: {self.default}")
        if self.repeatable:
            meta.append("repeatable")
        description = f" — {self.description}" if self.description else ""
        return f"  {', '.join(self.names)} ({'; '.join(meta)}){description}"


def _parse_flag_head(head: str, description: str) -> _Flag | None:
    """Parse ``-x, --long TYPE`` plus a default and description."""
    head = head.strip().rstrip(":")
    if not head.startswith("-"):
        return None

    names: list[str] = []
    default: str | None = None
    value_spec: str | None = None
    repeatable = False
    for part in head.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split(None, 1)
        name = tokens[0]
        if len(tokens) > 1 and value_spec is None:
            value_spec = tokens[1].strip()

        optional = _OPTIONAL_VALUE.search(name)
        if optional:
            if value_spec is None and "<" in optional.group(1):
                value_spec = optional.group(1)
            name = name[: optional.start()]

        if name.startswith("--") and "=" in name:
            name, default = name.split("=", 1)
        if name.endswith("..."):
            name = name[:-3]
            repeatable = True
        name = name.replace("[no-]", "")
        if name.startswith("-"):
            names.append(name)

    if not names:  # pragma: no cover - defensive; the head must start with "-"
        return None

    if value_spec is not None:
        value_spec = value_spec.strip()
        if value_spec.endswith("..."):
            value_spec = value_spec[:-3]
            repeatable = True
        value_spec = value_spec.strip("<>[]")

    if default is None:
        match = _DEFAULT.search(description)
        if match:
            default = match.group(1).strip()

    # A bare ``--flag=VALUE`` line with no description is usually a usage
    # template, not an actual default (for example ``--engine=ENGINE``).
    if (
        default is not None
        and value_spec is None
        and not description.strip()
        and re.fullmatch(r"[A-Z][A-Z0-9+?_<>./-]*", default)
    ):
        value_spec = default
        default = None

    lowered = (value_spec or "").lower()
    kind = "boolean"
    if (
        not lowered
        and default is None
        and any(_VALUE_NAME.search(name.lstrip("-")) for name in names)
    ):
        kind = "value"
    if lowered:
        if lowered in _INT_TYPES:
            kind = "integer"
        elif lowered in _OBJECT_TYPES:
            kind = "object"
        elif lowered in _ARRAY_TYPES or repeatable:
            kind = "array"
        else:
            kind = "string"

    if default is not None:
        bare = default.strip().strip("'\"")
        if bare in {"true", "false"}:
            kind = "boolean"
        elif bare in {"[]", "[ ]"}:
            kind = "array"
        elif bare in {"{}", "map[]"}:
            kind = "object"
        elif bare == "" and kind == "boolean":
            kind = "string"
        elif re.fullmatch(r"-?\d+", bare) and kind == "boolean":
            kind = "integer"
        elif kind == "boolean":
            kind = "string"

    if lowered in _ARRAY_TYPES:
        repeatable = True

    return _Flag(
        names=names,
        kind=kind,
        default=default,
        choices=_choices(description),
        repeatable=repeatable,
        description=description.strip(),
    )


def _parse_positionals(lines: Sequence[str], positionals: dict[str, str]) -> None:
    """Collect ``name  description`` rows from an Arguments section."""
    pending: str | None = None
    for line in lines:
        if not line.strip():
            pending = None
            continue
        match = _COMMAND.match(line)
        if match:
            name = match.group("name")
            positionals[name] = match.group("desc").strip()
            pending = name
            continue
        if pending is not None and line.startswith((" ", "\t")):
            positionals[pending] = (f"{positionals[pending]} {line.strip()}").strip()
        else:
            pending = None


def _parse_flags(lines: Sequence[str], flags: dict[str, _Flag]) -> None:
    """Add flag rows from one section, joining wrapped descriptions."""
    pending: _Flag | None = None
    for line in lines:
        if not line.strip():
            pending = None
            continue
        match = _FLAG.match(line)
        if match:
            flag = _parse_flag_head(match.group("head"), match.group("desc") or "")
            if flag is not None:
                key = flag.names[-1]
                if key not in flags:
                    flags[key] = flag
                    pending = flag
                else:
                    pending = None
                continue
        if pending is not None and line.startswith((" ", "\t")):
            pending.description = f"{pending.description} {line.strip()}".strip()
        else:
            pending = None


def _parse_commands(lines: Sequence[str], commands: dict[str, str]) -> None:
    """Add two-column command rows from one section."""
    for line in lines:
        if not line.strip() or line.lstrip().startswith("-"):
            continue
        match = _COMMAND.match(line)
        if match is None:
            continue
        name = match.group("name").rstrip(":")
        if name.casefold() in _META_COMMANDS or name.startswith("_"):
            continue
        commands.setdefault(name, match.group("desc").strip())


def _parse_help(
    text: str, *, is_root: bool
) -> tuple[str, str, dict[str, str], dict[str, _Flag], dict[str, str]]:
    """Extract usage, description, commands, flags, and arguments."""
    usage = _usage(text)
    description = _description(text)
    commands: dict[str, str] = {}
    flags: dict[str, _Flag] = {}
    positionals: dict[str, str] = {}
    for header, lines in _sections(text):
        kind = _classify(header)
        if kind == "commands":
            _parse_commands(lines, commands)
        elif kind == "flags":
            _parse_flags(lines, flags)
        elif kind == "arguments":
            _parse_positionals(lines, positionals)
    if not flags:
        _parse_flags(text.splitlines(), flags)
    if (
        not commands
        and is_root
        and re.search(r"<command>|\[command\]|\bsubcommand", usage, re.IGNORECASE)
    ):
        _parse_commands(text.splitlines(), commands)
    return usage, description, commands, flags, positionals
