"""Web search tools with bounded engine failover and explicit outcomes."""

from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Sequence
from typing import Any, Literal

_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]{2,3}$")

# ddgs builds a fresh HTTP client per instance and randomizes its browser
# profile, including mobile operating systems. Some engines answer a mobile
# profile with a layout their parser cannot read and report "No results found."
# for a query that does have results, while the same engine succeeds with a
# desktop profile. An empty answer is therefore not proof of an empty web;
# retry that engine with a fresh client before moving on. Transport failures
# are different: the engine itself is unreachable, so other engines are tried
# instead of waiting on it. Engines are ordered by observed reliability.
SEARCH_ENGINES: tuple[str, ...] = (
    "yahoo",
    "duckduckgo",
    "brave",
    "mojeek",
    "startpage",
    "wikipedia",
    "grokipedia",
)
NEWS_ENGINES: tuple[str, ...] = ("yahoo", "duckduckgo", "bing")
SEARCH_ENGINE_ATTEMPTS = 4
SEARCH_PASSES = 2
SEARCH_TIMEOUT_S = 8.0
SEARCH_RETRY_BACKOFF_S = 0.5
SEARCH_BUDGET_S = 45.0
_MAX_ERROR_CHARS = 600

SearchKind = Literal["text", "news"]


def _normalize_region(region: str | None) -> str:
    """Coerce ``region`` to the '<cc>-<lang>' form ddgs engines require.

    ddgs engines do ``region.split("-")`` and raise an opaque
    ``ValueError`` for anything without exactly one dash (e.g. "us",
    "United States", "").  "wt-wt" (the historical ddgs "no region"
    alias) also breaks engines that use the country code as a subdomain,
    sending requests to wt.wikipedia.org.  Language codes may be two or
    three letters (e.g. "hk-tzh", "tw-tzh").
    """
    r = (region or "").strip().lower()
    if r == "wt-wt":
        return "us-en"
    if _REGION_RE.match(r):
        return r
    return "us-en"


def _clean_url(url: str) -> str:
    """Unwrap DuckDuckGo redirect links to the real target."""
    if "duckduckgo.com/l/" in url and "uddg=" in url:
        parsed = urllib.parse.urlparse(url.replace("//", "https://"))
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs:
            return qs["uddg"][0]
    return url


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _pause(seconds: float, deadline: float) -> None:
    """Sleep briefly between retries, leaving enough budget for another call."""
    if seconds > 0 and time.monotonic() + seconds < deadline:
        time.sleep(seconds)


def _search_engine(
    kind: SearchKind,
    query: str,
    max_results: int,
    region: str | None,
    timelimit: str | None,
    engine: str,
    timeout: int,
) -> list[dict[str, Any]]:
    """Query one ddgs backend with a fresh client; raise if it did not answer."""
    from ddgs import DDGS

    client = DDGS(timeout=timeout)
    if kind == "news":
        return client.news(
            query,
            max_results=max_results,
            timelimit=timelimit,
            backend=engine,
        )
    return client.text(
        query,
        max_results=max_results,
        region=_normalize_region(region),
        timelimit=timelimit,
        backend=engine,
    )


def _search(
    kind: SearchKind,
    query: str,
    *,
    max_results: int,
    region: str | None,
    timelimit: str | None,
    engines: Sequence[str],
    timeout: float,
    attempts: int,
    passes: int,
    budget: float,
    backoff: float,
) -> tuple[list[dict[str, Any]], str | None, int, int]:
    """Try engines in failover order until one returns results.

    Returns ``(results, error, empty_answers, unavailable_engines)``.
    An engine that answers with no results gets ``attempts`` fresh clients
    because the random browser profile can hide real results. An engine that
    raises a transport error is skipped for the rest of the search. If any
    engine was unreachable, one more pass retries the reachable engines, since
    the missing engine may have been the only one with results.
    """
    deadline = time.monotonic() + budget
    failure: str | None = None
    calls = empties = unavailable = 0
    unreachable: set[str] = set()

    for _pass in range(passes):
        for engine in engines:
            if engine in unreachable:
                continue
            for _attempt in range(attempts):
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    break
                calls += 1
                request_timeout = max(1, int(min(timeout, remaining)))
                try:
                    results = _search_engine(
                        kind,
                        query,
                        max_results,
                        region,
                        timelimit,
                        engine,
                        request_timeout,
                    )
                except Exception as exc:  # noqa: BLE001 - provider failures are feedback
                    failure = f"{type(exc).__name__}: {exc}"
                    if "no results found" in failure.lower():
                        empties += 1
                        _pause(backoff, deadline)
                        continue
                    unavailable += 1
                    unreachable.add(engine)
                    break
                if results:
                    return results, None, empties, unavailable
                empties += 1
                _pause(backoff, deadline)
            if time.monotonic() >= deadline - 1:
                break
        if unavailable == 0 or time.monotonic() >= deadline - 1:
            # Every engine answered, and none had results: another pass would
            # only repeat the same empty answers.
            break

    if failure is None:
        failure = "No results found." if empties else "search was not attempted"
    return (
        [],
        _cap(f"{failure} (after {calls} attempt(s))", _MAX_ERROR_CHARS),
        empties,
        unavailable,
    )


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
