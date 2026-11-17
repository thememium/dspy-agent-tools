"""Allow-or-deny gate applied before a tool call or generated code runs.

A policy is a plain callable, so an application can consult a denylist, a
config file, an approval prompt, or another model. Returning a string denies
the action and becomes the error text; returning ``None`` allows it. The
policy is synchronous: it runs in the tool-call path, not on an event loop.
"""

from __future__ import annotations

from typing import Any, Protocol


class Policy(Protocol):
    """Decide whether one action may run.

    ``name`` is the tool name, or ``"code"`` for RLM-generated Python.
    ``arguments`` holds the call's keyword arguments, or ``{"code": ...,
    "iteration": ...}`` for generated code.
    """

    def __call__(self, name: str, arguments: dict[str, Any]) -> str | None: ...


def check(policy: Policy | None, name: str, arguments: dict[str, Any]) -> None:
    """Raise :class:`PermissionError` when ``policy`` denies the action."""
    if policy is None:
        return
    reason = policy(name, arguments)
    if reason:
        raise PermissionError(str(reason))


__all__ = ["Policy", "check"]
