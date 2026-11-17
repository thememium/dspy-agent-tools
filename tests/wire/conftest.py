"""Shared fixtures for the wire tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from dspy_agent_tools import Session


@pytest.fixture
def session() -> Iterator[Session]:
    current = Session()
    yield current
    current.close()
