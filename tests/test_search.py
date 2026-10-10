"""Tests for dspy_agent_tools.search."""

import sys
import types
from typing import ClassVar

import pytest

from dspy_agent_tools.search import _clean_url, web_news, web_search

DDG_REDIRECT = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Freal&rut=abc"


class FakeDDGS:
    calls: ClassVar[list[tuple[str, dict]]] = []

    def __init__(self):
        pass

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
    module = FakeDdgsModule("ddgs")
    monkeypatch.setitem(sys.modules, "ddgs", module)
    return FakeDDGS


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

    def test_default_kwargs(self, fake_ddgs):
        web_search("q")
        _, kwargs = fake_ddgs.calls[0]
        assert kwargs["max_results"] == 5
        assert kwargs["region"] == "wt-wt"
        assert kwargs["timelimit"] is None

    def test_empty_results(self, fake_ddgs):
        fake_ddgs.text = lambda *_args, **_kwargs: []
        result = web_search("q")
        assert result == {"query": "q", "results": [], "count": 0}


class TestWebNews:
    def test_returns_shape_and_unwraps_redirects(self, fake_ddgs):
        result = web_news("test query")
        assert result["query"] == "test query"
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

    def test_default_kwargs(self, fake_ddgs):
        web_news("q")
        _, kwargs = fake_ddgs.calls[0]
        assert kwargs["max_results"] == 5
        assert kwargs["timelimit"] is None

    def test_empty_results(self, fake_ddgs):
        fake_ddgs.news = lambda *_args, **_kwargs: []
        result = web_news("q")
        assert result == {"query": "q", "results": [], "count": 0}
