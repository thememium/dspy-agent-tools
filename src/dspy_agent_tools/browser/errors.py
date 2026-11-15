"""Tool error type and error payload helpers."""

from __future__ import annotations

from typing import Any


class _ToolError(RuntimeError):
    """A caller-correctable error, returned as ``status="error"``."""


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, _ToolError):
        return str(exc)
    message = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message and message != name else name


def _error(action: str, exc: BaseException, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "action": action,
        "error": _format_error(exc),
    }
    payload.update(extra)
    return payload
