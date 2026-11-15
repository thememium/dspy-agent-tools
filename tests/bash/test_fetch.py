"""Tests for dspy_agent_tools.bash.fetch."""

import sys
import types

import pytest

from dspy_agent_tools.bash import bash_curl, bash_fetch, bash_ls
from dspy_agent_tools.bash.fetch import _is_junk


class TestBashCurl:
    def test_fetches_body(self, tmp_path):
        f = tmp_path / "page.txt"
        f.write_text("plain body")
        result = bash_curl(f"file://{f}")
        assert result.startswith("plain body")

    def test_failed_transfer_is_a_notice(self):
        result = bash_curl("file:///nonexistent/definitely/missing")
        assert "[exit code" in result


HTML_PAGE = """\
<html>
  <head>
    <title>Page Title</title>
    <style>.junk {{ color: red }}</style>
    <script>var junk = 1;</script>
  </head>
  <body>
    <!-- analytics junk comment -->
    <nav>Navigation junk</nav>
    <div class="cookie-banner">Accept cookies</div>
    <article>
      <h1>Real Heading</h1>
      <p>Real paragraph one.</p>
      <p>Real paragraph two.</p>
    </article>
    <footer>Footer junk</footer>
  </body>
</html>
"""


class _FakeElement:
    """Minimal bs4.Tag stand-in for _is_junk's attribute access."""

    def __init__(self, name, attrs):
        self.name = name
        self._attrs = attrs

    def get(self, key):
        return self._attrs.get(key)


class TestIsJunk:
    def test_html_and_body_are_never_junk(self):
        assert _is_junk(_FakeElement("html", {"class": "promo"})) is False
        assert _is_junk(_FakeElement("body", {"id": "sidebar"})) is False

    def test_string_class_attribute_is_treated_as_one_value(self):
        # Defensive: parsers other than html.parser may return a bare string.
        assert _is_junk(_FakeElement("div", {"class": "promo-banner"})) is True

    def test_whole_tokens_match_but_substrings_do_not(self):
        assert _is_junk(_FakeElement("div", {"class": "ad-container"})) is True
        assert _is_junk(_FakeElement("div", {"class": "loading-note"})) is False


class TestBashFetch:
    def test_converts_html_to_clean_markdown(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text(HTML_PAGE)
        result = bash_fetch(f"file://{f}")
        assert result.startswith("# Page Title")
        assert "Real paragraph one." in result
        assert "Navigation junk" not in result
        assert "Accept cookies" not in result
        assert "Footer junk" not in result
        assert "var junk = 1;" not in result
        assert "analytics junk comment" not in result

    def test_header_in_root_class_does_not_wipe_the_page(self, tmp_path):
        # Regression: substring-matching "ad" against "header" removed <html>
        # itself, so the whole document decomposed to empty markdown.
        f = tmp_path / "page.html"
        f.write_text(
            '<html class="vector-feature-language-in-header-enabled">'
            "<head><title>Header Page</title></head>"
            "<body><header>Site chrome</header>"
            "<main><p>Article body survives.</p></main></body></html>"
        )
        result = bash_fetch(f"file://{f}")
        assert result.startswith("# Header Page")
        assert "Article body survives." in result
        assert "Site chrome" not in result

    def test_large_page_keeps_content_past_the_old_cap(self, tmp_path):
        # Regression: raw HTML was capped at 30KB before parsing, so an
        # article starting beyond that window never reached the converter.
        filler = "f" * 60_000
        f = tmp_path / "big.html"
        f.write_text(
            "<html><head><title>Big Page</title></head><body>"
            f"<div>{filler}</div>"
            "<article><p>Deep article text.</p></article>"
            "</body></html>"
        )
        result = bash_fetch(f"file://{f}")
        assert "Deep article text." in result
        assert "truncated" not in result

    def test_junk_class_tokens_are_removed_without_overmatching(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text(
            "<html><body><article>"
            '<div class="ad-container">Buy this</div>'
            '<div class="adSlot">Ad slot</div>'
            '<div class="cookie-banner">Accept cookies</div>'
            '<div class="sidebar">Sidebar text</div>'
            '<div class="breadcrumb download loading">Keep breadcrumb</div>'
            "<p>Real content.</p>"
            "</article></body></html>"
        )
        result = bash_fetch(f"file://{f}")
        assert "Real content." in result
        assert "Keep breadcrumb" in result
        assert "Buy this" not in result
        assert "Ad slot" not in result
        assert "Accept cookies" not in result
        assert "Sidebar text" not in result

    def test_junk_tags_inside_the_content_root_are_removed(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text(
            "<html><body><article>"
            "<script>alert(1)</script>"
            "<style>.x { color: red }</style>"
            "<nav>Nav chrome</nav>"
            "<p>Real content.</p>"
            "</article></body></html>"
        )
        result = bash_fetch(f"file://{f}")
        assert "Real content." in result
        assert "alert(1)" not in result
        assert "color: red" not in result
        assert "Nav chrome" not in result

    def test_comment_inside_the_content_root_is_stripped(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text(
            "<html><body><article>"
            "<!-- hidden note -->"
            "<p>Real content.</p>"
            "</article></body></html>"
        )
        result = bash_fetch(f"file://{f}")
        assert "hidden note" not in result
        assert "Real content." in result

    def test_large_markdown_is_returned_in_full(self, tmp_path):
        body = "word " * 12_000  # ~60KB of markdown
        f = tmp_path / "long.html"
        f.write_text(f"<html><body><article><p>{body}</p></article></body></html>")
        result = bash_fetch(f"file://{f}")
        assert len(result) > 30_000
        assert "word word word" in result
        assert "truncated" not in result

    def test_failed_transfer_returns_raw_output(self):
        result = bash_fetch("file:///nonexistent/definitely/missing")
        assert "[exit code" in result
        assert "# " not in result.split("\n", 1)[0]

    def test_empty_response(self, tmp_path):
        f = tmp_path / "empty.html"
        f.write_text("")
        result = bash_fetch(f"file://{f}")
        assert "[Error] Empty response from URL" in result

    def test_missing_bs4_reports_import_error(self, tmp_path, monkeypatch):
        f = tmp_path / "page.html"
        f.write_text("<html><body><p>hi</p></body></html>")
        monkeypatch.setitem(sys.modules, "bs4", None)
        result = bash_fetch(f"file://{f}")
        assert "bs4 and markdownify are required" in result

    def test_conversion_failure_is_returned_not_raised(self, tmp_path, monkeypatch):
        f = tmp_path / "page.html"
        f.write_text("<html><body><p>hi</p></body></html>")

        def boom(*_args, **_kwargs):
            raise RuntimeError("markdownify exploded")

        fake = types.SimpleNamespace(markdownify=boom)
        monkeypatch.setitem(sys.modules, "markdownify", fake)
        result = bash_fetch(f"file://{f}")
        assert "[Error] HTML processing failed: markdownify exploded" in result


class TestBashLs:
    def test_lists_entries(self, tmp_path):
        (tmp_path / "visible.txt").write_text("x")
        result = bash_ls(str(tmp_path))
        assert "visible.txt" in result

    def test_custom_options(self, tmp_path):
        (tmp_path / "visible.txt").write_text("x")
        result = bash_ls(str(tmp_path), options="-1")
        assert "visible.txt" in result
        assert "total" not in result

    def test_missing_path_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            bash_ls("/nonexistent/definitely/missing")
