"""Tool functions and the dspy.Tool bundle."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Self, cast

from ..utils.dspy import load_tool
from . import (
    _pydoll,
    config,
    errors,
    recording,
)
from . import session as session_module

_default_session = session_module.BrowserSession()


def browser(
    action: config.Action,
    target: config.TextArg | None = None,
    value: config.TextArg | None = None,
    timeout: float = config._DEFAULT_TIMEOUT_S,
) -> dict:
    """Control the headless browser with one action per call.

    The first call starts headless Chrome; later calls reuse the same tab.
    Every action returns a dict with ``status``: ``ok`` or ``error`` (with an
    ``error`` message). Check ``status`` before relying on other fields.

    Actions:
        navigate: ``value`` is the URL. A missing scheme is filled in
            (``example.com`` -> ``https://example.com``; localhost uses http).
            Only http, https, about, and data URLs are allowed by default;
            construct a ``BrowserSession`` with ``allowed_schemes`` or
            ``url_policy`` to change that.
        click: ``target`` is an ``@ref`` from ``browser_snapshot``, a CSS
            selector, an XPath expression, ``css=...``, ``xpath=...``,
            ``text=Exact text``, or ``text*=Partial text``. The click is
            refused, before it happens, when the element is hidden, disabled,
            or covered by an overlay at its centre point. The result reports
            ``changed`` and ``effect`` (``dom``, ``navigation``, or ``none``),
            and a JavaScript fallback is flagged in ``warning``.
        upload: ``target`` is a file input and ``value`` is a local file path.
        type: ``target`` plus ``value`` text. Clears the field first, then
            types. The field is read back and ``verified`` says whether it now
            holds exactly ``value``; a mismatch adds ``actual``, unless the
            field is a password.
        press: ``value`` is a key name (``Enter``, ``Tab``, ``Escape``,
            ``ArrowDown``) or a combination (``Control+A``). Reports
            ``changed``/``effect`` like ``click``.
        hover: ``target`` is moved over with the mouse.
        select: ``target`` is a ``<select>`` and ``value`` is the option value;
            fires ``input`` and ``change`` events, then reports ``verified``.
        scroll: ``value`` is ``down``, ``up``, ``top``, ``bottom``, or a pixel
            count (positive scrolls down, negative up).
        back / forward / reload: navigate browser history or reload.
        wait: wait for a condition instead of sleeping where possible.
            ``target`` waits for that selector to be visible; ``value``
            accepts ``text=Done`` (or ``text*=Don``) for body text,
            ``url=https://example.com/next`` for a URL prefix, and
            ``selector=#done`` for a selector. A number sleeps that many
            seconds (default 1, capped at 30). A timeout returns
            ``status="error"`` naming what it waited for.
    Every result may also carry diagnostics collected since the previous call:
    ``console`` (page console output, repeats collapsed), ``errors`` (uncaught
    page exceptions), and ``dialog`` (a dialog that opened, with ``handled``).
    Dialogs are answered as they open, so a ``confirm()`` never blocks a step;
    ``dialog_policy="accept"`` on the session confirms them instead.

    ``timeout`` bounds element lookup, page load, and the short settle wait
    after actions that can navigate or load data (seconds, default 15).
    ``target`` and ``value`` also accept numbers, which are converted to text
    (``scroll`` with ``value=300`` scrolls 300 pixels). After navigation or a
    click, call ``browser_snapshot`` to see the new page and get fresh
    ``@refs``; every snapshot clears the previous refs.
    """
    return _default_session.browser(action, target, value, timeout)


def browser_open(
    url: config.TextArg,
    max_nodes: int | str = config._DEFAULT_SNAPSHOT_NODES,
    format: config.SnapshotFormat = config._DEFAULT_SNAPSHOT_FORMAT,
    timeout: float = config._DEFAULT_TIMEOUT_S,
) -> dict:
    """Open ``url`` and return the new page snapshot in one call.

    Combines ``browser("navigate", value=url)`` with ``browser_snapshot``:
    one tool call per URL instead of two, with fresh ``@refs`` for the page.
    ``format`` selects ``nodes`` (default), a compact ``text`` tree, or
    ``both``. Returns the ``browser_snapshot`` payload under ``action="open"``.
    """
    return _default_session.open(url, max_nodes, format, timeout)


def browser_snapshot(
    max_nodes: int | str = config._DEFAULT_SNAPSHOT_NODES,
    format: config.SnapshotFormat = config._DEFAULT_SNAPSHOT_FORMAT,
) -> dict:
    """Return the current page as structured nodes for choosing the next action.

    Each node has ``ref`` (``@e1``), ``role``, ``name`` (accessible name),
    ``tag``, and ``depth``; inputs and links also carry ``value``, ``href``,
    ``checked``, ``disabled``, ``placeholder``, or ``expanded`` when relevant.
    Pass a node's ``ref`` as the ``target`` of ``browser`` to click, type,
    hover, or select it.

    ``format`` selects the output: ``nodes`` (default) returns the structured
    list, ``text`` returns only the cheaper indented tree, and ``both`` returns
    both representations. Returns ``status``, ``url``, ``title``,
    ``node_count``, ``truncated``, and the selected representation(s).
    Interactive elements and landmarks are kept first when ``max_nodes``
    truncates a long page. Every call clears and reassigns refs, so take a
    fresh snapshot after navigation or DOM changes.

    Like every browser call, the result may also carry ``console``, ``errors``,
    and ``dialog`` collected since the previous call.
    """
    return _default_session.snapshot(max_nodes, format)


def browser_tabs(
    action: str = "list",
    value: config.TextArg | None = None,
    timeout: float = config._DEFAULT_TIMEOUT_S,
) -> dict:
    """List, open, switch, or close browser tabs.

    ``action`` is one of ``list``, ``new``, ``switch``, or ``close``. The
    active tab is the one every other browser tool acts on.

    - ``list``: every page tab, oldest first, with ``index``, ``url``,
      ``title``, and ``active``.
    - ``new``: opens a tab; ``value`` is an optional URL to load in it, which
      makes the new tab active.
    - ``switch``: makes a tab active. ``value`` is an ``index`` from the list,
      or a substring of its URL or title; an ambiguous match fails with the
      candidates instead of guessing.
    - ``close``: closes the tab named by ``value`` (default: the active one)
      and activates the newest remaining tab. Closing the last tab stops the
      browser; the next call starts a fresh one.

    Returns ``status``, ``action``, ``count``, ``tabs``, and ``active``.
    ``closed_all`` is set when the last tab was closed.
    """
    return _default_session.tabs(action, value, timeout)


def browser_cookies(
    action: str = "get",
    value: config.TextArg | None = None,
) -> dict:
    """Read, write, clear, save, or load cookies for the current browser context.

    ``get`` returns ``name``, ``value``, ``domain``, ``path``, ``expires``,
    ``httpOnly``, ``secure``, and ``sameSite`` for each cookie; ``value``
    filters by name or domain substring. ``set`` accepts a JSON list of cookie
    objects or a raw ``Cookie:`` header such as ``a=1; b=2``; cookies without a
    ``domain`` or ``url`` are scoped to the current page. ``clear`` removes
    every cookie in the context.

    ``save`` writes ``{"url": ..., "cookies": [...]}`` to ``value`` and
    ``load`` restores that file, which is how a login survives a fresh browser
    when a persistent profile is not in use. ``load`` re-checks nothing: only
    load files you produced.

    Returns ``status``, ``action``, and ``count``, plus ``cookies`` for
    ``get`` and ``path`` for ``save``.
    """
    return _default_session.cookies(action, value)


def browser_network(
    action: str = "requests",
    value: config.TextArg | None = None,
    limit: int | str = 20,
) -> dict:
    """Inspect the requests the page made.

    Network events are recorded for every tab from the moment the browser
    starts, so this answers "did the request go out, and what came back"
    without a second click.

    - ``requests``: the newest ``limit`` requests, oldest first, each with
      ``id``, ``method``, ``url``, ``type``, ``status``, ``mime``, and
      ``failed`` when the load errored. ``value`` filters by URL substring or
      exact request ``id``.
    - ``body``: the response body for one ``id``, truncated to 4000 characters
      with ``truncated`` set when it was cut.
    - ``clear``: forgets the recorded requests, so the next action's requests
      are easy to spot.

    Returns ``status``, ``action``, ``count``, and ``requests`` (or ``body``).
    """
    return _default_session.network(action, value, limit)


def browser_screenshot(
    path: str | None = None,
    full_page: bool = False,
    quality: int | str = config._DEFAULT_SCREENSHOT_QUALITY,
    as_base64: bool = False,
) -> dict:
    """Capture the current page and return where the image was written.

    Without ``path``, the PNG is saved under the system temp directory and the
    returned ``path`` is absolute. A ``path`` without a suffix gets ``.png``;
    ``.png``, ``.jpg``/``.jpeg``, and ``.webp`` are supported. ``full_page``
    captures beyond the viewport. ``quality`` applies to JPEG/WebP (0-100).

    Returns ``status``, ``path``, ``format``, and ``bytes``. With
    ``as_base64=True`` the image is returned in ``base64`` instead of a file.
    """
    return _default_session.screenshot(path, full_page, quality, as_base64)


def browser_evaluate(script: config.TextArg, await_promise: bool = True) -> dict:
    """Run JavaScript in the current page and return its value.

    Use a ``return`` statement: ``return document.title``. The result is
    converted by value, so objects and arrays come back as Python dicts and
    lists. ``await_promise`` waits for a returned promise. Exceptions in the
    script become ``status="error"`` with the JavaScript message.

    Returns ``status``, ``result``, ``type``, and the current ``url``/``title``.
    """
    return _default_session.evaluate(script, await_promise)


def browser_record_start(
    path: str | None = None,
    fps: float = config._DEFAULT_RECORDING_FPS,
    quality: int | str = config._DEFAULT_SCREENSHOT_QUALITY,
    max_width: int | str | None = None,
    max_height: int | str | None = None,
    show_cursor: bool = False,
) -> dict:
    """Start recording the current browser tab to an MP4 or WebM video.

    The recording uses Chrome's CDP screencast: frames are captured only when
    the page changes, acked immediately, and encoded with ``ffmpeg`` when
    ``browser_record_stop`` is called. ``fps`` caps how many frames per second
    are kept (1-60); ``quality`` applies to the intermediate JPEG frames.
    ``max_width`` and ``max_height`` downscale the capture when given.
    ``show_cursor`` draws a cursor and click indicator in the recorded tab.

    Without ``path``, an MP4 is written under the system temp directory.
    ``.mp4`` uses H.264 and ``.webm`` uses VP9; other suffixes are rejected.
    ``ffmpeg`` comes from PATH, or from the optional ``[ffmpeg]`` extra when
    PATH has none. The recording is video-only: page audio is not captured.

    Returns ``status``, ``path``, ``fps``, ``quality``, ``max_width``, and
    ``max_height``. Stop with ``browser_record_stop``; ``browser_close`` also
    stops and encodes an active recording.
    """
    return recording._record_start(
        _default_session, path, fps, quality, max_width, max_height, show_cursor
    )


def browser_record_stop() -> dict:
    """Stop the active recording and encode its frames into the video file.

    Returns ``status``, ``path``, ``format``, ``bytes``, ``frames``, and
    ``duration_s``. Fails when no recording is active or when ``ffmpeg``
    cannot encode the captured frames.
    """
    return recording._record_stop(_default_session)


def _close_payload(session: session_module.BrowserSession) -> dict:
    try:
        closed = session.close()
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return errors._error("close", exc)
    payload: dict[str, Any] = {
        "status": "ok",
        "action": "close",
        "closed": closed,
    }
    recording = session.take_last_recording()
    if recording is not None:
        payload["recording"] = recording
    return payload


def browser_close() -> dict:
    """Stop this browser session early and release Chrome and its profile.

    Optional: the browser also stops at interpreter exit, and the next tool
    call after this starts a fresh one. Returns ``closed``: whether a browser
    was running. If a recording was active, it is stopped and encoded, and
    its result is included under ``recording``.
    """
    return _close_payload(_default_session)


def browser_wait_response(url: str, timeout: float = 15) -> dict:
    """Wait for a response or failure matching a URL substring or request id.

    Searches retained requests first. Call browser_network("clear") before the
    triggering action when an older matching response should not satisfy it.
    """
    return _default_session.wait_response(url, timeout)


def browser_pdf(path: str, width: float = 8.5, height: float = 11.0) -> dict:
    """Print the current page to a PDF at path; paper dimensions are inches."""
    return _default_session.pdf(path, width, height)


def browser_save_tabs(path: str) -> dict:
    """Save open tab URLs and the active tab to a JSON file."""
    return _default_session.save_tabs(path)


def browser_restore_tabs(path: str) -> dict:
    """Open tabs from a previously saved JSON file alongside existing tabs."""
    return _default_session.restore_tabs(path)


def _bind(
    function: Callable[..., Any], method: Callable[..., Any]
) -> Callable[..., Any]:
    @functools.wraps(function)
    def bound(*args: Any, **kwargs: Any) -> Any:
        return method(*args, **kwargs)

    return bound


def _bind_close(session: session_module.BrowserSession) -> Callable[..., Any]:
    @functools.wraps(browser_close)
    def bound() -> dict:
        return _close_payload(session)

    return bound


class BrowserTools(list):
    """List of ``dspy.Tool`` objects covering one browser session.

    Behaves like a normal list, so it can be passed directly as ``tools=``,
    concatenated with other tools, unpacked, or wrapped in ``Toolbox``.
    :meth:`close` stops this list's browser session; the tools themselves
    also clean up at interpreter exit.
    """

    def __init__(
        self,
        tools: Iterable[Any] = (),
        *,
        session: session_module.BrowserSession | None = None,
    ) -> None:
        super().__init__(tools)
        self.session = session if session is not None else _default_session

    def close(self) -> dict[str, Any]:
        """Stop this list's browser session and return the close payload."""
        return _close_payload(self.session)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _build_tools(
    session: session_module.BrowserSession,
    *,
    include_search: bool = False,
    include_recording: bool = False,
    show_cursor: bool = False,
) -> BrowserTools:
    _pydoll.require()
    Tool = load_tool(
        "dspy_agent_tools.Browser requires DSPy. Install it with: uv add dspy"
    )
    functions: list[Callable[..., Any]] = [
        _bind(browser_open, session.open),
        _bind(browser, session.browser),
        _bind(browser_snapshot, session.snapshot),
        _bind(browser_tabs, session.tabs),
        _bind(browser_network, session.network),
        _bind(browser_wait_response, session.wait_response),
        _bind(browser_pdf, session.pdf),
        _bind(browser_save_tabs, session.save_tabs),
        _bind(browser_restore_tabs, session.restore_tabs),
        _bind(browser_cookies, session.cookies),
        _bind(browser_screenshot, session.screenshot),
        _bind(browser_evaluate, session.evaluate),
    ]
    if include_recording:
        record_start = _bind(browser_record_start, session.record_start)
        if show_cursor:
            signature = inspect.signature(browser_record_start)
            parameters = [
                parameter.replace(default=True)
                if parameter.name == "show_cursor"
                else parameter
                for parameter in signature.parameters.values()
            ]

            @functools.wraps(browser_record_start)
            def record_start(*args: Any, **kwargs: Any) -> dict:
                if len(args) < 6:
                    kwargs.setdefault("show_cursor", True)
                return session.record_start(*args, **kwargs)

            cast(Any, record_start).__signature__ = signature.replace(
                parameters=parameters
            )
        functions.extend(
            [
                record_start,
                _bind(browser_record_stop, session.record_stop),
            ]
        )
    functions.append(_bind_close(session))
    if include_search:
        from ..search import web_search

        functions.append(web_search)
    return BrowserTools((Tool(function) for function in functions), session=session)


def Browser(
    *,
    session: session_module.BrowserSession | None = None,
    include_search: bool = False,
    include_recording: bool = False,
    show_cursor: bool = False,
    headless: bool | None = None,
    timeout: float | None = None,
    dialog_policy: config.DialogPolicy | None = None,
    user_data_dir: str | Path | None = None,
    cookie_state: str | Path | None = None,
    allowed_schemes: Iterable[str] | None = None,
    url_policy: Callable[[str], bool] | None = None,
    humanize: bool | None = None,
    stealth: bool | None = None,
    fingerprint: dict[str, Any] | None = None,
    ws_address: str | None = None,
) -> BrowserTools:
    """Return the browser tools as a ready-to-use list of ``dspy.Tool`` objects.

    The default list contains ``browser_open``, ``browser``,
    ``browser_snapshot``, ``browser_tabs``, ``browser_network``,
    ``browser_cookies``, ``browser_screenshot``, ``browser_evaluate``, and
    ``browser_close``. Pass ``include_recording=True`` to add
    ``browser_record_start`` and ``browser_record_stop``, and
    ``include_search=True`` to append ``web_search`` for research agents that
    need both search and a real browser.
    ``show_cursor=True`` makes cursor overlays the default for recordings;
    each call to ``browser_record_start`` can override it.

    Each call creates an isolated session; module-level functions retain their
    own shared session. Pass ``session=BrowserSession(...)`` to bind every
    tool, including ``browser_close`` and :meth:`BrowserTools.close`, to an
    isolated per-user or per-request session.

    The remaining keywords are forwarded to a session this call creates, so a
    one-line setup reaches the same options::

        agent = dspy.ReAct("task -> result", tools=Browser(user_data_dir="./profile"))

    ``user_data_dir`` keeps cookies, storage, and history across runs;
    ``cookie_state`` keeps just the cookies in a JSON file that is loaded at
    startup and rewritten whenever they change or the browser closes, which
    works without a profile; ``dialog_policy`` picks ``dismiss`` (default) or
    ``accept`` for page dialogs; ``headless``, ``timeout``,
    ``allowed_schemes``, and ``url_policy`` match :class:`BrowserSession`.
    ``humanize`` uses pydoll humanized input. ``stealth`` also enables it;
    ``fingerprint`` supplies an optional pydoll fingerprint profile. URL
    filters apply only to explicit navigation, not all browser requests.
    Passing any of them together with ``session=`` raises ``ValueError``,
    because the session would be built twice.
    """
    options: dict[str, Any] = {}
    if headless is not None:
        options["headless"] = headless
    if timeout is not None:
        options["timeout"] = timeout
    if dialog_policy is not None:
        options["dialog_policy"] = dialog_policy
    if user_data_dir is not None:
        options["user_data_dir"] = user_data_dir
    if cookie_state is not None:
        options["cookie_state"] = cookie_state
    if allowed_schemes is not None:
        options["allowed_schemes"] = allowed_schemes
    if url_policy is not None:
        options["url_policy"] = url_policy
    if humanize is not None:
        options["humanize"] = humanize
    if stealth is not None:
        options["stealth"] = stealth
    if fingerprint is not None:
        options["fingerprint"] = fingerprint
    if ws_address is not None:
        options["ws_address"] = ws_address
    if session is not None and options:
        raise ValueError(
            "pass either session= or session options to Browser(), not both"
        )
    if session is not None:
        target_session = session
    else:
        target_session = session_module.BrowserSession(**options)
    return _build_tools(
        target_session,
        include_search=include_search,
        include_recording=include_recording,
        show_cursor=show_cursor,
    )
