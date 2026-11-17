"""Append-only session and artifact log in SQLite.

DSPy agents keep their state in memory, and an offload handle points at a file
nothing tracks. A :class:`Session` records what the agent actually did —
iterations, generated code, results, tool calls, spilled artifacts — so the
run can be inspected afterwards or handed to another process.

It is a log, not a resumable checkpoint: DSPy exposes no way to re-seed an
RLM's interpreter history, and pretending otherwise would be a lie. What is
stored is everything needed to audit or replay the decisions by hand.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Self

#: Characters of any one recorded value kept in the log.
OUTPUT_LIMIT = 4_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    kind TEXT NOT NULL,
    iteration INTEGER,
    code TEXT,
    output TEXT,
    error TEXT,
    data TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cap(value: Any, limit: int = OUTPUT_LIMIT) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text)} chars total)"


class Session:
    """A named, append-only record of one agent run.

    Args:
        path: SQLite database path. The default keeps the session in memory;
            pass a file to keep it between processes.
        key: Session identifier. Defaults to a fresh hexadecimal id, so several
            agents can share one database without colliding.

    Example:
        >>> from dspy_agent_tools import Session
        >>> session = Session("runs.db", key="run-42")
        >>> session.record(kind="tool", output="ls output", data={"tool": "bash_ls"})
        >>> [event["kind"] for event in session.events()]
        ['tool']
    """

    def __init__(self, path: str = ":memory:", *, key: str | None = None) -> None:
        self.path = path
        self.key = key or uuid.uuid4().hex
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def record(
        self,
        *,
        kind: str = "iteration",
        iteration: int | None = None,
        code: str | None = None,
        output: Any = None,
        error: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append one event. ``output`` is capped at :data:`OUTPUT_LIMIT`."""
        with self._lock:
            self._db.execute(
                "INSERT INTO events"
                " (session, kind, iteration, code, output, error, data, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.key,
                    kind,
                    iteration,
                    _cap(code),
                    _cap(output),
                    _cap(error),
                    json.dumps(data, default=repr) if data else None,
                    _now(),
                ),
            )
            self._db.commit()

    def artifact(self, path: str, *, size: int = 0, note: str = "") -> None:
        """Record a file the run produced or spilled."""
        with self._lock:
            self._db.execute(
                "INSERT INTO artifacts (session, path, bytes, note, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.key, path, size, note, _now()),
            )
            self._db.commit()

    def events(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Events for this session, oldest first."""
        query = (
            "SELECT id, kind, iteration, code, output, error, data, created_at"
            " FROM events WHERE session = ? ORDER BY id"
        )
        parameters: tuple[Any, ...] = (self.key,)
        if limit is not None:
            query += " DESC LIMIT ?"
            parameters += (limit,)
        with self._lock:
            rows = [dict(row) for row in self._db.execute(query, parameters).fetchall()]
        for row in rows:
            row["data"] = json.loads(row["data"]) if row["data"] else None
        return rows[::-1] if limit is not None else rows

    def artifacts(self) -> list[dict[str, Any]]:
        """Artifacts recorded for this session, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, path, bytes, note, created_at FROM artifacts"
                " WHERE session = ? ORDER BY id",
                (self.key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear(self) -> None:
        """Delete this session's events and artifacts."""
        with self._lock:
            self._db.execute("DELETE FROM events WHERE session = ?", (self.key,))
            self._db.execute("DELETE FROM artifacts WHERE session = ?", (self.key,))
            self._db.commit()

    def close(self) -> None:
        """Close the database. Safe to call twice."""
        with self._lock:
            self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["OUTPUT_LIMIT", "Session"]
