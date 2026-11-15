"""Fixtures shared by the search tests."""

import sys

import pytest

from dspy_agent_tools.search import engine as engine_module

from .fakes import FakeDDGS, FakeDdgsModule


@pytest.fixture
def fake_ddgs(monkeypatch):
    FakeDDGS.calls = []
    FakeDDGS.timeouts = []
    module = FakeDdgsModule("ddgs")
    monkeypatch.setitem(sys.modules, "ddgs", module)
    return FakeDDGS


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(engine_module, "_pause", lambda *_args: None)
