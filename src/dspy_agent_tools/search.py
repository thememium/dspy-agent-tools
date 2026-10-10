import re
import urllib.parse

_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")


def _normalize_region(region: str | None) -> str:
    """Coerce ``region`` to the '<cc>-<lang>' form ddgs engines require.

    ddgs engines do ``region.split("-")`` and raise an opaque
    ``ValueError`` for anything without exactly one dash (e.g. "us",
    "United States", "").  "wt-wt" (the historical ddgs "no region"
    alias) also breaks engines that use the country code as a subdomain,
    sending requests to wt.wikipedia.org.
    """
    r = (region or "").strip().lower()
    if r == "wt-wt":
        return "us-en"
    if _REGION_RE.match(r):
        return r
    if re.fullmatch(r"[a-z]{2}", r):
        return f"{r}-en"
    return "us-en"


def _clean_url(url: str) -> str:
    """Unwrap DuckDuckGo redirect links to the real target."""
    if "duckduckgo.com/l/" in url and "uddg=" in url:
        parsed = urllib.parse.urlparse(url.replace("//", "https://"))
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs:
            return qs["uddg"][0]
    return url


def web_search(
    query: str,
    max_results: int = 5,
    region: str | None = "us-en",
    timelimit: str | None = None,
) -> dict:
    """
    Search the web with DuckDuckGo and return results.

    Returns {"query", "results": [{"title", "href", "body"}], "count"}.
    `region` must be a country-language code like "us-en", "uk-en",
    "de-de"; other values (including None) are coerced to "us-en".
    `timelimit` filters
    by recency: "d" (day), "w" (week), "m" (month), "y" (year), or None.
    """
    from ddgs import DDGS

    results = DDGS().text(
        query,
        max_results=max_results,
        region=_normalize_region(region),
        timelimit=timelimit,
    )
    return {
        "query": query,
        "results": [
            {
                "title": r.get("title", ""),
                "href": _clean_url(r.get("href", "")),
                "body": r.get("body", ""),
            }
            for r in results
        ],
        "count": len(results),
    }


def web_news(
    query: str,
    max_results: int = 5,
    timelimit: str | None = None,
) -> dict:
    """
    Search DuckDuckGo News and return recent news results.

    Returns {"query", "results": [{"title", "url", "body", "source",
    "date"}], "count"}. `timelimit` filters by recency: "d", "w", "m",
    "y", or None.
    """
    from ddgs import DDGS

    results = DDGS().news(
        query,
        max_results=max_results,
        timelimit=timelimit,
    )
    return {
        "query": query,
        "results": [
            {
                "title": r.get("title", ""),
                "url": _clean_url(r.get("url", "")),
                "body": r.get("body", ""),
                "source": r.get("source", ""),
                "date": r.get("date", ""),
            }
            for r in results
        ],
        "count": len(results),
    }
