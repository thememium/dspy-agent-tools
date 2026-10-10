import urllib.parse


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
    region: str = "wt-wt",
    timelimit: str | None = None,
) -> dict:
    """
    Search the web with DuckDuckGo and return results.

    Returns {"query", "results": [{"title", "href", "body"}], "count"}.
    `timelimit` filters by recency: "d" (day), "w" (week), "m" (month),
    "y" (year), or None.
    """
    from ddgs import DDGS

    results = DDGS().text(
        query,
        max_results=max_results,
        region=region,
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
