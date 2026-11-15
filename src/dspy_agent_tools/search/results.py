"""Shape raw engine hits into the public result payload."""

from __future__ import annotations

import urllib.parse
from typing import Any

from .config import SearchKind


def _clean_url(url: str) -> str:
    """Unwrap DuckDuckGo redirect links to the real target."""
    parsed = urllib.parse.urlparse("https:" + url if url.startswith("//") else url)
    if (
        parsed.hostname in ("duckduckgo.com", "www.duckduckgo.com")
        and parsed.path == "/l/"
    ):
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs:
            return qs["uddg"][0]
    return url


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _result_payload(
    kind: SearchKind,
    query: str,
    results: list[dict[str, Any]],
    status: str,
    error: str | None,
) -> dict[str, Any]:
    if kind == "news":
        items = [
            {
                "title": str(result.get("title", "")),
                "url": _clean_url(str(result.get("url", ""))),
                "body": str(result.get("body", "")),
                "source": str(result.get("source", "")),
                "date": str(result.get("date", "")),
            }
            for result in results
        ]
    else:
        items = [
            {
                "title": str(result.get("title", "")),
                "href": _clean_url(str(result.get("href", ""))),
                "body": str(result.get("body", "")),
            }
            for result in results
        ]
    payload: dict[str, Any] = {
        "query": query,
        "results": items,
        "count": len(items),
        "status": status,
    }
    if error is not None:
        payload["error"] = error
    return payload
