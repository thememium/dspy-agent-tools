"""Fixtures shared across the MCP test modules."""

import pytest

from .helpers import start


@pytest.fixture()
def tools():
    server = start()
    try:
        yield server
    finally:
        server.close()
