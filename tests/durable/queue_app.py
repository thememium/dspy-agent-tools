"""Subprocess fixture proving queued workers execute."""

from __future__ import annotations

import os
import pathlib

from dspy_agent_tools import Durable

HERE = pathlib.Path(os.environ["DURABLE_DIR"])
APP = os.environ["DURABLE_APP"]

Durable.configure(
    name=APP,
    database_url=f"sqlite:///{HERE / 'db.sqlite'}",
    log_level="WARNING",
)
Durable.queue("compute", concurrency=2)


@Durable.worker(name="double", queue="compute", concurrency=2)
def double(value: int) -> int:
    """Double a number."""
    return value * 2


Durable.launch()
print(double.enqueue(value=21).result())
Durable.shutdown()
