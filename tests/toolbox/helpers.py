"""Shared fakes and helpers for the toolbox test package."""

from pathlib import Path

SERVER = Path(__file__).parents[1] / "fixtures" / "mcp_server.py"


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


async def greet(name: str = "world") -> str:
    """Greet someone."""
    return f"hello {name}"


def local_echo(text: str) -> str:
    """Echo text locally."""
    return f"local:{text}"


def verbose(value: int) -> int:
    """Return a value unchanged.

    This is a deliberately verbose description used to exercise description
    truncation. This is a deliberately verbose description used to exercise
    description truncation. This is a deliberately verbose description used to
    exercise description truncation. This is a deliberately verbose description
    used to exercise description truncation.
    """
    return value


def by_name(tools):
    return {tool.name: tool for tool in tools}


class _Closer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True
