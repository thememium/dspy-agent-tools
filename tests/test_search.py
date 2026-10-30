"""Tests for dspy_agent_tools.search."""

import sys
import types
from typing import ClassVar

import pytest

from dspy_agent_tools import search
from dspy_agent_tools.search import _clean_url, _pause, _search, web_news, web_search

DDG_REDIRECT = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Freal&rut=abc"


class FakeDDGS:
    calls: ClassVar[list[tuple[str, dict]]] = []
    timeouts: ClassVar[list[int | None]] = []

    def __init__(self, timeout=None):
        FakeDDGS.timeouts.append(timeout)

    def text(self, query, **kwargs):
        FakeDDGS.calls.append(("text", {"query": query, **kwargs}))
        return [
            {"title": "Result One", "href": DDG_REDIRECT, "body": "Body one"},
            {
                "title": "Result Two",
                "href": "https://plain.example.com",
                "body": "Body two",
            },
            {"href": "https://sparse.example.com"},  # missing keys -> defaults
        ]

    def news(self, query, **kwargs):
        FakeDDGS.calls.append(("news", {"query": query, **kwargs}))
        return [
            {
                "title": "News One",
                "url": DDG_REDIRECT,
                "body": "News body",
                "source": "Example",
                "date": "2026-10-08",
            },
            {"url": "https://plain.example.com"},  # missing keys -> defaults
        ]


class FakeDdgsModule(types.ModuleType):
    DDGS = FakeDDGS


@pytest.fixture
def fake_ddgs(monkeypatch):
    FakeDDGS.calls = []
    FakeDDGS.timeouts = []
    module = FakeDdgsModule("ddgs")
    monkeypatch.setitem(sys.modules, "ddgs", module)
    return FakeDDGS


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(search, "_pause", lambda *_args: None)


class TestCleanUrl:
    def test_unwraps_redirect(self):
        assert _clean_url(DDG_REDIRECT) == "https://example.com/real"

    def test_plain_url_untouched(self):
        assert _clean_url("https://example.com") == "https://example.com"

    def test_redirect_without_uddg_untouched(self):
        url = "https://duckduckgo.com/l/?other=1"
        assert _clean_url(url) == url

    def test_protocol_relative_redirect_is_upgraded_to_https(self):
        url = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com"
        assert _clean_url(url) == "https://example.com"


