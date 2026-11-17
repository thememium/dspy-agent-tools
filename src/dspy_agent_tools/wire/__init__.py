"""Composition of memory, offloading, tracing, sessions, and policy.

Everything here is optional at import time: OpenTelemetry is imported lazily,
``dspy-rlm-hooks`` only when an RLM is wired, and a memory store is duck-typed.
"""

from .hooks import build_hooks, install
from .observe import NOOP, SCOPE, span, start_span
from .offload import DEFAULT_HEAD, DEFAULT_LIMIT, DEFAULT_TAIL, Offloader
from .policy import Policy, check
from .session import OUTPUT_LIMIT, Session
from .tools import wrap_tools
from .wire import Wire

__all__ = [
    "DEFAULT_HEAD",
    "DEFAULT_LIMIT",
    "DEFAULT_TAIL",
    "NOOP",
    "OUTPUT_LIMIT",
    "SCOPE",
    "Offloader",
    "Policy",
    "Session",
    "Wire",
    "build_hooks",
    "check",
    "install",
    "span",
    "start_span",
    "wrap_tools",
]
