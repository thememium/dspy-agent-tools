"""DSPy integration shared by every tool family.

DSPy is an optional dependency for the bare package, so ``dspy.Tool`` is
imported on first use. Tool catalogs that must call async tools from the
synchronous path share one cached subclass instead of mutating global DSPy
settings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from dspy import Tool


def load_tool(message: str) -> type[Tool]:
    """Import ``dspy.Tool`` on first use, or raise ``message``."""
    try:
        from dspy import Tool
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(message) from exc
    return Tool


_SYNC_TOOL_CLASSES: dict[bool, Any] = {}


def sync_tool_class(*, drop_nulls: bool = False) -> type[Tool]:
    """Build once a ``Tool`` whose sync path converts async results.

    ``dspy.Tool.__call__`` refuses to run an async function unless
    ``allow_tool_async_sync_conversion`` is set. Enabling it inside the tool
    call keeps ``agent(...)`` working without mutating global DSPy settings,
    and leaves ``acall`` fully asynchronous.

    ``drop_nulls`` treats ``None`` as "not supplied", which MCP input schemas
    require; local Python tools keep explicit ``None`` values.
    """
    import dspy
    from dspy import Tool as DspyTool

    cached = _SYNC_TOOL_CLASSES.get(drop_nulls)
    if cached is not None:
        return cast(type[DspyTool], cached)

    class _SyncCapableTool(DspyTool):
        def __call__(self, **kwargs: Any) -> Any:
            with dspy.context(allow_tool_async_sync_conversion=True):
                return super().__call__(**self._prepare(kwargs))

        async def acall(self, **kwargs: Any) -> Any:
            return await super().acall(**self._prepare(kwargs))

        @staticmethod
        def _prepare(kwargs: dict[str, Any]) -> dict[str, Any]:
            if not drop_nulls:
                return kwargs
            return {name: value for name, value in kwargs.items() if value is not None}

    _SYNC_TOOL_CLASSES[drop_nulls] = _SyncCapableTool
    return _SyncCapableTool