class TestWebSearch:
    def test_returns_shape_and_unwraps_redirects(self, fake_ddgs):
        result = web_search("test query")
        assert result["query"] == "test query"
        assert result["status"] == "ok"
        assert result["count"] == 3
        assert len(result["results"]) == 3
        first = result["results"][0]
        assert first == {
            "title": "Result One",
            "href": "https://example.com/real",
            "body": "Body one",
        }
        assert result["results"][2] == {
            "title": "",
            "href": "https://sparse.example.com",
            "body": "",
        }

    def test_kwargs_forwarded(self, fake_ddgs):
        web_search("q", max_results=7, region="us-en", timelimit="w")
        kind, kwargs = fake_ddgs.calls[0]
        assert kind == "text"
        assert kwargs["max_results"] == 7
        assert kwargs["region"] == "us-en"
        assert kwargs["timelimit"] == "w"
        assert kwargs["backend"] == search.SEARCH_ENGINES[0]
        assert fake_ddgs.timeouts[0] == int(search.SEARCH_TIMEOUT_S)

    def test_default_kwargs(self, fake_ddgs):
        web_search("q")
        _, kwargs = fake_ddgs.calls[0]
        assert kwargs["max_results"] == 5
        assert kwargs["region"] == "us-en"
        assert kwargs["timelimit"] is None

    def test_region_coerced_to_country_language_form(self, fake_ddgs):
        for raw, expected in [
            ("us", "us-en"),
            ("US", "us-en"),
            ("United States", "us-en"),
            ("", "us-en"),
            (None, "us-en"),
            ("wt-wt", "us-en"),
            ("US-EN", "us-en"),
            ("a-b-c", "us-en"),
            ("uk", "us-en"),
            ("uk-en", "uk-en"),
            ("de-de", "de-de"),
            ("hk-tzh", "hk-tzh"),
            ("tw-tzh", "tw-tzh"),
            (" hk-tzh ", "hk-tzh"),
        ]:
            web_search("q", region=raw)
            _, kwargs = fake_ddgs.calls[-1]
            assert kwargs["region"] == expected, (raw, kwargs["region"])

    def test_empty_results_are_retried_and_reported(
        self, fake_ddgs, monkeypatch, no_backoff
    ):
        def empty(self, query, **kwargs):
            fake_ddgs.calls.append(("text", {"query": query, **kwargs}))
            return []

        monkeypatch.setattr(fake_ddgs, "text", empty)
        result = web_search("q")
        assert result["results"] == [] and result["count"] == 0
        assert result["status"] == "no_results"
        assert "attempt" in result["error"]
        # Every engine is retried: a random mobile profile can hide real
        # results. One fully answered pass is enough to conclude it is empty.
        assert (
            len(fake_ddgs.calls)
            == len(search.SEARCH_ENGINES) * search.SEARCH_ENGINE_ATTEMPTS
        )

    def test_each_attempt_uses_a_fresh_client(self, fake_ddgs, monkeypatch, no_backoff):
        def empty(self, query, **kwargs):
            fake_ddgs.calls.append(("text", {"query": query, **kwargs}))
            return []

        monkeypatch.setattr(fake_ddgs, "text", empty)
        web_search("q")
        assert len(fake_ddgs.timeouts) == len(fake_ddgs.calls)

    def test_no_results_exception_is_retried_as_an_empty_answer(
        self, monkeypatch, no_backoff
    ):
        attempts = []

        def flaky(kind, query, max_results, region, timelimit, engine, timeout):
            attempts.append(engine)
            if len(attempts) < search.SEARCH_ENGINE_ATTEMPTS:
                raise RuntimeError("No results found.")
            return [{"title": "hit", "href": "https://example.com/", "body": "body"}]

        monkeypatch.setattr(search, "_search_engine", flaky)
        result = web_search("query")
        assert result["status"] == "ok" and result["count"] == 1
        assert attempts == [search.SEARCH_ENGINES[0]] * search.SEARCH_ENGINE_ATTEMPTS

    def test_unreachable_engine_is_skipped_for_the_rest_of_the_search(
        self, monkeypatch, no_backoff
    ):
        calls = []

        def failed(kind, query, max_results, region, timelimit, engine, timeout):
            calls.append(engine)
            raise RuntimeError("error sending request > operation timed out")

        monkeypatch.setattr(search, "_search_engine", failed)
        result = web_search("query")
        assert result["status"] == "error"
        assert "attempt" in result["error"]
        # A dead engine is not retried; every other engine still gets one try.
        assert calls == list(search.SEARCH_ENGINES)

    def test_unreachable_engine_falls_through_to_the_next(
        self, monkeypatch, no_backoff
    ):
        engines = []

        def flaky(kind, query, max_results, region, timelimit, engine, timeout):
            engines.append(engine)
            if engine == search.SEARCH_ENGINES[0]:
                raise RuntimeError("error sending request > operation timed out")
            return [
                {
                    "title": "hit",
                    "href": "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F",
                    "body": "body",
                }
            ]

        monkeypatch.setattr(search, "_search_engine", flaky)
        result = web_search("query")
        assert result["status"] == "ok" and result["count"] == 1
        assert result["results"][0]["href"] == "https://example.com/"
        assert engines[:2] == list(search.SEARCH_ENGINES[:2])

    def test_unreachable_engine_does_not_end_the_failover_passes(
        self, monkeypatch, no_backoff
    ):
        hits = 0

        def flaky(kind, query, max_results, region, timelimit, engine, timeout):
            nonlocal hits
            if engine == search.SEARCH_ENGINES[1]:
                raise RuntimeError("error sending request > operation timed out")
            if engine == search.SEARCH_ENGINES[0]:
                hits += 1
                if hits > search.SEARCH_ENGINE_ATTEMPTS:
                    return [{"title": "hit", "href": "https://example.com/"}]
            raise RuntimeError("No results found.")

        monkeypatch.setattr(search, "_search_engine", flaky)
        result = web_search("query")
        assert result["status"] == "ok" and result["count"] == 1
        # The first engine exhausted its retries in pass one; the second pass
        # retries the reachable engines because another engine never answered.
        assert hits == search.SEARCH_ENGINE_ATTEMPTS + 1

    def test_reachable_engines_are_not_retried_when_all_answered(
        self, monkeypatch, no_backoff
    ):
        calls = []

        def empty(kind, query, max_results, region, timelimit, engine, timeout):
            calls.append(engine)
            return []

        monkeypatch.setattr(search, "_search_engine", empty)
        result = web_search("query")
        assert result["status"] == "no_results"
        assert calls == [
            engine
            for engine in search.SEARCH_ENGINES
            for _ in range(search.SEARCH_ENGINE_ATTEMPTS)
        ]


