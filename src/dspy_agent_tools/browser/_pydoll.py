"""Lazy pydoll import shared by every browser submodule."""

from __future__ import annotations

from typing import TYPE_CHECKING

#: Names bound at runtime by :func:`require`.
__all__ = [
    "BrowserCommands",
    "BrowserException",
    "CallArgument",
    "Chrome",
    "ChromiumOptions",
    "ConnectionException",
    "Key",
    "NetworkEvent",
    "PageCommands",
    "PageEvent",
    "RuntimeEvent",
    "ScreencastFormat",
    "ScrollPosition",
    "SyncError",
    "TargetCrashed",
    "TimeoutException",
]

if TYPE_CHECKING:
    # Declared here so type checkers resolve the names require() binds.
    from pydoll.commands import BrowserCommands, PageCommands
    from pydoll.constants import ScrollPosition
    from pydoll.exceptions import (
        BrowserException,
        ConnectionException,
        TargetCrashed,
        TimeoutException,
    )
    from pydoll.protocol.network.events import NetworkEvent
    from pydoll.protocol.page.types import ScreencastFormat
    from pydoll.protocol.runtime.events import RuntimeEvent
    from pydoll.protocol.runtime.types import CallArgument
    from pydoll.sync import Chrome, ChromiumOptions, Key, PageEvent, SyncError

_IMPORT_HINT = (
    "dspy_agent_tools browser tools require pydoll. "
    "Install the browser extra: uv add 'dspy-agent-tools[browser]'"
)
_loaded = False


def require() -> None:
    """Import pydoll on first use so the package works without the extra."""
    global _loaded
    if _loaded:
        return
    try:
        from pydoll.commands import (
            BrowserCommands as _BrowserCommands,
        )
        from pydoll.commands import (
            PageCommands as _PageCommands,
        )
        from pydoll.constants import ScrollPosition as _ScrollPosition
        from pydoll.exceptions import (
            BrowserException as _BrowserException,
        )
        from pydoll.exceptions import (
            ConnectionException as _ConnectionException,
        )
        from pydoll.exceptions import (
            TargetCrashed as _TargetCrashed,
        )
        from pydoll.exceptions import (
            TimeoutException as _TimeoutException,
        )
        from pydoll.protocol.network.events import NetworkEvent as _NetworkEvent
        from pydoll.protocol.page.types import ScreencastFormat as _ScreencastFormat
        from pydoll.protocol.runtime.events import RuntimeEvent as _RuntimeEvent
        from pydoll.protocol.runtime.types import CallArgument as _CallArgument
        from pydoll.sync import (
            Chrome as _Chrome,
        )
        from pydoll.sync import (
            ChromiumOptions as _ChromiumOptions,
        )
        from pydoll.sync import (
            Key as _Key,
        )
        from pydoll.sync import (
            PageEvent as _PageEvent,
        )
        from pydoll.sync import (
            SyncError as _SyncError,
        )
    except ImportError as exc:
        raise ImportError(_IMPORT_HINT) from exc
    for name, value in {
        "BrowserCommands": _BrowserCommands,
        "PageCommands": _PageCommands,
        "ScrollPosition": _ScrollPosition,
        "BrowserException": _BrowserException,
        "ConnectionException": _ConnectionException,
        "TargetCrashed": _TargetCrashed,
        "ScreencastFormat": _ScreencastFormat,
        "CallArgument": _CallArgument,
        "Chrome": _Chrome,
        "ChromiumOptions": _ChromiumOptions,
        "Key": _Key,
        "NetworkEvent": _NetworkEvent,
        "PageEvent": _PageEvent,
        "RuntimeEvent": _RuntimeEvent,
        "SyncError": _SyncError,
        "TimeoutException": _TimeoutException,
    }.items():
        globals().setdefault(name, value)
    _loaded = True
