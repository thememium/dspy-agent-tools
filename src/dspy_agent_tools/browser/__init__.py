"""Headless browser tools backed by pydoll.

``Browser()`` returns the whole browser toolset as one list of ``dspy.Tool``
objects, so the common case needs a single import::

    import dspy
    from dspy_agent_tools import Browser

    agent = dspy.ReAct("task -> result", tools=Browser())

Pass ``include_recording=True`` to add the video recording tools, and
``include_search=True`` to add ``web_search`` to the same list. The individual
functions remain available for hand-picked tool lists. Chrome
starts on the first call and the same tab is reused afterwards, so callers
never create, enter, or clean up a browser session::

    from dspy_agent_tools import browser, browser_snapshot

    agent = dspy.ReAct(
        "task -> result",
        tools=[browser, browser_snapshot],
    )

``browser`` performs one action, ``browser_snapshot`` returns the current page
as structured nodes or a compact text tree with ``@eN`` refs,
``browser_tabs`` lists, opens, switches, and closes tabs, ``browser_network``
reports what the page fetched, ``browser_cookies`` reads and writes cookies,
``browser_screenshot`` captures an image,
``browser_record_start``/``browser_record_stop`` capture a video,
``browser_evaluate`` runs JavaScript, and ``browser_close`` releases the
browser early. The browser is headless by default and is also stopped at
interpreter exit. Video recording uses ``ffmpeg`` from PATH, or the bundled
binary from the optional ``[ffmpeg]`` extra.

For a server that needs one browser per user or session, bind the tool list to
a ``BrowserSession``::

    session = BrowserSession()
    tools = Browser(session=session)
    ...
    tools.close()

The session defaults to http, https, about, and data URLs. Pass
``allowed_schemes`` or ``url_policy`` to filter explicit navigation requests.
These filters are not a security boundary; use network and OS isolation to
restrict destinations.

Requires the optional ``browser`` extra (pydoll)::

    uv add "dspy-agent-tools[browser]"
"""

from .session import BrowserSession
from .tools import (
    Browser,
    BrowserTools,
    browser,
    browser_close,
    browser_cookies,
    browser_evaluate,
    browser_network,
    browser_open,
    browser_record_start,
    browser_record_stop,
    browser_screenshot,
    browser_snapshot,
    browser_tabs,
)

__all__ = [
    "Browser",
    "BrowserSession",
    "BrowserTools",
    "browser",
    "browser_close",
    "browser_cookies",
    "browser_evaluate",
    "browser_network",
    "browser_open",
    "browser_record_start",
    "browser_record_stop",
    "browser_screenshot",
    "browser_snapshot",
    "browser_tabs",
]
