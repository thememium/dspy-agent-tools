"""Ready-made policies for the tools this package ships.

Each factory returns a :class:`~dspy_agent_tools.Policy`, so they compose with
``&`` and can be passed directly to ``Wire(..., policy=...)`` or
``wrap_tools(..., policy=...)``.

The defaults name the tools they know about, but every factory takes ``tools=``
so the same rule covers a renamed, prefixed, or third-party tool:

    policy = deny_shell(tools=("bash_exec", "terminal"))
    policy = workspace("src", tools=("read_file", "write_file", "my_reader"))

Limits worth stating plainly:

- ``bash_exec`` can do anything the process can do. A regex denylist is a
  speed bump, not a sandbox; use an OS sandbox when the model is untrusted.
- Path policies inspect tool arguments, not what a shell command does with
  them. ``workspace`` covers the file tools; a shell can still escape it.
- Policies run synchronously in the call path. Keep them fast and offline.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .policy import Decision, Policy

#: File tools that take a ``path`` argument.
FILE_TOOLS = frozenset({"read_file", "write_file", "edit_file", "delete_file"})
#: File tools that modify the filesystem.
WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})
#: Tools that run an arbitrary shell command.
SHELL_TOOLS = frozenset({"bash_exec"})
#: Tools that fetch from the network.
NETWORK_TOOLS = frozenset({"bash_curl", "bash_fetch", "web_search", "web_news"})

#: Commands that are destructive regardless of the working directory.
DESTRUCTIVE_PATTERNS = (
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\b",
    r"\bsudo\b",
    r"\bdoas\b",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^\n]*\bof=",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r":\(\)\s*\{.*\};\s*:",
    r"\bch(attr|mod)\b[^\n]*\b777\b",
    r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b",
    r">\s*/dev/(sd|disk)",
    r"\bgit\s+push\b[^\n]*--force\b",
)

#: Files that usually hold credentials.
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".pypirc",
    ".npmrc",
    ".ssh/*",
    ".aws/*",
    "**/secrets/**",
    "**/credentials*",
)

_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")


def _names(tools: Iterable[str] | str | None) -> tuple[str, ...] | None:
    if tools is None:
        return None
    if isinstance(tools, str):
        return (tools,)
    return tuple(tools)


def _applies(name: str, tools: Iterable[str] | str | None) -> bool:
    """Whether a rule naming ``tools`` applies to ``name`` (fnmatch patterns)."""
    patterns = _names(tools)
    if patterns is None:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _values(arguments: dict[str, Any], keys: Sequence[str] | None = None) -> list[str]:
    """String values under ``keys``, or every string value when ``keys`` is None."""
    if keys is None:
        return [value for value in arguments.values() if isinstance(value, str)]
    found: list[str] = []
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend(item for item in value if isinstance(item, str))
    return found


def _paths(arguments: dict[str, Any]) -> list[str]:
    return _values(arguments, ("path", "cwd"))


def _urls(arguments: dict[str, Any]) -> list[str]:
    found = _values(arguments, ("url", "href"))
    for text in _values(arguments):
        found.extend(_URL.findall(text))
    return list(dict.fromkeys(found))


def deny_tools(*names: str, reason: str | None = None) -> Policy:
    """Deny the named tools. Names are fnmatch patterns.

    ``deny_tools("browser_*")`` blocks the whole browser family; with no names
    it denies every call.
    """

    def decide(name: str, arguments: dict[str, Any]) -> Decision:
        if names and not _applies(name, names):
            return None
        return reason or f"{name} is denied by policy"

    return Policy(decide, name="deny_tools")


def allow_tools(*names: str, reason: str | None = None) -> Policy:
    """Deny every tool not named here. Names are fnmatch patterns.

    For an RLM, include ``"code"`` as well or every generated block is denied.
    """

    def decide(name: str, arguments: dict[str, Any]) -> Decision:
        if _applies(name, names):
            return None
        return reason or f"{name} is not in the allowed tool list"

    return Policy(decide, name="allow_tools")


def deny_patterns(
    *patterns: str,
    keys: Sequence[str] | None = None,
    tools: Iterable[str] | str | None = None,
    reason: str | None = None,
) -> Policy:
    """Deny a call when any targeted string argument matches a regex.

    ``keys`` selects argument names; ``None`` scans every string argument. With
    no patterns the targeted calls are denied outright. Matching is
    case-insensitive, and a caller-supplied reason replaces the default.
    """
    compiled = [(pattern, re.compile(pattern, re.IGNORECASE)) for pattern in patterns]

    def decide(name: str, arguments: dict[str, Any]) -> Decision:
        if not _applies(name, tools):
            return None
        if not compiled:
            return reason or f"{name} is denied by policy"
        for text in _values(arguments, keys):
            for source, regex in compiled:
                if regex.search(text):
                    return reason or f"argument matches denied pattern {source!r}"
        return None

    return Policy(decide, name="deny_patterns")


def deny_shell(
    *patterns: str,
    tools: Iterable[str] | str | None = SHELL_TOOLS,
    reason: str | None = None,
) -> Policy:
    """Deny shell commands matching a regex; with no patterns, deny all shell."""
    return deny_patterns(*patterns, keys=("command",), tools=tools, reason=reason)


def destructive_shell(
    *extra: str,
    tools: Iterable[str] | str | None = SHELL_TOOLS,
    reason: str | None = None,
) -> Policy:
    """Deny the destructive-command patterns in :data:`DESTRUCTIVE_PATTERNS`."""
    return deny_shell(*DESTRUCTIVE_PATTERNS, *extra, tools=tools, reason=reason)


def deny_code(*patterns: str, reason: str | None = None) -> Policy:
    """Deny RLM-generated Python matching a regex; with no patterns, deny all code."""
    return deny_patterns(*patterns, keys=("code",), tools=("code",), reason=reason)


def workspace(
    root: str | Path = ".",
    *,
    tools: Iterable[str] | str | None = FILE_TOOLS,
    reason: str | None = None,
) -> Policy:
    """Confine file paths to ``root``.

    Relative paths resolve against ``root``; absolute paths and symlinks are
    resolved before the check, so ``../`` and a symlink out of the tree are
    both denied. Apply it to ``bash_exec`` as well to check its ``cwd``, but
    that does not stop a command from changing directory.
    """
    base = Path(root).expanduser().resolve()

    def decide(name: str, arguments: dict[str, Any]) -> Decision:
        if not _applies(name, tools):
            return None
        for raw in _paths(arguments):
            try:
                candidate = Path(raw).expanduser()
                resolved = (
                    candidate.resolve()
                    if candidate.is_absolute()
                    else (base / candidate).resolve()
                )
            except (OSError, RuntimeError, ValueError):
                return reason or f"{raw!r} cannot be resolved inside {base}"
            if not resolved.is_relative_to(base):
                return reason or f"{raw!r} is outside workspace {base}"
        return None

    return Policy(decide, name=f"workspace({base})")


def protect(
    *patterns: str,
    tools: Iterable[str] | str | None = FILE_TOOLS,
    reason: str | None = None,
) -> Policy:
    """Deny file tools touching a path that matches any fnmatch pattern.

    Patterns match the whole path, the basename, or a shell-style suffix, so
    ``.env``, ``*.pem``, ``.ssh/*``, and ``**/secrets/**`` all work.
    """
    patterns = tuple(patterns)

    def decide(name: str, arguments: dict[str, Any]) -> Decision:
        if not _applies(name, tools):
            return None
        for raw in _paths(arguments):
            path = Path(raw)
            for pattern in patterns:
                if path.match(pattern) or fnmatch.fnmatchcase(path.name, pattern):
                    return reason or f"{raw!r} is protected by {pattern!r}"
                if fnmatch.fnmatchcase(str(path), pattern):
                    return reason or f"{raw!r} is protected by {pattern!r}"
        return None

    return Policy(decide, name="protect")


def protect_secrets(
    *extra: str,
    tools: Iterable[str] | str | None = FILE_TOOLS,
    reason: str | None = None,
) -> Policy:
    """Deny file tools touching the credential patterns in :data:`SECRET_PATTERNS`."""
    return protect(*SECRET_PATTERNS, *extra, tools=tools, reason=reason)


def deny_urls(
    *hosts: str,
    schemes: Iterable[str] = (),
    tools: Iterable[str] | str | None = None,
    reason: str | None = None,
) -> Policy:
    """Deny URLs whose host is listed or whose scheme is listed.

    A host matches itself and its subdomains (``example.com`` also blocks
    ``api.example.com``). Every string argument is scanned for URLs, so the
    rule also covers a URL placed in a shell command or a browser action.
    Malformed URLs are denied rather than guessed at.
    """
    blocked_hosts = tuple(host.lower().lstrip(".") for host in hosts)
    blocked_schemes = tuple(scheme.lower() for scheme in schemes)
    if not blocked_hosts and not blocked_schemes:
        raise ValueError("deny_urls() needs at least one host or scheme")

    def decide(name: str, arguments: dict[str, Any]) -> Decision:
        if not _applies(name, tools):
            return None
        for url in _urls(arguments):
            try:
                parts = urlsplit(url)
            except ValueError:
                return reason or f"{url} is not a parseable URL"
            host = (parts.hostname or "").lower()
            scheme = (parts.scheme or "").lower()
            if scheme in blocked_schemes:
                return reason or f"{url} uses denied scheme {scheme!r}"
            if any(
                host == blocked or host.endswith(f".{blocked}")
                for blocked in blocked_hosts
            ):
                return reason or f"{url} targets denied host {host!r}"
        return None

    return Policy(decide, name="deny_urls")


def require_approval(
    *tools: str,
    ask: Callable[[str, dict[str, Any]], bool | str],
    reason: str = "approval denied",
) -> Policy:
    """Ask before running the named tools (all calls when none are named).

    ``ask`` returns ``True`` to allow, a non-empty string to deny with that
    text, or anything else to deny with ``reason``. It runs synchronously.
    """

    def decide(name: str, arguments: dict[str, Any]) -> Decision:
        if tools and not _applies(name, tools):
            return None
        answer = ask(name, arguments)
        if answer is True:
            return None
        if isinstance(answer, str) and answer.strip():
            return answer
        return reason

    return Policy(decide, name="require_approval")


def read_only(
    *,
    write_tools: Iterable[str] | str | None = WRITE_TOOLS,
    shell: Iterable[str] | str | None = SHELL_TOOLS,
    reason: str | None = None,
) -> Policy:
    """Deny every mutating action: write tools, shell, and generated code.

    A read-only RLM cannot work, because generated code is denied; use this for
    inspecting agents, or compose the individual rules you want instead.
    """
    rules = [deny_shell(tools=shell, reason=reason), deny_code(reason=reason)]
    if write_tools:
        rules.insert(0, deny_tools(*(_names(write_tools) or ()), reason=reason))
    return Policy.all(*rules)


def no_network(*, reason: str | None = None) -> Policy:
    """Deny the network tools and the browser family."""
    return deny_tools(
        *NETWORK_TOOLS,
        "browser",
        "browser_*",
        reason=reason or "network access is denied by policy",
    )


def safe_defaults(root: str | Path = ".") -> Policy:
    """A sane starting policy: confine files, protect secrets, deny destructive shell."""
    return workspace(root) & protect_secrets() & destructive_shell()


__all__ = [
    "DESTRUCTIVE_PATTERNS",
    "FILE_TOOLS",
    "NETWORK_TOOLS",
    "SECRET_PATTERNS",
    "SHELL_TOOLS",
    "WRITE_TOOLS",
    "allow_tools",
    "deny_code",
    "deny_patterns",
    "deny_shell",
    "deny_tools",
    "deny_urls",
    "destructive_shell",
    "no_network",
    "protect",
    "protect_secrets",
    "read_only",
    "require_approval",
    "safe_defaults",
    "workspace",
]
