"""Shared runtime for durable tests.

DBOS owns a process-wide singleton, so every test in this directory shares one
SQLite system database and one launched runtime. Agent names are unique per
test because DBOS workflow names are global.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest

from dspy_agent_tools import Durable


@pytest.fixture(scope="session", autouse=True)
def durable_runtime(tmp_path_factory: pytest.TempPathFactory) -> Generator[str]:
    path = tmp_path_factory.mktemp("durable") / "db.sqlite"
    name = f"dt-{uuid.uuid4().hex[:10]}"
    Durable.configure(
        name=name,
        database_url=f"sqlite:///{path}",
        log_level="WARNING",
    )
    yield name
    Durable.shutdown()


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
