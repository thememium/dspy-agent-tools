"""Tests for dspy_agent_tools.search.results."""

from dspy_agent_tools.search.results import _clean_url

from .fakes import DDG_REDIRECT


class TestCleanUrl:
    def test_non_duckduckgo_urls_are_not_unwrapped(self):
        url = "https://example.com/duckduckgo.com/l/?uddg=other"
        assert _clean_url(url) == url
        url = "https://duckduckgo.com.evil.example/l/?uddg=other"
        assert _clean_url(url) == url

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
