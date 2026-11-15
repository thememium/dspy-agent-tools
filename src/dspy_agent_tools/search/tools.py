"""Public web and news search tools."""

from __future__ import annotations

from .config import (
    NEWS_ENGINES,
    SEARCH_BUDGET_S,
    SEARCH_ENGINE_ATTEMPTS,
    SEARCH_ENGINES,
    SEARCH_PASSES,
    SEARCH_RETRY_BACKOFF_S,
    SEARCH_TIMEOUT_S,
)
from .engine import _search
from .results import _result_payload


def web_search(
    query: str,
    max_results: int = 5,
    region: str | None = "us-en",
    timelimit: str | None = None,
) -> dict:
    """Search the web and return results with an explicit status.

    Returns `query`, `results` (`title`, `href`, `body`), `count`, and
    `status`: `ok`, `no_results` (engines answered but found nothing), or
    `error` (no engine answered). Check `status` before using results; an
    `error` includes the attempt count. Engines are tried in failover order
    inside a bounded budget: an empty answer is retried with a fresh client
    because its random browser profile can hide results, while an unreachable
    engine is skipped so the next one can answer.

    `region` must be a country-language code like "us-en", "uk-en", "de-de";
    other values (including None) are coerced to "us-en". `timelimit` filters
    by recency: "d" (day), "w" (week), "m" (month), "y" (year), or None.
    """
    results, error, empties, unavailable = _search(
        "text",
        query,
        max_results=max_results,
        region=region,
        timelimit=timelimit,
        engines=SEARCH_ENGINES,
        timeout=SEARCH_TIMEOUT_S,
        attempts=SEARCH_ENGINE_ATTEMPTS,
        passes=SEARCH_PASSES,
        budget=SEARCH_BUDGET_S,
        backoff=SEARCH_RETRY_BACKOFF_S,
    )
    if results:
        return _result_payload("text", query, results, "ok", None)
    status = "no_results" if empties and not unavailable else "error"
    return _result_payload("text", query, [], status, error)


def web_news(
    query: str,
    max_results: int = 5,
    timelimit: str | None = None,
) -> dict:
    """Search news and return recent results with an explicit status.

    Returns `query`, `results` (`title`, `url`, `body`, `source`, `date`),
    `count`, and `status`: `ok`, `no_results`, or `error`. Engines are tried
    in failover order with the same retry behaviour as `web_search`.
    `timelimit` filters by recency: "d", "w", "m", "y", or None.
    """
    results, error, empties, unavailable = _search(
        "news",
        query,
        max_results=max_results,
        region=None,
        timelimit=timelimit,
        engines=NEWS_ENGINES,
        timeout=SEARCH_TIMEOUT_S,
        attempts=SEARCH_ENGINE_ATTEMPTS,
        passes=SEARCH_PASSES,
        budget=SEARCH_BUDGET_S,
        backoff=SEARCH_RETRY_BACKOFF_S,
    )
    if results:
        return _result_payload("news", query, results, "ok", None)
    status = "no_results" if empties and not unavailable else "error"
    return _result_payload("news", query, [], status, error)
