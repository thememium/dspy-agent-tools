"""Fetch URLs and list directories for the curl, fetch, and ls tools."""

import re

from ..utils.process import execute, render, resolve_existing

_JUNK_TAGS = ["script", "style", "noscript", "iframe", "svg", "nav", "footer", "header"]
# Class/id tokens that mark page chrome.  Matched as whole tokens (see
# _is_junk), never as substrings: "ad" as a substring also matches "header",
# "loading", and "breadcrumb".
_JUNK_TOKENS = frozenset(
    {
        "ad",
        "ads",
        "advert",
        "adverts",
        "advertise",
        "advertisement",
        "advertising",
        "adsbygoogle",
        "adslot",
        "adunit",
        "banner",
        "banners",
        "cookie",
        "cookies",
        "consent",
        "popup",
        "modal",
        "sidebar",
        "related",
        "recommendation",
        "recommendations",
        "social",
        "share",
        "sharing",
        "comment",
        "comments",
        "disqus",
        "newsletter",
        "subscribe",
        "sponsored",
        "promo",
        "promotion",
    }
)
# Split class/id strings on punctuation and camelCase transitions.
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _is_junk(element) -> bool:
    """True when an element's class/id carries a whole junk token."""
    if element.name in ("html", "body"):
        return False
    classes = element.get("class") or []
    if isinstance(classes, str):
        classes = [classes]
    values = [*classes, str(element.get("id") or "")]
    tokens = {
        token.lower()
        for value in values
        for token in _TOKEN_SPLIT.split(str(value))
        if token
    }
    return not tokens.isdisjoint(_JUNK_TOKENS)


def bash_curl(url: str, options: str = "-sSL") -> str:
    """
    Fetch a URL with curl and return the response body.

    WHAT: Runs `curl` and returns the raw response body as a string.
    WHEN: Use for fetching web pages, APIs, or any http(s) content —
    including JSON APIs (pair with jq via bash_exec when needed). Not for
    searching local files; not for pages that need JavaScript rendering.
    HOW:
        - url (str): the URL to fetch, e.g. "https://api.github.com/...".
        - options (str, default "-sSL"): curl flags as a single string.
          The default is silent, shows errors on failure (stderr is merged
          into the output), and follows redirects. Useful additions:
          "-i" (include response headers), "-H 'Auth: Bearer ...'"
          (headers), "-X POST -d '{...}'" (requests), "-m 20" (per-request
          timeout), "-o /dev/null -w '%{http_code}'" (status only).

    A failed transfer (bad URL, connection refused) is reported as
    "[exit code N]" with curl's error message rather than an exception.

    For readable page text instead of raw HTML, use bash_fetch.
    """
    output, notices, elapsed, _ = execute(["curl", *options.split(), url])
    return render(output, notices, elapsed)


def bash_fetch(url: str, options: str = "-sSL") -> str:
    """
    Fetch a web page and return its readable content as markdown.

    WHAT: Downloads the URL (via curl, like bash_curl) and converts the
    HTML to clean markdown: scripts, styles, nav/header/footer, ads,
    cookie banners, sidebars, and similar chrome are stripped, the
    <article>/<main>/<body> content is kept, and the page <title> becomes
    a leading "# heading". WHEN: Use whenever you want to READ a web page
    (docs, articles, blog posts) instead of parsing raw HTML. Use
    bash_curl instead when you need the exact raw response (API JSON,
    headers, binary). Not suitable for pages that require JavaScript to
    render.
    HOW:
        - url (str): the page URL, e.g. "https://example.com/docs".
        - options (str, default "-sSL"): curl flags passed through,
          exactly as in bash_curl; you rarely need to change them.

    Returns the complete markdown text (no truncation). A failed transfer is
    reported with a "[exit code N]" notice and curl's error instead of the
    markdown.
    """
    output, notices, elapsed, rc = execute(["curl", *options.split(), url])
    if rc != 0:
        return render(output, notices, elapsed)
    if not output.strip():
        return render("[Error] Empty response from URL", notices, elapsed)

    try:
        from bs4 import BeautifulSoup, Comment
        from markdownify import markdownify as md
    except ImportError:
        return render(
            "[Error] bs4 and markdownify are required for HTML conversion",
            notices,
            elapsed,
        )

    try:
        soup = BeautifulSoup(output, "html.parser")

        # Choose the content root before pruning.  Pruning the whole document
        # first can remove an ancestor of <article>/<main>, and a junk-looking
        # class on <html>/<body> would otherwise decompose the entire page.
        content = soup.find("article") or soup.find("main") or soup.find("body") or soup
        for tag in list(content.find_all(_JUNK_TAGS)):
            if tag is not content:
                tag.decompose()

        for comment in content.find_all(string=lambda s: isinstance(s, Comment)):
            comment.extract()

        to_remove = [
            el for el in content.find_all(True) if el is not content and _is_junk(el)
        ]
        for el in to_remove:
            el.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        body_md = md(str(content), heading_style="ATX", strip=["img"])

        cleaned: list[str] = []
        blanks = 0
        for line in body_md.split("\n"):
            stripped = line.rstrip()
            if not stripped:
                blanks += 1
                if blanks <= 2:
                    cleaned.append("")
            else:
                blanks = 0
                cleaned.append(stripped)

        result = "\n".join(cleaned).strip()
        if title:
            result = f"# {title}\n\n{result}"
        return render(result, notices, elapsed)
    except Exception as exc:  # noqa: BLE001 — conversion failure returns text, never raises
        return render(f"[Error] HTML processing failed: {exc}", notices, elapsed)


def bash_ls(path: str = ".", options: str = "-la") -> str:
    """
    List the entries of a directory with ls.

    WHAT: Runs `ls` on one directory and returns its entries in long
    format. WHEN: Use to see what's directly inside a directory — file
    names, sizes, timestamps. Use bash_find to walk a whole tree by
    pattern, and bash_ripgrep to search file contents.
    HOW:
        - path (str, default "."): directory to list.
        - options (str, default "-la"): ls flags as a single string; the
          default shows all entries (including dotfiles) in long format.
          Useful ones: "-l" (long, no dotfiles), "-lah" (human sizes),
          "-lt" (newest first), "-1" (one per line).
    """
    output, notices, elapsed, _ = execute(
        ["ls", *options.split(), str(resolve_existing(path))]
    )
    return render(output, notices, elapsed)
