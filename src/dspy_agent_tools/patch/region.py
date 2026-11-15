"""The delimited region of an agent's instructions that Patch manages."""

from __future__ import annotations

from collections.abc import Sequence

#: Delimit the managed region so a later call replaces it instead of appending.
_BEGIN = "<!-- dspy-agent-tools:begin -->"
_END = "<!-- dspy-agent-tools:end -->"


def _without_region(instructions: str) -> str:
    """``instructions`` with the region this module manages removed."""
    start = instructions.find(_BEGIN)
    if start < 0:
        return instructions
    end = instructions.find(_END, start)
    tail = instructions[end + len(_END) :] if end >= 0 else ""
    return (instructions[:start] + tail).strip("\n")


def _with_region(instructions: str, blocks: Sequence[str]) -> str:
    """``instructions`` with one managed region holding ``blocks``."""
    base = _without_region(instructions)
    if not blocks:
        return base
    region = "\n\n".join([_BEGIN, *blocks, _END])
    return f"{base}\n\n{region}" if base else region
