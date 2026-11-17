"""Composition of memory, offloading, tracing, sessions, and policy.

Everything here is optional at import time: OpenTelemetry is imported lazily,
``dspy-rlm-hooks`` only when an RLM is wired, and a memory store is duck-typed.
"""

from .hooks import build_hooks, install
from .observe import NOOP, SCOPE, span, start_span
from .offload import DEFAULT_HEAD, DEFAULT_LIMIT, DEFAULT_TAIL, Offloader
from .policies import (
    DESTRUCTIVE_PATTERNS,
    FILE_TOOLS,
    NETWORK_TOOLS,
    SECRET_PATTERNS,
    SHELL_TOOLS,
    WRITE_TOOLS,
    allow_tools,
    deny_code,
    deny_patterns,
    deny_shell,
    deny_tools,
    deny_urls,
    destructive_shell,
    no_network,
    protect,
    protect_secrets,
    read_only,
    require_approval,
    safe_defaults,
    workspace,
)
from .policy import Policy, PolicyLike, as_policy, check
from .session import OUTPUT_LIMIT, Session
from .tools import wrap_tools
from .wire import Wire

__all__ = [
    "DEFAULT_HEAD",
    "DEFAULT_LIMIT",
    "DEFAULT_TAIL",
    "DESTRUCTIVE_PATTERNS",
    "FILE_TOOLS",
    "NETWORK_TOOLS",
    "NOOP",
    "OUTPUT_LIMIT",
    "SCOPE",
    "SECRET_PATTERNS",
    "SHELL_TOOLS",
    "WRITE_TOOLS",
    "Offloader",
    "Policy",
    "PolicyLike",
    "Session",
    "Wire",
    "allow_tools",
    "as_policy",
    "build_hooks",
    "check",
    "deny_code",
    "deny_patterns",
    "deny_shell",
    "deny_tools",
    "deny_urls",
    "destructive_shell",
    "install",
    "no_network",
    "protect",
    "protect_secrets",
    "read_only",
    "require_approval",
    "safe_defaults",
    "span",
    "start_span",
    "workspace",
    "wrap_tools",
]
