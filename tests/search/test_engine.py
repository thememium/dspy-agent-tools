"""Tests for engine failover, retry pauses, and the search budget."""

import time

import pytest

from dspy_agent_tools.search import engine as engine_module
from dspy_agent_tools.search.engine import _normalize_region, _pause, _search


@pytest.mark.parametrize(
    ("region", "expected"),
    [(None, "us-en"), ("wt-wt", "us-en"), ("US-EN", "us-en"), ("de-de", "de-de")],
)
def test_region_is_normalized_for_the_engines(region, expected):
    assert _normalize_region(region) == expected


class TestPause:
    @pytest.fixture
    def slept(self, monkeypatch):
        calls = []
        monkeypatch.setattr(time, "sleep", calls.append)
        return calls

    def test_sleeps_while_budget_allows(self, monkeypatch, slept):
        monkeypatch.setattr(time, "monotonic", lambda: 100.0)
        _pause(0.5, 101.0)
        assert slept == [0.5]

    def test_skips_sleep_when_budget_is_too_tight(self, monkeypatch, slept):
        # Sleeping would land exactly on the deadline, leaving no time to retry.
        monkeypatch.setattr(time, "monotonic", lambda: 100.5)
        _pause(0.5, 101.0)
        assert slept == []

    def test_zero_delay_never_sleeps(self, monkeypatch, slept):
        monkeypatch.setattr(time, "monotonic", lambda: 100.0)
        _pause(0.0, 101.0)
        assert slept == []


def _search_budget(**overrides):
    kwargs = {
        "max_results": 5,
        "region": None,
        "timelimit": None,
        "engines": ("a", "b"),
        "timeout": 8.0,
        "attempts": 2,
        "passes": 2,
        "budget": 10.0,
        "backoff": 0.5,
    }
    kwargs.update(overrides)
    return kwargs


class TestSearchBudget:
    def test_no_attempt_when_budget_is_under_one_second(self, monkeypatch, no_backoff):
        engines = []

        def record(*args):
            engines.append(args[5])
            return []

        monkeypatch.setattr(engine_module, "_search_engine", record)
        monkeypatch.setattr(time, "monotonic", lambda: 0.0)
        result = _search("text", "q", **_search_budget(budget=0.5))
        assert engines == []
        assert result == ([], "search was not attempted (after 0 attempt(s))", 0, 0)

    def test_pass_stops_once_the_deadline_is_near(self, monkeypatch, no_backoff):
        engines = []

        def empty(kind, query, max_results, region, timelimit, engine, timeout):
            engines.append(engine)
            return []

        # deadline=10.0; two attempts fit, then the post-engine check is at 9.5.
        times = iter([0.0, 0.0, 1.0, 9.5])
        monkeypatch.setattr(time, "monotonic", lambda: next(times))
        monkeypatch.setattr(engine_module, "_search_engine", empty)
        result = _search("text", "q", **_search_budget())
        assert result == ([], "No results found. (after 2 attempt(s))", 2, 0)
        # Engine "b" is not called: the pass ended with no time left.
        assert engines == ["a", "a"]
