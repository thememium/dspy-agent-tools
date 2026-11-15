"""Execute CLI argv tails and shape their output for the model."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from plumbum import CommandNotFound

from ..utils.process import execute, render

_FLAG_UNSUPPORTED = re.compile(
    r"(?:unknown|unrecognized|invalid|unexpected|not defined|no such)"
    r"[^\n]*\b(?:flag|option|argument|switch|command)\b"
    r"|\bflag provided but not defined\b"
    r"|\bwas(?:n't| not) expected\b",
    re.IGNORECASE,
)
_USAGE_ERROR = re.compile(
    r"(?:unexpected extra argument|unknown (?:flag|option)|"
    r"unrecognized (?:flag|option)|requires an argument|missing argument|"
    r"invalid value|usageerror|^usage:)",
    re.IGNORECASE | re.MULTILINE,
)


def _json_document(text: str) -> str | None:
    """Return ``text`` when it is a complete JSON document, else ``None``."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        json.loads(stripped)
    except ValueError:
        return None
    return stripped


def _has_flag(argv: Sequence[str], flag: str) -> bool:
    """Return whether an argv list already carries ``flag`` (or ``flag=...``)."""
    return any(item == flag or item.startswith(f"{flag}=") for item in argv)


def _tokens_rejected(output: str, tokens: Sequence[str]) -> bool:
    """Return whether any token in ``tokens`` was rejected by the CLI."""
    return any(_flag_was_rejected(output, token) for token in tokens if token)


def _flag_was_rejected(output: str, flag: str) -> bool:
    """Recognize a CLI rejecting an unknown flag, e.g. ``gh --json``.

    Git renders the rejected option without dashes (``unknown option `json'``),
    so the bare name counts when it is long enough to be unambiguous.
    """
    lowered = output.casefold()
    bare = flag.lstrip("-").split("=", 1)[0].casefold()
    mentioned = flag.casefold() in lowered or (len(bare) >= 3 and bare in lowered)
    return mentioned and bool(_FLAG_UNSUPPORTED.search(output))


def _argv_hint(output: str, argv: Sequence[str], returncode: int | None) -> str:
    """Show the exact argv after a usage error so the agent can fix one item."""
    if returncode == 0 or not _USAGE_ERROR.search(output):
        return output
    rendered = " ".join(shlex.quote(item) for item in argv)
    return (
        f"{output.rstrip()}\n"
        f"[argv: {rendered}]\n"
        "[args is an argv list: one item per argument; a multi-word query is "
        "one item, and do not repeat the command path in args.]"
    )


def _truncate_output(text: str, limit: int) -> str:
    """Cap one command result and say how much was omitted."""
    if limit <= 0 or len(text) <= limit:
        return text
    marker = (
        f"\n\n[output truncated: set max_output_chars=0 for the full {len(text)} chars]"
    )
    keep = max(0, limit - len(marker))
    return text[:keep].rstrip() + marker


def _run_argv(
    executable: str,
    base_args: Sequence[str],
    path: Sequence[str],
    args: Sequence[str],
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    timeout: float,
    ok_codes: frozenset[int] = frozenset(),
    json_flag: str | None = None,
    max_output_chars: int = 0,
) -> str:
    """Execute one argv tail, trying ``json_flag`` first and falling back.

    The JSON attempt is retried without the flag only when the CLI explicitly
    rejects it as an unknown flag; real command failures are returned as-is.
    """
    argv = [executable, *base_args, *path, *args]
    try:
        if json_flag is not None and not _has_flag(argv, json_flag):
            json_argv = [executable, *base_args, *path, json_flag, *args]
            output, notices, elapsed, returncode = execute(
                json_argv, cwd=cwd, timeout=timeout, env=env, ok_codes=ok_codes
            )
            if returncode == 0 or not _flag_was_rejected(output, json_flag):
                output = _argv_hint(output, json_argv, returncode)
                return render(
                    _truncate_output(output, max_output_chars), notices, elapsed
                )
        output, notices, elapsed, returncode = execute(
            argv, cwd=cwd, timeout=timeout, env=env, ok_codes=ok_codes
        )
    except CommandNotFound:
        return f"[command not found: {executable}]"
    output = _argv_hint(output, argv, returncode)
    return render(_truncate_output(output, max_output_chars), notices, elapsed)
