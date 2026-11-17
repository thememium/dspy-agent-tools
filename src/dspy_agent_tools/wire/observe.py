"""OpenTelemetry spans for tool calls and RLM iterations.

The package depends on ``opentelemetry-api`` only through the optional
``[otel]`` extra. Without it every helper here is a no-op, so importing and
using :class:`~dspy_agent_tools.Wire` never requires a tracer.

Any backend that consumes OTLP works without a package-specific adapter:
configure an exporter and SDK tracer provider in the application, and the
spans emitted here arrive with the rest of the trace.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any

#: Instrumentation scope name reported on every span.
SCOPE = "dspy_agent_tools"


class _NoopSpan:
    """Stand-in that accepts the calls the instrumentation makes."""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def end(self) -> None:
        pass


_NOOP = _NoopSpan()

#: Shared no-op span, for a caller that must always have a span object.
NOOP = _NOOP


def _trace() -> Any:
    """The OpenTelemetry trace API, or ``None`` when it is not installed."""
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - exercised with a patched module
        return None
    return trace


def _start(name: str, attributes: Mapping[str, Any] | None) -> Any:
    trace = _trace()
    if trace is None:
        return _NOOP
    current = trace.get_tracer(SCOPE).start_span(name)
    for key, value in (attributes or {}).items():
        current.set_attribute(key, value)
    return current


@contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None) -> Generator[Any]:
    """Open a span for the duration of the block, recording any exception."""
    current = _start(name, attributes)
    try:
        yield current
    except BaseException as exc:
        current.record_exception(exc)
        raise
    finally:
        current.end()


def start_span(name: str, attributes: Mapping[str, Any] | None = None) -> Any:
    """Open a span whose lifetime spans two callbacks.

    Used for hooks that run before and after one unit of work: the returned
    object is ended by the pairing hook. Without OpenTelemetry this returns a
    shared no-op, so callers never branch on availability.
    """
    return _start(name, attributes)


__all__ = ["NOOP", "SCOPE", "span", "start_span"]
