"""Spill oversized results to disk and return a retrievable handle.

Shell output and file reads are returned in full by design elsewhere in this
package. That is correct for a single call and wrong for a long-running agent:
one build log can consume the whole context window. An offloader writes the
full value to a file and replaces it with a bounded envelope holding the path,
sizes, and head/tail samples, so the model can page the file with
:func:`~dspy_agent_tools.read_file` or ``bash_exec``.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session import Session

#: Value length above which the value is spilled.
DEFAULT_LIMIT = 20_000
#: Characters kept from the start of a spilled value.
DEFAULT_HEAD = 2_000
#: Characters kept from the end of a spilled value.
DEFAULT_TAIL = 1_000

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _text(value: Any) -> str | None:
    """The value as text, or ``None`` when it has no faithful text form."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        return repr(value)


class Offloader:
    """Replace oversized values with a handle to a file that holds them.

    Args:
        directory: Where spilled values are written. Defaults to
            ``<tmp>/dspy-agent-tools/offload``. The name is derived from the
            content, so the same value always lands in the same file.
        limit: Character count above which a value is spilled.
        head: Characters kept from the start of the spilled value. Defaults to
            :data:`DEFAULT_HEAD`, capped at half of ``limit``.
        tail: Characters kept from the end of the spilled value. Defaults to
            :data:`DEFAULT_TAIL`, capped at half of ``limit``.
        session: Optional :class:`~dspy_agent_tools.Session` that records
            every spilled file as an artifact.

    Example:
        >>> from dspy_agent_tools import Offloader
        >>> offload = Offloader(limit=1000)
        >>> offload.spill("x" * 5000)["path"]
        '/tmp/dspy-agent-tools/offload/result-....txt'
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
        head: int | None = None,
        tail: int | None = None,
        session: Session | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError(f"limit must be positive, got {limit}")
        head = min(DEFAULT_HEAD, limit // 2) if head is None else head
        tail = min(DEFAULT_TAIL, limit // 2) if tail is None else tail
        for name, value in (("head", head), ("tail", tail)):
            if value < 1:
                raise ValueError(f"{name} must be positive, got {value}")
        if limit < head + tail:
            raise ValueError(
                f"limit ({limit}) must be at least head + tail ({head + tail})"
            )
        self.directory = (
            Path(directory)
            if directory is not None
            else Path(tempfile.gettempdir()) / "dspy-agent-tools" / "offload"
        )
        self.limit = limit
        self.head = head
        self.tail = tail
        self.session = session

    def spill(self, value: Any, *, label: str = "result") -> Any:
        """Return ``value`` unchanged when small, or a handle when it is not.

        A value that cannot become text (bytes) and a value that already is a
        handle pass through unchanged.
        """
        if isinstance(value, dict) and value.get("offloaded") is True:
            return value
        text = _text(value)
        if text is None or len(text) <= self.limit:
            return value

        path = self._write(text, label)
        size = len(text.encode())
        if self.session is not None:
            self.session.artifact(str(path), size=size, note=label)
        return {
            "offloaded": True,
            "path": str(path),
            "chars": len(text),
            "bytes": size,
            "head": text[: self.head],
            "tail": text[-self.tail :],
            "note": (
                f"{len(text)} characters were saved to {path}. Read that file "
                "or narrow the command that produced it."
            ),
        }

    def _write(self, text: str, label: str) -> Path:
        """Write ``text`` under a content-addressed name and return the path."""
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = _UNSAFE.sub("-", label).strip("-.") or "result"
        digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        path = self.directory / f"{stem[:40]}-{digest}.txt"
        path.write_text(text)
        return path


__all__ = [
    "DEFAULT_HEAD",
    "DEFAULT_LIMIT",
    "DEFAULT_TAIL",
    "Offloader",
]
