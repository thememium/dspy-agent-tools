"""Lazily discover and cache one executable's help tree."""

from __future__ import annotations

import shlex
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import runner
from .help import _clean, _Flag, _is_man_page, _parse_help
from .render import _details_text
from .runner import (
    _USAGE_ERROR,
    _json_document,
    _run_argv,
    _tokens_rejected,
)

DEFAULT_MAX_HELP_CHARS = 20_000

# Discovery must never open a pager or wait for a terminal.
_HELP_ENV = {
    "PAGER": "cat",
    "MANPAGER": "cat",
    "GIT_PAGER": "cat",
    "GH_PAGER": "cat",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "100",
}


@dataclass(slots=True)
class _Node:
    name: str
    path: tuple[str, ...]
    description: str = ""
    usage: str = ""
    help: str = ""
    json_help: str = ""
    flags: dict[str, _Flag] = field(default_factory=dict)
    positionals: dict[str, str] = field(default_factory=dict)
    children: dict[str, _Node] = field(default_factory=dict)
    expanded: bool = False

    def label(self) -> str:
        return " ".join(self.path) or "(root)"


class _Discovery:
    """Lazily expand one executable's help tree on a background-free cache."""

    def __init__(
        self,
        executable: str,
        *,
        base_args: Sequence[str],
        cwd: Path | None,
        env: dict[str, str] | None,
        timeout: float,
        help_timeout: float,
        max_expands: int,
        max_depth: int,
        help_tokens: tuple[str, ...],
        json_flag: str | None,
        max_help_chars: int = DEFAULT_MAX_HELP_CHARS,
        max_output_chars: int = 0,
    ) -> None:
        self.executable = executable
        self.base_args = tuple(base_args)
        self.cwd = cwd
        self.env = dict(env or {})
        self.help_env = {**self.env, **_HELP_ENV}
        self.timeout = timeout
        self.help_timeout = help_timeout
        self.help_tokens = help_tokens
        self.json_flag = json_flag
        self.max_help_chars = max_help_chars
        self.max_output_chars = max_output_chars
        self.max_expands = max_expands
        self.max_depth = max_depth
        self._lock = threading.RLock()
        self.root = _Node("", ())
        self._nodes: dict[tuple[str, ...], _Node] = {(): self.root}

    def refresh(self) -> None:
        """Drop every cached help page and start from the root again."""
        with self._lock:
            self.root = _Node("", ())
            self._nodes = {(): self.root}

    def _help_text(self, path: tuple[str, ...]) -> tuple[str, int | None, str]:
        """Fetch help, trying ``help_tokens`` and falling back to the command.

        The configured help invocation is tried first (``--help`` by default).
        If the CLI rejects it or returns nothing, the command itself is run so
        the agent can inspect whatever it prints. JSON help is kept alongside
        the text help used for command discovery.
        """
        last: tuple[str, int | None] = ("", 1)
        json_help = ""
        rejected = False

        if self.help_tokens:
            if self.json_flag is not None:
                json_argv = [
                    self.executable,
                    *self.base_args,
                    *path,
                    *self.help_tokens,
                    self.json_flag,
                ]
                try:
                    output, _notices, _elapsed, returncode = runner.execute(
                        json_argv,
                        cwd=self.cwd,
                        timeout=self.help_timeout,
                        env=self.help_env,
                    )
                except Exception as exc:  # noqa: BLE001 - surfaced as help text
                    last = (str(exc), 1)
                    rejected = True
                else:
                    if output.strip() and not _is_man_page(output):
                        document = _json_document(output)
                        if document is not None:
                            json_help = document
                        elif not _tokens_rejected(
                            output, [*self.help_tokens, self.json_flag]
                        ):
                            return _clean(output), returncode, json_help
                    last = (output, returncode)
                    rejected = rejected or _tokens_rejected(
                        output, [*self.help_tokens, self.json_flag]
                    )

            try:
                output, _notices, _elapsed, returncode = runner.execute(
                    [self.executable, *self.base_args, *path, *self.help_tokens],
                    cwd=self.cwd,
                    timeout=self.help_timeout,
                    env=self.help_env,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as help text
                last = (str(exc), 1)
                rejected = True
            else:
                if (
                    output.strip()
                    and not _is_man_page(output)
                    and not _tokens_rejected(output, self.help_tokens)
                ):
                    return _clean(output), returncode, json_help
                last = (output, returncode)
                rejected = rejected or _tokens_rejected(output, self.help_tokens)

        if not self.help_tokens or rejected or not last[0].strip():
            try:
                output, _notices, _elapsed, returncode = runner.execute(
                    [self.executable, *self.base_args, *path],
                    cwd=self.cwd,
                    timeout=self.help_timeout,
                    env=self.help_env,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as help text
                return _clean(str(exc)), 1, json_help
            return _clean(output), returncode, json_help

        return _clean(last[0]), last[1], json_help

    def expand(self, path: tuple[str, ...]) -> _Node:
        """Fetch and parse one node's help exactly once."""
        with self._lock:
            node = self._nodes.get(path)
            if node is None:
                raise KeyError(" ".join(path))
            if node.expanded:
                return node
            text, returncode, json_help = self._help_text(path)
            node.help = text
            node.json_help = json_help
            usage, description, commands, flags, positionals = _parse_help(
                text, is_root=not path
            )
            node.usage = usage
            node.flags = flags
            node.positionals = positionals
            if not node.description and returncode == 0:
                node.description = description
            for name, child_description in commands.items():
                child_path = (*path, name)
                child = self._nodes.get(child_path)
                if child is None:
                    child = _Node(name, child_path)
                    self._nodes[child_path] = child
                if not child.description:
                    child.description = child_description
                node.children[name] = child
            node.expanded = True
            return node

    def nodes(self) -> list[_Node]:
        return [node for path, node in self._nodes.items() if path]

    @staticmethod
    def _score(node: _Node, tokens: list[str], query: str) -> float:
        path = " ".join(node.path).casefold()
        name = node.name.casefold()
        description = node.description.casefold()
        score = 0.0
        if path == query:
            score += 100
        if name == query:
            score += 60
        if all(token in path.split() for token in tokens):
            score += 50
        if path.startswith(query):
            score += 30
        for token in tokens:
            if name == token:
                score += 40
            elif name.startswith(token):
                score += 20
            elif token in name:
                score += 10
            if token in description:
                score += 5
        if score:
            score += 5 * len(node.path)
        return score

    def search(self, query: str, limit: int) -> list[_Node]:
        """Rank known commands, expanding the best candidates within budget."""
        query = query.strip().casefold()
        tokens = query.split()
        limit = max(1, min(int(limit), 50))
        with self._lock:
            self.expand(())
            if not self.root.children:
                return [self.root]
            if not tokens:
                return list(self.root.children.values())[:limit]

            expands = 1
            for _ in range(self.max_depth):
                progressed = False
                candidates = sorted(
                    self.nodes(),
                    key=lambda node: (
                        -self._score(node, tokens, query),
                        node.label(),
                    ),
                )
                for node in candidates:
                    if expands >= self.max_expands:
                        break
                    if node.expanded or self._score(node, tokens, query) <= 0:
                        continue
                    self.expand(node.path)
                    expands += 1
                    progressed = True
                if not progressed:
                    break

            ranked = sorted(
                ((self._score(node, tokens, query), node) for node in self.nodes()),
                key=lambda pair: (-pair[0], pair[1].label()),
            )
            return [node for score, node in ranked if score > 0][:limit]

    def resolve(self, command: str) -> _Node:
        """Resolve a whitespace-separated command path to its expanded node."""
        tokens = shlex.split(command) if command.strip() else []
        with self._lock:
            self.expand(())
            node = self.root
            for token in tokens:
                child = node.children.get(token)
                if child is None:
                    child = next(
                        (
                            candidate
                            for name, candidate in node.children.items()
                            if name.casefold() == token.casefold()
                        ),
                        None,
                    )
                if child is None:
                    self.expand(node.path)
                    child = node.children.get(token) or next(
                        (
                            candidate
                            for name, candidate in node.children.items()
                            if name.casefold() == token.casefold()
                        ),
                        None,
                    )
                if child is None:
                    known = ", ".join(list(node.children)[:8]) or "none"
                    raise ValueError(
                        f"Unknown command {token!r} under "
                        f"{node.label()!r}; call get_command_help first. "
                        f"Known commands: {known}."
                    )
                node = child
            if not node.expanded:
                self.expand(node.path)
            return node

    def run(self, command: str, args: Sequence[str]) -> str:
        """Resolve ``command`` and execute it with an argv tail."""
        node = self.resolve(command)
        result = _run_argv(
            self.executable,
            self.base_args,
            node.path,
            [str(item) for item in args],
            cwd=self.cwd,
            env=self.env,
            timeout=self.timeout,
            json_flag=self.json_flag,
            max_output_chars=self.max_output_chars,
        )
        if "[exit code" in result and _USAGE_ERROR.search(result):
            help_text = _details_text(node, self.max_help_chars)
            if help_text:
                result = f"{result.rstrip()}\n\n[command help]\n{help_text}"
        return result
