"""Tests for the public web and news search tools."""

from dspy_agent_tools.search import engine as engine_module
from dspy_agent_tools.search import web_news, web_search
from dspy_agent_tools.search.config import (
    NEWS_ENGINES,
    SEARCH_ENGINE_ATTEMPTS,
    SEARCH_ENGINES,
    SEARCH_TIMEOUT_S,
)


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
        assert kwargs["backend"] == SEARCH_ENGINES[0]
        assert fake_ddgs.timeouts[0] == int(SEARCH_TIMEOUT_S)

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
        assert len(fake_ddgs.calls) == len(SEARCH_ENGINES) * SEARCH_ENGINE_ATTEMPTS

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
            if len(attempts) < SEARCH_ENGINE_ATTEMPTS:
                raise RuntimeError("No results found.")
            return [{"title": "hit", "href": "https://example.com/", "body": "body"}]

        monkeypatch.setattr(engine_module, "_search_engine", flaky)
        result = web_search("query")
        assert result["status"] == "ok" and result["count"] == 1
        assert attempts == [SEARCH_ENGINES[0]] * SEARCH_ENGINE_ATTEMPTS

    def test_unreachable_engine_is_skipped_for_the_rest_of_the_search(
        self, monkeypatch, no_backoff
    ):
        calls = []

        def failed(kind, query, max_results, region, timelimit, engine, timeout):
            calls.append(engine)
            raise RuntimeError("error sending request > operation timed out")

        monkeypatch.setattr(engine_module, "_search_engine", failed)
        result = web_search("query")
        assert result["status"] == "error"
        assert "attempt" in result["error"]
        # A dead engine is not retried; every other engine still gets one try.
        assert calls == list(SEARCH_ENGINES)

    def test_unreachable_engine_falls_through_to_the_next(
        self, monkeypatch, no_backoff
    ):
        engines = []

        def flaky(kind, query, max_results, region, timelimit, engine, timeout):
            engines.append(engine)
            if engine == SEARCH_ENGINES[0]:
                raise RuntimeError("error sending request > operation timed out")
            return [
                {
                    "title": "hit",
                    "href": "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F",
                    "body": "body",
                }
            ]

        monkeypatch.setattr(engine_module, "_search_engine", flaky)
        result = web_search("query")
        assert result["status"] == "ok" and result["count"] == 1
        assert result["results"][0]["href"] == "https://example.com/"
        assert engines[:2] == list(SEARCH_ENGINES[:2])

    def test_unreachable_engine_does_not_end_the_failover_passes(
        self, monkeypatch, no_backoff
    ):
        hits = 0

        def flaky(kind, query, max_results, region, timelimit, engine, timeout):
            nonlocal hits
            if engine == SEARCH_ENGINES[1]:
                raise RuntimeError("error sending request > operation timed out")
            if engine == SEARCH_ENGINES[0]:
                hits += 1
                if hits > SEARCH_ENGINE_ATTEMPTS:
                    return [{"title": "hit", "href": "https://example.com/"}]
            raise RuntimeError("No results found.")

        monkeypatch.setattr(engine_module, "_search_engine", flaky)
        result = web_search("query")
        assert result["status"] == "ok" and result["count"] == 1
        # The first engine exhausted its retries in pass one; the second pass
        # retries the reachable engines because another engine never answered.
        assert hits == SEARCH_ENGINE_ATTEMPTS + 1

    def test_reachable_engines_are_not_retried_when_all_answered(
        self, monkeypatch, no_backoff
    ):
        calls = []

        def empty(kind, query, max_results, region, timelimit, engine, timeout):
            calls.append(engine)
            return []

        monkeypatch.setattr(engine_module, "_search_engine", empty)
        result = web_search("query")
        assert result["status"] == "no_results"
        assert calls == [
            engine for engine in SEARCH_ENGINES for _ in range(SEARCH_ENGINE_ATTEMPTS)
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
        assert kwargs["backend"] == NEWS_ENGINES[0]

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
        assert len(fake_ddgs.calls) == len(NEWS_ENGINES) * SEARCH_ENGINE_ATTEMPTS
