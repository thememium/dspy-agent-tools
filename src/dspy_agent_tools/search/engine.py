"""Query ddgs engines in failover order within a bounded budget."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from .config import _MAX_ERROR_CHARS, _REGION_RE, SearchKind
from .results import _cap


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