class TestWebNews:
    def test_returns_shape_and_unwraps_redirects(self, fake_ddgs):
        result = web_news("test query")
        assert result["query"] == "test query"
        assert result["status"] == "ok"
        assert result["count"] == 2
        first = result["results"][0]
        assert first == {
            "title": "News One",
            "url": "https://example.com/real",
            "body": "News body",
            "source": "Example",
            "date": "2026-10-08",
        }
        assert result["results"][1] == {
            "title": "",
            "url": "https://plain.example.com",
            "body": "",
            "source": "",
            "date": "",
        }

    def test_kwargs_forwarded(self, fake_ddgs):
        web_news("q", max_results=9, timelimit="m")
        kind, kwargs = fake_ddgs.calls[0]
        assert kind == "news"
        assert kwargs["max_results"] == 9
        assert kwargs["timelimit"] == "m"
        assert kwargs["backend"] == search.NEWS_ENGINES[0]

    def test_default_kwargs(self, fake_ddgs):
        web_news("q")
        _, kwargs = fake_ddgs.calls[0]
        assert kwargs["max_results"] == 5
        assert kwargs["timelimit"] is None

    def test_empty_results_report_no_results(self, fake_ddgs, monkeypatch, no_backoff):
        def empty(self, query, **kwargs):
            fake_ddgs.calls.append(("news", {"query": query, **kwargs}))
            return []

        monkeypatch.setattr(fake_ddgs, "news", empty)
        result = web_news("q")
        assert result["results"] == [] and result["count"] == 0
        assert result["status"] == "no_results"
        assert len(fake_ddgs.calls) == len(search.NEWS_ENGINES) * (
            search.SEARCH_ENGINE_ATTEMPTS
        )


@pytest.mark.parametrize(
    ("region", "expected"),
    [(None, "us-en"), ("wt-wt", "us-en"), ("US-EN", "us-en"), ("de-de", "de-de")],
)
def test_region_is_normalized_for_the_engines(region, expected):
    assert search._normalize_region(region) == expected


class TestPause:
    @pytest.fixture
    def slept(self, monkeypatch):
        calls = []
        monkeypatch.setattr(search.time, "sleep", calls.append)
        return calls

    def test_sleeps_while_budget_allows(self, monkeypatch, slept):
        monkeypatch.setattr(search.time, "monotonic", lambda: 100.0)
        _pause(0.5, 101.0)
        assert slept == [0.5]

    def test_skips_sleep_when_budget_is_too_tight(self, monkeypatch, slept):
        # Sleeping would land exactly on the deadline, leaving no time to retry.
        monkeypatch.setattr(search.time, "monotonic", lambda: 100.5)
        _pause(0.5, 101.0)
        assert slept == []

    def test_zero_delay_never_sleeps(self, monkeypatch, slept):
        monkeypatch.setattr(search.time, "monotonic", lambda: 100.0)
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

        monkeypatch.setattr(search, "_search_engine", record)
        monkeypatch.setattr(search.time, "monotonic", lambda: 0.0)
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
        monkeypatch.setattr(search.time, "monotonic", lambda: next(times))
        monkeypatch.setattr(search, "_search_engine", empty)
        result = _search("text", "q", **_search_budget())
        assert result == ([], "No results found. (after 2 attempt(s))", 2, 0)
        # Engine "b" is not called: the pass ended with no time left.
        assert engines == ["a", "a"]
