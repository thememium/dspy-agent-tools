"""Handles for persistent DBOS schedules."""

from __future__ import annotations

from typing import Any

from .exceptions import DurableError
from .runtime import runtime


class Schedule:
    """A recurring durable execution registered with DBOS."""

    def __init__(self, name: str) -> None:
        self.name = name

    @classmethod
    def get(cls, name: str) -> Schedule:
        """Return a handle for an existing schedule."""
        return cls(name)

    def __repr__(self) -> str:
        return f"Schedule(name={self.name!r})"

    def _client(self) -> Any:
        return runtime().client()

    def info(self) -> dict[str, Any]:
        """Return the schedule row from the DBOS system database."""
        schedule = self._client().get_schedule(self.name)
        if schedule is None:
            raise DurableError(f"no schedule named {self.name!r}")
        return dict(schedule)

    def pause(self) -> Schedule:
        """Stop firing until :meth:`resume`."""
        self._client().pause_schedule(self.name)
        return self

    def resume(self) -> Schedule:
        """Resume a paused schedule."""
        self._client().resume_schedule(self.name)
        return self

    def trigger(self) -> Any:
        """Run the schedule once now and return its :class:`Run`."""
        handle = self._client().trigger_schedule(self.name)
        from .run import Run

        return Run(handle.get_workflow_id())

    def delete(self) -> None:
        """Delete the schedule."""
        self._client().delete_schedule(self.name)
