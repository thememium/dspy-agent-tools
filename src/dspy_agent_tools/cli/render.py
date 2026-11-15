"""Render discovered command nodes as text or structured records."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .discovery import _Node


def _search_text(nodes: Sequence[_Node]) -> str:
    if not nodes:
        return (
            "[no matching commands; call get_command_help(command='') for the "
            "root command list]"
        )
    lines = []
    for index, node in enumerate(nodes, 1):
        label = node.label()
        line = (
            f"{index}. {label}: {node.description}"
            if node.description
            else f"{index}. {label}"
        )
        lines.append(line)
    return "\n".join(lines)


def _search_records(nodes: Sequence[_Node]) -> list[dict[str, Any]]:
    return [
        {
            "command": node.label(),
            "description": node.description,
        }
        for node in nodes
    ]


def _details_text(node: _Node, max_help_chars: int) -> str:
    parts = []
    label = node.label()
    parts.append(f"{label}: {node.description}" if node.description else label)
    if node.usage:
        parts.append(
            "Usage:\n" + "\n".join("  " + line for line in node.usage.splitlines())
        )
    if node.positionals:
        parts.append(
            "Arguments:\n"
            + "\n".join(
                f"  {name} — {description}" if description else f"  {name}"
                for name, description in node.positionals.items()
            )
        )
    if node.children:
        parts.append(
            "Subcommands:\n"
            + "\n".join(
                f"  {name} — {child.description}" if child.description else f"  {name}"
                for name, child in node.children.items()
            )
        )
    if node.flags:
        parts.append(
            "Flags:\n" + "\n".join(flag.line() for flag in node.flags.values())
        )
    if node.json_help and max_help_chars:
        data = node.json_help
        if len(data) > max_help_chars:
            data = data[:max_help_chars].rstrip() + "\n[help truncated]"
        parts.append("JSON help:\n" + data.rstrip())
    structured = bool(node.flags or node.children or node.json_help)
    if node.help and max_help_chars and not structured:
        raw = node.help
        if len(raw) > max_help_chars:
            raw = raw[:max_help_chars].rstrip() + "\n[help truncated]"
        parts.append("Raw help:\n" + raw.rstrip())
    return "\n\n".join(parts)


def _details_record(node: _Node, max_help_chars: int) -> dict[str, Any]:
    raw = ""
    if node.help and max_help_chars:
        raw = node.help
        if len(raw) > max_help_chars:
            raw = raw[:max_help_chars].rstrip() + "\n[help truncated]"
    json_help = ""
    if node.json_help and max_help_chars:
        json_help = node.json_help
        if len(json_help) > max_help_chars:
            json_help = json_help[:max_help_chars].rstrip() + "\n[help truncated]"
    return {
        "command": node.label(),
        "description": node.description,
        "usage": node.usage,
        "arguments": dict(node.positionals),
        "subcommands": {
            name: child.description for name, child in node.children.items()
        },
        "flags": {name: flag.record() for name, flag in node.flags.items()},
        "help": raw,
        "json_help": json_help,
    }
