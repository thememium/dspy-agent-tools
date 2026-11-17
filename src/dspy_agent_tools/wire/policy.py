"""Allow-or-deny decisions, composable and callable.

A policy receives the tool name and keyword arguments and returns ``None`` to
allow or a reason string to deny. Plain functions work; :class:`Policy` adds
composition so several rules read as one:

    policy = workspace(".") & destructive_shell() & protect_secrets()

``&`` is conjunction: the first denial wins and every rule can speak for
itself. A list of policies is accepted anywhere a policy is, and is combined
the same way.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol, cast

#: ``None`` allows the call; a string denies it and becomes the error text.
Decision = str | None


class PolicyLike(Protocol):
    """Anything callable as ``(name, arguments) -> Decision``."""

    def __call__(self, name: str, arguments: dict[str, Any]) -> Decision: ...


class Policy:
    """A named decision with conjunction.

    Args:
        decide: The callable that decides one call.
        name: Label used by ``repr`` and combined policies.

    Example:
        >>> from dspy_agent_tools import Policy, deny_tools
        >>> read_only = Policy(lambda name, args: None, name="allow")
        >>> (read_only & deny_tools("write_file"))("write_file", {})
        'write_file is denied by policy'
    """

    __slots__ = ("decide", "name")

    def __init__(
        self, decide: Callable[[str, dict[str, Any]], Decision], *, name: str = ""
    ) -> None:
        if not callable(decide):
            raise TypeError(f"Policy() needs a callable, got {type(decide).__name__}")
        self.decide = decide
        self.name = name or getattr(decide, "__name__", "policy")

    def __call__(self, name: str, arguments: dict[str, Any]) -> Decision:
        return self.decide(name, arguments)

    def __and__(self, other: PolicyLike | Sequence[PolicyLike]) -> Policy:
        return Policy.all(self, other)

    def __rand__(self, other: PolicyLike | Sequence[PolicyLike]) -> Policy:
        return Policy.all(other, self)

    @classmethod
    def all(cls, *policies: PolicyLike | Sequence[PolicyLike] | None) -> Policy:
        """Combine policies with conjunction; ``None`` entries are skipped."""
        items: list[Policy] = []
        for candidate in policies:
            item = as_policy(candidate)
            if item is not None:
                items.append(item)
        if not items:
            return cls(lambda name, arguments: None, name="allow")
        if len(items) == 1:
            return items[0]

        def decide(name: str, arguments: dict[str, Any]) -> Decision:
            for item in items:
                reason = item(name, arguments)
                if reason:
                    return reason
            return None

        return cls(decide, name=" & ".join(item.name for item in items))

    def __repr__(self) -> str:
        return f"Policy({self.name!r})"


def as_policy(
    policy: PolicyLike | Sequence[PolicyLike] | None,
) -> Policy | None:
    """Coerce a callable, policy, list, or ``None`` into a :class:`Policy`."""
    if policy is None:
        return None
    if isinstance(policy, Policy):
        return policy
    if isinstance(policy, (list, tuple)):
        return Policy.all(*policy)
    if callable(policy):
        return Policy(cast(Callable[[str, dict[str, Any]], Decision], policy))
    raise TypeError(
        "policy must be a callable, a Policy, a list of them, or None; got "
        f"{type(policy).__name__}"
    )


def check(
    policy: PolicyLike | Sequence[PolicyLike] | None,
    name: str,
    arguments: dict[str, Any],
) -> None:
    """Raise :class:`PermissionError` when ``policy`` denies the action."""
    current = as_policy(policy)
    if current is None:
        return
    reason = current(name, arguments)
    if reason:
        raise PermissionError(str(reason))


__all__ = ["Decision", "Policy", "PolicyLike", "as_policy", "check"]
