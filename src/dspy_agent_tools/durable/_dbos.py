"""Lazy access to the optional DBOS dependency."""

from __future__ import annotations

from types import ModuleType

_INSTALL_HINT = (
    "Durable execution requires DBOS. Install it with "
    '`uv add "dspy-agent-tools[dbos]"` (or `uv add dbos>=3.6`).'
)


def load_dbos() -> ModuleType:
    """Import :mod:`dbos`, or raise an error naming the extra to install."""
    try:
        import dbos
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(_INSTALL_HINT) from exc
    return dbos
