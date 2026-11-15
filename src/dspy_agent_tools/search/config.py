"""Engine order, retry policy, and limits for web searches."""

from __future__ import annotations

import re
from typing import Literal

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
