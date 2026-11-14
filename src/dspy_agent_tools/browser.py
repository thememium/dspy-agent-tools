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
``allowed_schemes`` or ``url_policy`` to ``BrowserSession`` when a server must
block local files, private hosts, or other destinations.

Requires the optional ``browser`` extra (pydoll)::

    uv add "dspy-agent-tools[browser]"
"""

from __future__ import annotations

import atexit
import base64
import functools
import importlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self
from urllib.parse import urlsplit
from uuid import uuid4

if TYPE_CHECKING:
    from dspy import Tool

    # Runtime imports are lazy in _require_pydoll so the package imports
    # without the optional [browser] extra.
    from pydoll.commands import BrowserCommands, PageCommands
    from pydoll.constants import ScrollPosition
    from pydoll.exceptions import (
        BrowserException,
        ConnectionException,
        TargetCrashed,
        TimeoutException,
    )
    from pydoll.protocol.network.events import NetworkEvent
    from pydoll.protocol.page.types import ScreencastFormat
    from pydoll.protocol.runtime.events import RuntimeEvent
    from pydoll.protocol.runtime.types import CallArgument
    from pydoll.sync import Chrome, ChromiumOptions, Key, PageEvent, SyncError

_IMPORT_HINT = (
    "dspy_agent_tools browser tools require pydoll. "
    "Install the browser extra: uv add 'dspy-agent-tools[browser]'"
)
_pydoll_loaded = False


def _require_pydoll() -> None:
    """Import pydoll on first use so the package works without the extra."""
    global _pydoll_loaded
    if _pydoll_loaded:
        return
    try:
        from pydoll.commands import BrowserCommands as _BrowserCommands
        from pydoll.commands import PageCommands as _PageCommands
        from pydoll.constants import ScrollPosition as _ScrollPosition
        from pydoll.exceptions import BrowserException as _BrowserException
        from pydoll.exceptions import ConnectionException as _ConnectionException
        from pydoll.exceptions import TargetCrashed as _TargetCrashed
        from pydoll.exceptions import TimeoutException as _TimeoutException
        from pydoll.protocol.network.events import NetworkEvent as _NetworkEvent
        from pydoll.protocol.page.types import ScreencastFormat as _ScreencastFormat
        from pydoll.protocol.runtime.events import RuntimeEvent as _RuntimeEvent
        from pydoll.protocol.runtime.types import CallArgument as _CallArgument
        from pydoll.sync import Chrome as _Chrome
        from pydoll.sync import ChromiumOptions as _ChromiumOptions
        from pydoll.sync import Key as _Key
        from pydoll.sync import PageEvent as _PageEvent
        from pydoll.sync import SyncError as _SyncError
    except ImportError as exc:
        raise ImportError(_IMPORT_HINT) from exc
    for name, value in {
        "BrowserCommands": _BrowserCommands,
        "PageCommands": _PageCommands,
        "ScrollPosition": _ScrollPosition,
        "BrowserException": _BrowserException,
        "ConnectionException": _ConnectionException,
        "TargetCrashed": _TargetCrashed,
        "ScreencastFormat": _ScreencastFormat,
        "CallArgument": _CallArgument,
        "Chrome": _Chrome,
        "ChromiumOptions": _ChromiumOptions,
        "Key": _Key,
        "NetworkEvent": _NetworkEvent,
        "PageEvent": _PageEvent,
        "RuntimeEvent": _RuntimeEvent,
        "SyncError": _SyncError,
        "TimeoutException": _TimeoutException,
    }.items():
        globals().setdefault(name, value)
    _pydoll_loaded = True


TextArg = str | int | float

Action = Literal[
    "navigate",
    "click",
    "type",
    "press",
    "hover",
    "select",
    "scroll",
    "back",
    "forward",
    "reload",
    "wait",
]
SnapshotFormat = Literal["nodes", "text", "both"]
DialogPolicy = Literal["dismiss", "accept"]

_DEFAULT_TIMEOUT_S = 15.0
_MAX_TIMEOUT_S = 120.0
_MAX_WAIT_S = 30.0
_SETTLE_IDLE_S = 0.2
_SETTLE_TIMEOUT_S = 2.0
_DEFAULT_SNAPSHOT_FORMAT: SnapshotFormat = "nodes"
_DEFAULT_ALLOWED_SCHEMES = frozenset({"http", "https", "about", "data"})
_MAX_SNAPSHOT_NODES = 1000
_DEFAULT_SNAPSHOT_NODES = 400
_MAX_NAME_CHARS = 300
_DEFAULT_SCROLL_PX = 600
_DEFAULT_SCREENSHOT_QUALITY = 80
_DEFAULT_RECORDING_FPS = 10.0
_MAX_RECORDING_FPS = 60.0
_VIDEO_SUFFIXES = (".mp4", ".webm")
_REF_ATTR = "data-dspy-agent-ref"
_DEFAULT_WINDOW_SIZE = (1440, 900)
_DEFAULT_DIALOG_POLICY: DialogPolicy = "dismiss"
_MAX_DIALOG_CHARS = 200
_MAX_CONSOLE_ENTRIES = 200
_MAX_CONSOLE_ITEMS = 8
_MAX_CONSOLE_TEXT = 300
_PROFILE_FLUSH_S = 0.5
_MAX_NETWORK_ENTRIES = 300
_MAX_NETWORK_ITEMS = 100
_MAX_NETWORK_BODY = 4000
_MAX_COOKIE_ITEMS = 200
_COOKIE_FIELDS = (
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
)
_NO_EFFECT_WARNING = (
    "the page did not change; the target may be inert, or its effect is not "
    "visible in the DOM"
)
_ACTIONS = (
    "navigate",
    "click",
    "type",
    "press",
    "hover",
    "select",
    "scroll",
    "back",
    "forward",
    "reload",
    "wait",
)
_TAB_ACTIONS = frozenset({"click", "press", "navigate"})
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_REF_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)$")
_KEY_ALIASES = {
    "CTRL": "CONTROL",
    "CMD": "META",
    "COMMAND": "META",
    "ESC": "ESCAPE",
    "RETURN": "ENTER",
    "DEL": "DELETE",
}
_TEXT_XPATH = (
    "self::a or self::button or self::input or self::textarea or self::select "
    "or self::summary or @role='button' or @role='link' or @role='tab' "
    "or @role='menuitem'"
)

logger = logging.getLogger(__name__)

_SNAPSHOT_JS = r"""
return (
  function () {
  const MAX_NODES = __MAX_NODES__;
  const MAX_NAME = __MAX_NAME__;
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "META", "LINK", "HEAD", "TITLE"]);
  const TEXT_TAGS = new Set(["H1","H2","H3","H4","H5","H6","P","LI","TD","TH","PRE","CODE","BLOCKQUOTE","DD","DT","FIGCAPTION","LABEL","IMG"]);
  const LANDMARK_ROLES = new Set(["main","navigation","banner","contentinfo","complementary","search","form","region"]);
  const INTERACTIVE_TAGS = new Set(["A","BUTTON","INPUT","SELECT","TEXTAREA","SUMMARY","DETAILS"]);
  const INTERACTIVE_ROLES = new Set(["button","link","checkbox","radio","tab","menuitem","menuitemcheckbox","menuitemradio","option","switch","textbox","searchbox","combobox","slider","spinbutton"]);

  for (const el of document.querySelectorAll("[data-dspy-agent-ref]")) {
    el.removeAttribute("data-dspy-agent-ref");
  }

  const candidates = [];
  let order = 0;

  function clean(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  function isVisible(el) {
    if (typeof el.checkVisibility === "function") {
      try {
        if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
      } catch (e) {}
    }
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
    if (parseFloat(style.opacity) === 0) return false;
    return true;
  }

  function accessibleName(el) {
    const aria = clean(el.getAttribute("aria-label"));
    if (aria) return aria;
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/).map(id => {
        const target = document.getElementById(id);
        return target ? clean(target.innerText || target.textContent) : "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    if (el.tagName === "IMG") return clean(el.getAttribute("alt"));
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      if (el.labels && el.labels.length) {
        const label = Array.from(el.labels).map(node => clean(node.innerText || node.textContent)).filter(Boolean).join(" ");
        if (label) return label;
      }
      if (el.tagName === "SELECT") {
        const selected = el.selectedOptions && el.selectedOptions[0];
        if (selected) return clean(selected.textContent);
      }
      return clean(el.getAttribute("placeholder") || el.getAttribute("name") || el.getAttribute("title"));
    }
    return clean(el.innerText || el.textContent || el.getAttribute("title"));
  }

  function roleOf(el) {
    const explicit = clean(el.getAttribute("role"));
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === "A") return el.hasAttribute("href") ? "link" : "generic";
    if (tag === "BUTTON" || tag === "SUMMARY") return "button";
    if (tag === "INPUT") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "submit" || type === "button" || type === "reset") return "button";
      if (type === "search") return "searchbox";
      return "textbox";
    }
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "SELECT") return "combobox";
    if (tag === "IMG") return "img";
    if (tag === "H1" || tag === "H2" || tag === "H3" || tag === "H4" || tag === "H5" || tag === "H6") return "heading";
    if (tag === "NAV") return "navigation";
    if (tag === "MAIN") return "main";
    if (tag === "FORM") return "form";
    if (tag === "TABLE") return "table";
    if (tag === "UL" || tag === "OL") return "list";
    if (tag === "LI") return "listitem";
    if (tag === "P") return "paragraph";
    if (tag === "PRE" || tag === "CODE") return "code";
    return "generic";
  }

  function isInteractive(el) {
    if (INTERACTIVE_TAGS.has(el.tagName)) return true;
    if (INTERACTIVE_ROLES.has(roleOf(el))) return true;
    return el.hasAttribute("onclick") || el.hasAttribute("tabindex") || el.isContentEditable;
  }

  function isLandmark(el) {
    return LANDMARK_ROLES.has(roleOf(el)) || ["NAV","MAIN","HEADER","FOOTER","ASIDE","FORM"].includes(el.tagName);
  }

  function collect(root, depth) {
    for (const el of root.children) {
      if (SKIP.has(el.tagName) || !isVisible(el)) continue;
      const tag = el.tagName;
      const interactive = isInteractive(el);
      const landmark = isLandmark(el);
      const textTag = TEXT_TAGS.has(tag);
      const name = (interactive || landmark || textTag) ? accessibleName(el) : "";
      if ((interactive || landmark || textTag) && (interactive || landmark || name)) {
        candidates.push({
          el,
          depth,
          order: order++,
          interactive,
          landmark,
          role: roleOf(el),
          name: name.slice(0, MAX_NAME),
          tag: tag.toLowerCase(),
        });
      }
      collect(el, depth + 1);
    }
  }

  collect(document.body || document.documentElement, 0);

  const selected = candidates
    .slice()
    .sort((a, b) => {
      const ap = (a.interactive || a.landmark || a.role === "heading") ? 0 : 1;
      const bp = (b.interactive || b.landmark || b.role === "heading") ? 0 : 1;
      return ap - bp || a.order - b.order;
    })
    .slice(0, MAX_NODES)
    .sort((a, b) => a.order - b.order);

  const nodes = selected.map((candidate, index) => {
    const ref = "e" + (index + 1);
    candidate.el.setAttribute("data-dspy-agent-ref", ref);
    const node = {
      ref: "@" + ref,
      role: candidate.role,
      name: candidate.name,
      tag: candidate.tag,
      depth: candidate.depth,
    };
    if (candidate.el.value !== undefined && ["input", "textarea", "select"].includes(candidate.tag)) {
      node.value = String(candidate.el.value).slice(0, 200);
    }
    if (candidate.tag === "a" && candidate.el.getAttribute("href")) {
      node.href = candidate.el.getAttribute("href");
    }
    if (candidate.tag === "input") {
      node.input_type = (candidate.el.getAttribute("type") || "text").toLowerCase();
      if (candidate.el.checked) node.checked = true;
    }
    if (candidate.el.disabled) node.disabled = true;
    const placeholder = candidate.el.getAttribute("placeholder");
    if (placeholder) node.placeholder = placeholder;
    const expanded = candidate.el.getAttribute("aria-expanded");
    if (expanded !== null) node.expanded = expanded === "true";
    return node;
  });

  return {
    url: location.href,
    title: document.title,
    nodes,
    truncated: candidates.length > selected.length,
  };
  }
)();
"""


_PROBE_JS = r"""
return (
  function () {
  const existing = window.__dspyAgentProbe;
  if (existing && existing.observer) {
    return { url: location.href, mutations: existing.mutations };
  }
  const probe = { mutations: 0, observer: null };
  try {
    probe.observer = new MutationObserver(function (records) {
      probe.mutations += records.length;
    });
    probe.observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
  } catch (error) {
    probe.observer = null;
  }
  window.__dspyAgentProbe = probe;
  return { url: location.href, mutations: 0 };
  }
)();
"""

_WAIT_SELECTOR_JS = r"""
return (
  function (selector) {
  let element = null;
  try {
    element = selector.startsWith("/") || selector.startsWith("(")
      ? document.evaluate(selector, document, null, 9, null).singleNodeValue
      : document.querySelector(selector);
  } catch (error) {
    return false;
  }
  if (!element) return false;
  const rect = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  return (
    rect.width > 0 &&
    rect.height > 0 &&
    style.display !== "none" &&
    style.visibility !== "hidden" &&
    parseFloat(style.opacity) > 0
  );
  }
)(__SELECTOR__);
"""

_WAIT_TEXT_JS = r"""
return (
  function (text) {
  const body = document.body;
  if (!body) return false;
  const haystack = body.innerText || body.textContent || "";
  return haystack.indexOf(text) !== -1;
  }
)(__TEXT__);
"""

_WAIT_URL_JS = r"""
return location.href.indexOf(__PREFIX__) === 0;
"""

_FIELD_STATE_JS = r"""
const el = this;
return (
  function () {
  const raw = el.value;
  const value = raw === undefined || raw === null ? null : String(raw);
  return {
    tag: el.tagName ? el.tagName.toLowerCase() : "",
    value: value,
    text: value === null ? (el.textContent || "").slice(0, __MAX_TEXT__) : null,
    type: el.getAttribute ? (el.getAttribute("type") || "").toLowerCase() : "",
    disabled: !!el.disabled,
    readonly: !!el.readOnly,
  };
  }
)();
"""


class _ToolError(RuntimeError):
    """A caller-correctable error, returned as ``status="error"``."""


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, _ToolError):
        return str(exc)
    message = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message and message != name else name


def _error(action: str, exc: BaseException, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "action": action,
        "error": _format_error(exc),
    }
    payload.update(extra)
    return payload


def _console_text(args: Any) -> str:
    """Flatten one ``Runtime.consoleAPICalled`` argument list into text."""
    if not isinstance(args, list):
        return ""
    parts: list[str] = []
    for arg in args:
        if not isinstance(arg, dict):
            parts.append(str(arg))
            continue
        if "value" in arg:
            value = arg["value"]
            if isinstance(value, str):
                parts.append(value)
            elif value is None:
                parts.append("null")
            else:
                parts.append(json.dumps(value, ensure_ascii=False, default=str))
        elif arg.get("description"):
            parts.append(str(arg["description"]))
        elif arg.get("type"):
            parts.append(f"<{arg['type']}>")
    return " ".join(parts)[:_MAX_CONSOLE_TEXT]


def _compress(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeats and keep the most recent entries."""
    merged: list[dict[str, Any]] = []
    for entry in entries:
        if merged and merged[-1]["text"] == entry["text"]:
            merged[-1]["count"] += 1
            continue
        merged.append({**entry, "count": 1})
    for entry in merged:
        if entry["count"] == 1:
            del entry["count"]
    return merged[-_MAX_CONSOLE_ITEMS:]


class _PageWatch:
    """Console output, page errors, and JavaScript dialogs for one tab.

    pydoll delivers these events on its own loop, so the callbacks only append
    to bounded deques; each tool call drains what arrived since the previous
    one.

    A dialog blocks the page, and the command that opened it (a click, say)
    does not return until the dialog is answered. ``responder`` therefore runs
    on a short-lived thread: the callback only records the dialog, and the
    thread answers it while the blocked call waits.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._answers_lock = threading.Lock()
        self._answers_done = threading.Condition(self._answers_lock)
        self._answers = 0
        self._console: deque[tuple[int, dict[str, Any]]] = deque(
            maxlen=_MAX_CONSOLE_ENTRIES
        )
        self._errors: deque[tuple[int, dict[str, Any]]] = deque(
            maxlen=_MAX_CONSOLE_ENTRIES
        )
        self._dialogs: deque[tuple[int, dict[str, Any]]] = deque(
            maxlen=_MAX_CONSOLE_ENTRIES
        )
        self._console_seen = 0
        self._errors_seen = 0
        self._dialogs_seen = 0
        self._next_seq = 0
        self._network: dict[str, dict[str, Any]] = {}
        self._network_order: deque[str] = deque()
        self.callback_ids: list[int] = []

    def _seq(self) -> int:
        self._next_seq += 1
        return self._next_seq

    def on_console(self, event: dict) -> None:
        try:
            params = event.get("params") or {}
            text = _console_text(params.get("args"))
            if not text:
                return
            entry = {"level": str(params.get("type") or "log"), "text": text}
            with self._lock:
                self._console.append((self._seq(), entry))
        except Exception as exc:  # noqa: BLE001 - one event must not stop the collector
            logger.debug("Could not record a console event: %s", _format_error(exc))

    def on_exception(self, event: dict) -> None:
        try:
            params = event.get("params") or {}
            details = params.get("exceptionDetails") or {}
            exception = details.get("exception") or {}
            text = exception.get("description") or details.get("text") or ""
            text = " ".join(str(text).split())[:_MAX_CONSOLE_TEXT]
            if not text:
                return
            with self._lock:
                self._errors.append((self._seq(), {"text": text}))
        except Exception as exc:  # noqa: BLE001 - one event must not stop the collector
            logger.debug("Could not record a page error: %s", _format_error(exc))

    def on_request(self, event: dict) -> None:
        try:
            params = event.get("params") or {}
            request = params.get("request") or {}
            request_id = str(params.get("requestId") or "")
            if not request_id:
                return
            entry = {
                "id": request_id,
                "method": str(request.get("method") or ""),
                "url": str(request.get("url") or "")[:300],
                "type": str(params.get("type") or ""),
            }
            with self._lock:
                known = self._network.get(request_id)
                if known is not None:
                    known.update(entry)
                    return
                self._network[request_id] = entry
                self._network_order.append(request_id)
                while len(self._network_order) > _MAX_NETWORK_ENTRIES:
                    self._network.pop(self._network_order.popleft(), None)
        except Exception as exc:  # noqa: BLE001 - one event must not stop the collector
            logger.debug("Could not record a request: %s", _format_error(exc))

    def on_response(self, event: dict) -> None:
        try:
            params = event.get("params") or {}
            response = params.get("response") or {}
            request_id = str(params.get("requestId") or "")
            with self._lock:
                entry = self._network.get(request_id)
                if entry is None:
                    return
                entry["status"] = response.get("status")
                entry["mime"] = str(response.get("mimeType") or "")[:80]
                if response.get("fromDiskCache") or response.get("fromServiceWorker"):
                    entry["cached"] = True
        except Exception as exc:  # noqa: BLE001 - one event must not stop the collector
            logger.debug("Could not record a response: %s", _format_error(exc))

    def on_failed(self, event: dict) -> None:
        try:
            params = event.get("params") or {}
            request_id = str(params.get("requestId") or "")
            with self._lock:
                entry = self._network.get(request_id)
                if entry is None:
                    return
                entry["failed"] = str(params.get("errorText") or "failed")[:120]
        except Exception as exc:  # noqa: BLE001 - one event must not stop the collector
            logger.debug("Could not record a failure: %s", _format_error(exc))

    def network(self, limit: int, needle: str | None) -> list[dict[str, Any]]:
        """Return the newest recorded requests, oldest first."""
        with self._lock:
            entries = [
                dict(self._network[request_id])
                for request_id in self._network_order
                if request_id in self._network
            ]
        if needle:
            entries = [
                entry
                for entry in entries
                if needle in entry.get("url", "") or needle == entry.get("id")
            ]
        return entries[-limit:]

    def clear_network(self) -> None:
        with self._lock:
            self._network.clear()
            self._network_order.clear()

    def on_dialog(
        self, event: dict, responder: Callable[[dict[str, Any]], str]
    ) -> None:
        params = event.get("params") or {}
        entry: dict[str, Any] = {
            "type": str(params.get("type") or "alert"),
            "message": str(params.get("message") or "")[:_MAX_DIALOG_CHARS],
            "handled": "pending",
        }
        prompt = params.get("defaultPrompt")
        if prompt:
            entry["default_prompt"] = str(prompt)[:_MAX_DIALOG_CHARS]
        with self._lock:
            self._dialogs.append((self._seq(), entry))
        with self._answers_lock:
            self._answers += 1
        threading.Thread(
            target=self._answer, args=(responder, entry), daemon=True
        ).start()

    def _answer(
        self, responder: Callable[[dict[str, Any]], str], entry: dict[str, Any]
    ) -> None:
        """Answer one dialog off the event loop; the blocked call then returns."""
        try:
            handled = responder(entry)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            handled = f"failed: {_format_error(exc)}"
        with self._answers_lock:
            entry["handled"] = handled
            self._answers -= 1
            self._answers_done.notify_all()

    def take(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
        """Return console entries, page errors, and the latest dialog since last time."""
        with self._answers_lock:
            while self._answers:
                self._answers_done.wait()
        with self._lock:
            console = _compress(
                [entry for seq, entry in self._console if seq > self._console_seen]
            )
            errors = _compress(
                [entry for seq, entry in self._errors if seq > self._errors_seen]
            )
            dialogs = [
                dict(entry) for seq, entry in self._dialogs if seq > self._dialogs_seen
            ]
            if self._console:
                self._console_seen = self._console[-1][0]
            if self._errors:
                self._errors_seen = self._errors[-1][0]
            if self._dialogs:
                self._dialogs_seen = self._dialogs[-1][0]
            return console, errors, dialogs[-1] if dialogs else None


def _close_browser_gracefully(browser: Any) -> None:
    """Ask Chrome to exit on its own so a persistent profile flushes to disk.

    pydoll ends the process as soon as ``Browser.close`` returns, which leaves
    the cookie database in a journal and loses the login the profile was meant
    to keep. Waiting here gives Chrome the moment it needs to commit.
    """
    try:
        browser.execute_command(BrowserCommands.close(), timeout=3)
    except Exception:  # noqa: BLE001 - the process is stopped either way
        return
    time.sleep(_PROFILE_FLUSH_S)


def _is_disconnected(exc: BaseException) -> bool:
    return isinstance(
        exc, (BrowserException, ConnectionException, TargetCrashed, SyncError)
    )


def _page_info(tab: Any) -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        info["url"] = tab.current_url()
    except Exception:  # noqa: BLE001, S110 - page metadata is optional
        pass
    try:
        info["title"] = tab.title()
    except Exception:  # noqa: BLE001, S110 - page metadata is optional
        pass
    return info


def _coerce_timeout(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise _ToolError("timeout must be a number of seconds") from exc
    if seconds <= 0:
        raise _ToolError("timeout must be greater than zero")
    return min(seconds, _MAX_TIMEOUT_S)


def _coerce_seconds(value: str | None, default: float) -> float:
    """Coerce an already validated seconds value; ``_wait`` checks the shape."""
    if value is None or value == "":
        return default
    seconds = float(value)
    if seconds < 0:
        raise _ToolError("wait seconds cannot be negative")
    return min(seconds, _MAX_WAIT_S)


def _coerce_quality(value: int | str) -> int:
    try:
        quality = int(value)
    except (TypeError, ValueError) as exc:
        raise _ToolError("quality must be an integer from 0 to 100") from exc
    if not 0 <= quality <= 100:
        raise _ToolError("quality must be an integer from 0 to 100")
    return quality


def _coerce_fps(value: float | str) -> float:
    try:
        fps = float(value)
    except (TypeError, ValueError) as exc:
        raise _ToolError("fps must be a number from 1 to 60") from exc
    if not 1 <= fps <= _MAX_RECORDING_FPS:
        raise _ToolError("fps must be a number from 1 to 60")
    return fps


def _optional_dimension(value: int | str | None, name: str) -> int | None:
    if value is None:
        return None
    try:
        dimension = int(value)
    except (TypeError, ValueError) as exc:
        raise _ToolError(f"{name} must be a positive integer") from exc
    if dimension <= 0:
        raise _ToolError(f"{name} must be a positive integer")
    return dimension


def _video_path(path: str | None) -> Path:
    if path:
        target = Path(path).expanduser()
        if target.is_dir():
            raise _ToolError("video path must be a file, not a directory")
        suffix = target.suffix.lower()
        if not suffix:
            target = target.with_suffix(".mp4")
        elif suffix not in _VIDEO_SUFFIXES:
            raise _ToolError("video path must end with .mp4 or .webm")
        else:
            target = target.with_suffix(suffix)
        return target.absolute()
    directory = Path(tempfile.gettempdir()) / "dspy-agent-tools"
    return directory / f"browser-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.mp4"


def _ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if binary is not None:
        return binary
    try:
        imageio_ffmpeg = importlib.import_module("imageio_ffmpeg")
    except ImportError as exc:
        raise _ToolError(
            "video recording requires ffmpeg on PATH or the optional extra: "
            "uv add 'dspy-agent-tools[ffmpeg]'"
        ) from exc
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise _ToolError(
            f"could not locate an ffmpeg binary: {_format_error(exc)}"
        ) from exc


def _text_arg(value: TextArg | None) -> str | None:
    """Tool arguments arrive as JSON; a number where text is expected becomes its text."""
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _normalize_url(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _ToolError("navigate requires value=URL")
    url = value.strip()
    if url.startswith("//"):
        return "https:" + url
    if _SCHEME_RE.match(url) or url.startswith(
        ("about:", "data:", "file:", "mailto:", "chrome:", "view-source:")
    ):
        return url
    if url.startswith(("localhost", "127.", "0.0.0.0", "[::1]")):
        return "http://" + url
    return "https://" + url


def _xpath_literal(text: str) -> str:
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in parts) + ")"


def _text_xpath(text: str, *, contains: bool) -> str:
    literal = _xpath_literal(text)
    predicate = (
        f"contains(normalize-space(.), {literal})"
        if contains
        else f"normalize-space(.) = {literal}"
    )
    return f"(//*[{predicate} and ({_TEXT_XPATH})])[1]"


def _selector(target: str | None) -> str:
    if not isinstance(target, str) or not target.strip():
        raise _ToolError(
            "This action requires target=... (an @ref, CSS, or XPath selector)"
        )
    target = target.strip()
    if target.startswith("@"):
        ref = target[1:]
        if not _REF_RE.fullmatch(ref):
            raise _ToolError(
                f"Invalid element ref {target!r}; use an @ref from browser_snapshot"
            )
        return f'[{_REF_ATTR}="{ref}"]'
    if target.startswith("ref="):
        ref = target[4:].strip()
        if not _REF_RE.fullmatch(ref):
            raise _ToolError(
                f"Invalid element ref {target!r}; use an @ref from browser_snapshot"
            )
        return f'[{_REF_ATTR}="{ref}"]'
    if target.startswith("css="):
        value = target[4:].strip()
        if not value:
            raise _ToolError("css= requires a CSS selector")
        return value
    if target.startswith("xpath="):
        value = target[6:].strip()
        if not value:
            raise _ToolError("xpath= requires an XPath expression")
        return value
    if target.startswith("text*="):
        value = target[6:].strip()
        if not value:
            raise _ToolError("text*= requires text to match")
        return _text_xpath(value, contains=True)
    if target.startswith("text="):
        value = target[5:].strip()
        if not value:
            raise _ToolError("text= requires text to match")
        return _text_xpath(value, contains=False)
    return target


def _element(tab: Any, target: str | None, timeout: float) -> Any:
    selector = _selector(target)
    element = tab.query(selector, timeout=max(1, int(timeout)), raise_exc=False)
    if element is None:
        raise _ToolError(
            f"No element matched {target!r}. Call browser_snapshot for fresh @refs, "
            "or pass a CSS/XPath selector."
        )
    return element


def _probe_bool(element: Any, name: str) -> bool:
    """Read one boolean element probe; a probe that cannot run is not a failure."""
    probe = getattr(element, name, None)
    if probe is None:
        return True
    try:
        return bool(probe() if callable(probe) else probe)
    except Exception:  # noqa: BLE001 - a probe that cannot run must not block
        return True


def _require_actionable(element: Any, action: str) -> None:
    """Refuse an action a user could not perform, before it silently no-ops."""
    if not _probe_bool(element, "is_visible"):
        raise _ToolError(
            "target is not visible; it may be hidden, zero-size, or scrolled away"
        )
    if action == "hover":
        return
    if not _probe_bool(element, "is_enabled"):
        raise _ToolError("target is disabled; wait for it to become enabled and retry")
    if action not in ("click", "type"):
        return
    # ``type`` clicks the field to focus it, so both actions need the point.
    try:
        element.scroll_into_view()
    except Exception:  # noqa: BLE001, S110 - pydoll scrolls again inside click
        pass
    if not _probe_bool(element, "is_on_top"):
        raise _ToolError(
            "target is covered by another element at its centre point; dismiss "
            "the overlay (cookie banner, modal, sticky header) or target the "
            "covering control"
        )


def _click(element: Any) -> tuple[str, str | None]:
    """Click natively, falling back to a JavaScript click that is reported."""
    try:
        element.click()
        return "native", None
    except Exception as native_error:  # noqa: BLE001 - fall back to a JS click
        try:
            element.click_using_js()
        except Exception:  # noqa: BLE001 - report the original native failure
            raise native_error from None
        return "js", (
            "native click failed; used a JavaScript click, which ignores "
            "overlays and pointer-events"
        )


def _probe(tab: Any) -> dict[str, Any] | None:
    """Read the page's mutation counter and URL, installing the counter once."""
    try:
        data = _remote_value(
            tab.execute_script(
                _PROBE_JS,
                return_by_value=True,
                allow_unsafe_eval_blocked_by_csp=True,
            )
        )
    except Exception:  # noqa: BLE001 - the effect probe is optional
        return None
    return data if isinstance(data, dict) else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _effect(tab: Any, before: dict[str, Any] | None) -> dict[str, Any]:
    """Describe what an action changed, when the page can say."""
    after = _probe(tab)
    if before is None or after is None:
        return {}
    if after.get("url") != before.get("url"):
        return {"changed": True, "effect": "navigation"}
    mutations = _count(after.get("mutations")) - _count(before.get("mutations"))
    if mutations > 0:
        return {"changed": True, "effect": "dom", "mutations": mutations}
    return {"changed": False, "effect": "none", "warning": _NO_EFFECT_WARNING}


def _field_state(element: Any) -> dict[str, Any] | None:
    """Read a form field's live value so typing can be verified."""
    script = _FIELD_STATE_JS.replace("__MAX_TEXT__", str(_MAX_CONSOLE_TEXT))
    try:
        data = _remote_value(element.execute_script(script, return_by_value=True))
    except Exception:  # noqa: BLE001 - verification is best-effort
        return None
    return data if isinstance(data, dict) else None


def _verify_typed(state: dict[str, Any] | None, expected: str) -> dict[str, Any]:
    """Report whether the field holds the typed text after typing it."""
    if state is None:
        return {}
    actual = state.get("value")
    if actual is None:
        actual_text = str(state.get("text") or "")
        verified = expected in actual_text
    else:
        actual_text = str(actual)
        verified = actual_text == expected
    if verified:
        return {"verified": True}
    payload: dict[str, Any] = {
        "verified": False,
        "actual_length": len(actual_text),
        "warning": (
            "the field value differs after typing; the page may mask, truncate, "
            "or reject the text"
        ),
    }
    if str(state.get("type") or "") != "password":
        payload["actual"] = actual_text[:80]
    return payload


def _wait(
    tab: Any, target: str | None, value: str | None, timeout: float
) -> dict[str, Any]:
    """Sleep, or wait for a selector, text, or URL prefix."""
    budget = min(timeout, _MAX_WAIT_S)
    if isinstance(target, str) and target.strip():
        return _wait_for(tab, "selector", _selector(target), target, budget)
    text = (value or "").strip()
    if not text or _INT_RE.fullmatch(text) or _FLOAT_RE.fullmatch(text):
        seconds = _coerce_seconds(text or None, 1.0)
        time.sleep(seconds)
        return {
            "status": "ok",
            "action": "wait",
            "seconds": seconds,
            **_page_info(tab),
        }
    if text.startswith("selector="):
        selector = text[len("selector=") :].strip()
        if not selector:
            raise _ToolError("selector= requires a CSS or XPath selector")
        return _wait_for(tab, "selector", selector, selector, budget)
    for prefix in ("text*=", "text="):
        if text.startswith(prefix):
            needle = text[len(prefix) :]
            if not needle:
                raise _ToolError(f"{prefix} requires text to wait for")
            return _wait_for(tab, "text", needle, f"text {needle!r}", budget)
    if text.startswith("url=") or _SCHEME_RE.match(text):
        prefix = text[len("url=") :].strip() if text.startswith("url=") else text
        prefix = prefix.rstrip("*")
        if not prefix:
            raise _ToolError("url= requires a URL prefix to wait for")
        return _wait_for(tab, "url", prefix, f"url starting with {prefix!r}", budget)
    raise _ToolError(
        "wait value must be seconds, text=..., url=..., or selector=...; "
        "or pass target=selector"
    )


def _wait_for(
    tab: Any, kind: str, needle: str, description: str, timeout: float
) -> dict[str, Any]:
    if kind == "selector":
        script = _WAIT_SELECTOR_JS.replace("__SELECTOR__", json.dumps(needle))
    elif kind == "text":
        script = _WAIT_TEXT_JS.replace("__TEXT__", json.dumps(needle))
    else:
        script = _WAIT_URL_JS.replace("__PREFIX__", json.dumps(needle))
    try:
        tab.wait_for_script(script, timeout=timeout)
    except TimeoutException as exc:
        raise _ToolError(
            f"timed out after {timeout:g}s waiting for {description}"
        ) from exc
    return {
        "status": "ok",
        "action": "wait",
        "waited_for": description,
        "timeout_s": timeout,
        **_page_info(tab),
    }


def _press(tab: Any, value: str | None) -> None:
    if not isinstance(value, str) or value == "":
        raise _ToolError("press requires value=Key, for example Enter or Control+A")
    parts = (
        [" "]
        if value == " "
        else [part.strip() for part in value.split("+") if part.strip()]
    )
    if not 1 <= len(parts) <= 3:
        raise _ToolError(
            "press accepts one key or a combination of up to three, e.g. Enter or Control+A"
        )
    keys = [_key(part) for part in parts]
    if len(keys) == 1:
        tab.keyboard.press(keys[0])
    elif len(keys) == 2:
        tab.keyboard.hotkey(keys[0], keys[1])
    else:
        tab.keyboard.hotkey(keys[0], keys[1], keys[2])


def _key(value: str) -> Key:
    if value == " ":
        return Key.SPACE
    raw = value.strip()
    normalized = re.sub(r"[\s_-]+", "", raw).upper()
    normalized = _KEY_ALIASES.get(normalized, normalized)
    key = Key.__members__.get(normalized)
    if key is not None:
        return key
    raise _ToolError(
        f"Unknown key {value!r}. Use a name like Enter, Tab, Escape, ArrowDown, "
        "Control+A, or a single letter."
    )


def _scroll(tab: Any, value: str | None) -> None:
    direction = (value or "down").strip().lower()
    if direction == "top":
        tab.scroll.to_top(smooth=False)
        return
    if direction == "bottom":
        tab.scroll.to_bottom(smooth=False)
        return
    if direction in ("up", "down"):
        position = ScrollPosition.UP if direction == "up" else ScrollPosition.DOWN
        tab.scroll.by(position, _DEFAULT_SCROLL_PX, smooth=False)
        return
    if _INT_RE.fullmatch(direction):
        pixels = int(direction)
        if pixels == 0:
            raise _ToolError("scroll pixels cannot be zero")
        position = ScrollPosition.DOWN if pixels > 0 else ScrollPosition.UP
        tab.scroll.by(position, abs(pixels), smooth=False)
        return
    raise _ToolError("scroll value must be down, up, top, bottom, or a pixel count")


def _select(element: Any, value: str) -> Any:
    response = element.execute_script(
        "this.value = arguments[0];"
        "this.dispatchEvent(new Event('input', {bubbles: true}));"
        "this.dispatchEvent(new Event('change', {bubbles: true}));"
        "return this.value;",
        arguments=[CallArgument(value=value)],
        return_by_value=True,
    )
    return _remote_value(response)


def _remote_value(response: Any) -> Any:
    if not isinstance(response, dict):
        raise _ToolError("Browser returned an unexpected script response")
    payload = response.get("result")
    if not isinstance(payload, dict):
        raise _ToolError("Browser returned an unexpected script response")
    if "exceptionDetails" in payload:
        details = payload["exceptionDetails"]
        exception = details.get("exception") or {}
        text = (
            exception.get("description")
            or details.get("text")
            or "JavaScript exception"
        )
        raise _ToolError(f"JavaScript error: {text}")
    remote = payload.get("result") or {}
    if "value" in remote:
        return remote["value"]
    if "unserializableValue" in remote:
        return remote["unserializableValue"]
    return remote.get("description")


def _settle(tab: Any, timeout: float, *, previous_url: str | None = None) -> bool:
    """Wait briefly for navigation and in-flight requests after an action."""
    budget = max(0.1, min(timeout, _SETTLE_TIMEOUT_S))
    deadline = time.monotonic() + budget
    remaining = budget
    if previous_url is not None:
        try:
            tab.wait_for_url(lambda url: url != previous_url, timeout=budget)
        except Exception:  # noqa: BLE001, S110 - the action already succeeded
            pass
        remaining = max(0.1, deadline - time.monotonic())
    try:
        tab.wait_for_network_idle(
            idle_time=min(_SETTLE_IDLE_S, remaining), timeout=remaining
        )
    except Exception:  # noqa: BLE001 - settle is best-effort
        return False
    return True


def _perform(
    tab: Any,
    action: str,
    target: str | None,
    value: str | None,
    timeout: float,
    *,
    url: str | None = None,
) -> dict:
    if action == "navigate":
        assert url is not None
        tab.go_to(url, timeout=max(1, int(timeout)))
        settled = _settle(tab, timeout)
        return {
            "status": "ok",
            "action": "navigate",
            "settled": settled,
            **_page_info(tab),
        }

    if action == "click":
        element = _element(tab, target, timeout)
        _require_actionable(element, "click")
        before = _probe(tab)
        method, method_warning = _click(element)
        settled = _settle(tab, timeout)
        effect = _effect(tab, before)
        payload: dict[str, Any] = {
            "status": "ok",
            "action": "click",
            "target": target,
            "method": method,
            "settled": settled,
            **_page_info(tab),
            **effect,
        }
        warnings = [text for text in (method_warning, effect.get("warning")) if text]
        if warnings:
            payload["warning"] = "; ".join(warnings)
        return payload

    if action == "type":
        if value is None:
            raise _ToolError("type requires value=text")
        element = _element(tab, target, timeout)
        _require_actionable(element, "type")
        if not _probe_bool(element, "is_editable"):
            raise _ToolError(
                "target does not accept text; it is not an input, textarea, or "
                "contenteditable, or it is disabled or read-only"
            )
        element.clear()
        method = "keys"
        try:
            element.type_text(value)
        except Exception as type_error:  # noqa: BLE001 - fall back to JS insertion
            try:
                element.focus()
                element.insert_text(value)
            except Exception:  # noqa: BLE001 - report the original typing failure
                raise type_error from None
            method = "js"
        settled = _settle(tab, timeout)
        payload = {
            "status": "ok",
            "action": "type",
            "target": target,
            "value_length": len(value),
            "method": method,
            "settled": settled,
            **_page_info(tab),
        }
        payload.update(_verify_typed(_field_state(element), value))
        return payload

    if action == "press":
        before = _probe(tab)
        _press(tab, value)
        settled = _settle(tab, timeout)
        return {
            "status": "ok",
            "action": "press",
            "key": value,
            "settled": settled,
            **_page_info(tab),
            **_effect(tab, before),
        }

    if action == "hover":
        element = _element(tab, target, timeout)
        _require_actionable(element, "hover")
        element.hover()
        return {"status": "ok", "action": "hover", "target": target, **_page_info(tab)}

    if action == "select":
        if value is None:
            raise _ToolError("select requires value=option value")
        element = _element(tab, target, timeout)
        _require_actionable(element, "select")
        selected = _select(element, value)
        settled = _settle(tab, timeout)
        payload = {
            "status": "ok",
            "action": "select",
            "target": target,
            "selected": selected,
            "settled": settled,
            **_page_info(tab),
        }
        if selected is not None:
            payload["verified"] = str(selected) == value
            if not payload["verified"]:
                payload["warning"] = (
                    f"the field holds {selected!r} after selecting {value!r}; "
                    "the option may be absent or disabled"
                )
        return payload

    if action == "scroll":
        _scroll(tab, value)
        return {"status": "ok", "action": "scroll", "value": value, **_page_info(tab)}

    if action in ("back", "forward"):
        previous_url = _page_info(tab).get("url")
        tab.execute_script(f"history.{action}()")
        settled = _settle(tab, timeout, previous_url=previous_url)
        return {"status": "ok", "action": action, "settled": settled, **_page_info(tab)}

    if action == "reload":
        tab.refresh()
        settled = _settle(tab, timeout)
        return {
            "status": "ok",
            "action": "reload",
            "settled": settled,
            **_page_info(tab),
        }

    if action == "wait":
        return _wait(tab, target, value, timeout)

    raise _ToolError(
        f"Unknown action {action!r}"
    )  # pragma: no cover - validated earlier


def _browser_action(
    session: BrowserSession,
    action: str,
    target: TextArg | None,
    value: TextArg | None,
    timeout: float | None,
) -> dict:
    action = str(action).strip().lower()
    target = _text_arg(target)
    value = _text_arg(value)
    if action not in _ACTIONS:
        return _error(
            action,
            _ToolError(
                f"Unknown action {action!r}. Use one of: {', '.join(_ACTIONS)}."
            ),
        )
    extra = {"target": target} if target else {}
    try:
        timeout_s = _coerce_timeout(timeout, session.timeout)
        url = (
            session._check_url(_normalize_url(value)) if action == "navigate" else None
        )
        before = _tab_count(session) if action in _TAB_ACTIONS else None
        payload = session._run(
            lambda tab: _perform(tab, action, target, value, timeout_s, url=url)
        )
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        extra.update(session.take_diagnostics())
        return _error(action, exc, **extra)
    if before is not None and payload.get("status") == "ok":
        after = _tab_count(session)
        if after is not None and after != before:
            payload["tabs"] = after
            payload["tabs_changed"] = after - before
            if after > before:
                payload["warning"] = "; ".join(
                    text
                    for text in (
                        payload.get("warning"),
                        "a new tab opened; call browser_tabs to switch to it",
                    )
                    if text
                )
    return payload


def _clamp_nodes(value: int | str) -> int:
    try:
        nodes = int(value)
    except (TypeError, ValueError) as exc:
        raise _ToolError("max_nodes must be an integer") from exc
    return max(1, min(nodes, _MAX_SNAPSHOT_NODES))


def _snapshot_tab(tab: Any, max_nodes: int) -> dict:
    script = _SNAPSHOT_JS.replace("__MAX_NODES__", str(max_nodes)).replace(
        "__MAX_NAME__", str(_MAX_NAME_CHARS)
    )
    response = tab.execute_script(
        script,
        return_by_value=True,
        allow_unsafe_eval_blocked_by_csp=True,
    )
    data = _remote_value(response)
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise _ToolError("Snapshot script returned an unexpected shape")
    return data


def _render_nodes(nodes: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for node in nodes:
        depth = min(int(node.get("depth", 0)), 20)
        bits = [str(node.get("ref", "")), str(node.get("role", ""))]
        name = str(node.get("name", ""))
        if name:
            bits.append(json.dumps(name, ensure_ascii=False))
        value = node.get("value")
        if value not in (None, ""):
            bits.append(f"value={json.dumps(str(value), ensure_ascii=False)}")
        href = node.get("href")
        if href:
            bits.append(f"href={json.dumps(str(href), ensure_ascii=False)}")
        if node.get("checked"):
            bits.append("checked")
        if node.get("disabled"):
            bits.append("disabled")
        lines.append("  " * depth + " ".join(bits))
    return "\n".join(lines)


def _coerce_snapshot_format(value: SnapshotFormat | str) -> SnapshotFormat:
    if value not in ("nodes", "text", "both"):
        raise _ToolError("format must be nodes, text, or both")
    return value


def _snapshot(
    session: BrowserSession, max_nodes: int | str, format: SnapshotFormat | str
) -> dict:
    try:
        nodes_limit = _clamp_nodes(max_nodes)
        view = _coerce_snapshot_format(format)
        data = session._run(lambda tab: _snapshot_tab(tab, nodes_limit))
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return _error("snapshot", exc)
    nodes = data["nodes"]
    payload: dict[str, Any] = {
        "status": "ok",
        "action": "snapshot",
        "url": data.get("url", ""),
        "title": data.get("title", ""),
        "node_count": len(nodes),
        "truncated": bool(data.get("truncated")),
    }
    if view in ("nodes", "both"):
        payload["nodes"] = nodes
    if view in ("text", "both"):
        payload["text"] = _render_nodes(nodes)
    return payload


def _evaluate_tab(tab: Any, script: TextArg, await_promise: bool) -> dict:
    if not isinstance(script, str) or not script.strip():
        raise _ToolError("evaluate requires a non-empty JavaScript string")
    response = tab.execute_script(
        script,
        return_by_value=True,
        await_promise=await_promise,
        allow_unsafe_eval_blocked_by_csp=True,
    )
    value = _remote_value(response)
    payload = response.get("result", {})
    remote = payload.get("result", {})
    data: dict[str, Any] = {
        "result": value,
        "type": remote.get("type"),
    }
    data.update(_page_info(tab))
    return data


def _evaluate(session: BrowserSession, script: TextArg, await_promise: bool) -> dict:
    try:
        data = session._run(lambda tab: _evaluate_tab(tab, script, await_promise))
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return _error("evaluate", exc)
    return {"status": "ok", "action": "evaluate", **data}


def _tab_count(session: BrowserSession) -> int | None:
    """How many page tabs are open, or ``None`` when that cannot be read."""
    browser = session._browser
    if browser is None:
        return None
    try:
        return len(browser.get_opened_tabs())
    except Exception:  # noqa: BLE001 - the action already succeeded
        return None


def _coerce_limit(value: int | str | None, default: int = 20) -> int:
    if value is None or value == "":
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise _ToolError("limit must be an integer") from exc
    return max(1, min(limit, _MAX_NETWORK_ITEMS))


def _tab_text(tab: Any) -> str:
    """Lowercased URL and title of a tab, for matching by substring."""
    parts: list[str] = []
    for name in ("current_url", "title"):
        try:
            parts.append(str(getattr(tab, name)()))
        except Exception:  # noqa: BLE001, S112 - a tab may have gone away
            continue
    return " ".join(parts).lower()


def _resolve_tab(session: BrowserSession, tabs: list[Any], value: str | None) -> Any:
    if not tabs:
        raise _ToolError("no tabs are open; use browser_tabs with action=new")
    text = (value or "").strip()
    if not text:
        return session._tab if session._tab is not None else tabs[-1]
    if _INT_RE.fullmatch(text):
        index = int(text)
        if not 0 <= index < len(tabs):
            raise _ToolError(
                f"tab index {index} is out of range; {len(tabs)} tabs are open"
            )
        return tabs[index]
    needle = text.lower()
    matches = [tab for tab in tabs if needle in _tab_text(tab)]
    if not matches:
        raise _ToolError(f"no tab matches {value!r}; call browser_tabs to list them")
    if len(matches) > 1:
        raise _ToolError(
            f"{len(matches)} tabs match {value!r}; pass an index from browser_tabs"
        )
    return matches[0]


def _tabs_action(
    session: BrowserSession, action: str, value: TextArg | None, timeout: float
) -> dict:
    action = str(action).strip().lower()
    if action not in ("list", "new", "switch", "close"):
        return _error("tabs", _ToolError("action must be list, new, switch, or close"))
    text = _text_arg(value)
    with session._lock:
        try:
            timeout_s = _coerce_timeout(timeout, session.timeout)
            tabs = session._tab_list()
            closed_all = False
            if action == "new":
                browser = session._browser
                assert browser is not None  # _tab_list started it above
                url = session._check_url(_normalize_url(text)) if text else None
                tab = browser.new_tab()
                session._tab = tab
                session._install_watch(tab)
                if url is not None:
                    tab.go_to(url, timeout=max(1, int(timeout_s)))
                    _settle(tab, timeout_s)
                tabs = session._tab_list()
            elif action == "switch":
                session._tab = _resolve_tab(session, tabs, text)
            elif action == "close":
                target = _resolve_tab(session, tabs, text)
                target_id = getattr(target, "target_id", None)
                target.close()
                if target_id is not None:
                    session._watched.discard(target_id)
                tabs = [
                    tab
                    for tab in session._tab_list()
                    if getattr(tab, "target_id", None) != target_id
                ]
                if not tabs:
                    session._reset()
                    closed_all = True
        except Exception as exc:  # noqa: BLE001 - returned as tool status
            payload = _error("tabs", exc)
            payload.update(session.take_diagnostics())
            return payload
    entries = [session._tab_entry(index, tab) for index, tab in enumerate(tabs)]
    payload = {
        "status": "ok",
        "action": action,
        "count": len(entries),
        "tabs": entries,
        "active": next((entry["index"] for entry in entries if entry["active"]), None),
    }
    if closed_all:
        payload["closed_all"] = True
    payload.update(session.take_diagnostics())
    return payload


def _cookie_list(value: str | None) -> list[dict[str, Any]]:
    """Parse a JSON cookie list, a ``Cookie:`` header, or a saved state file."""
    text = (value or "").strip()
    if not text:
        raise _ToolError(
            "cookies set requires value=JSON or a Cookie header such as a=1; b=2"
        )
    if text.startswith(("[", "{")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _ToolError(f"cookies value is not valid JSON: {exc.msg}") from exc
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        if not isinstance(data, list) or not data:
            raise _ToolError("cookies JSON must be a list of cookie objects")
        cookies: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict) or "name" not in item or "value" not in item:
                raise _ToolError("each cookie needs a name and a value")
            cookies.append({key: item[key] for key in _COOKIE_FIELDS if key in item})
        return cookies
    cookies = []
    for part in text.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if not separator or not name.strip():
            continue
        cookies.append({"name": name.strip(), "value": cookie_value.strip()})
    if not cookies:
        raise _ToolError("the cookie header did not contain any name=value pair")
    return cookies


def _cookie_records(cookies: Any) -> list[dict[str, Any]]:
    if not isinstance(cookies, list):
        return []
    return [
        {key: cookie[key] for key in _COOKIE_FIELDS if key in cookie}
        for cookie in cookies
        if isinstance(cookie, dict)
    ]


def _cookies_action(
    session: BrowserSession, action: str, value: TextArg | None
) -> dict:
    action = str(action).strip().lower()
    if action not in ("get", "set", "clear", "save", "load"):
        return _error(
            "cookies",
            _ToolError("action must be get, set, clear, save, or load"),
        )
    text = _text_arg(value)
    with session._lock:
        try:
            if action == "get":
                cookies = _cookie_records(session._run(lambda tab: tab.get_cookies()))
                if text:
                    cookies = [
                        cookie
                        for cookie in cookies
                        if text in str(cookie.get("name", ""))
                        or text in str(cookie.get("domain", ""))
                    ]
                cookies = cookies[-_MAX_COOKIE_ITEMS:]
                return {
                    "status": "ok",
                    "action": "get",
                    "count": len(cookies),
                    "cookies": cookies,
                    **session.take_diagnostics(),
                }
            if action == "clear":
                session._run(lambda tab: tab.delete_all_cookies())
                return {
                    "status": "ok",
                    "action": "clear",
                    **session.take_diagnostics(),
                }
            if action == "save":
                if not text:
                    raise _ToolError("cookies save requires value=path")
                target = Path(text).expanduser()
                state = session._run(
                    lambda tab: {
                        "url": tab.current_url(),
                        "cookies": _cookie_records(tab.get_cookies()),
                    }
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(state, indent=2), encoding="utf-8")
                return {
                    "status": "ok",
                    "action": "save",
                    "path": str(target.absolute()),
                    "count": len(state["cookies"]),
                }
            if action == "load":
                if not text:
                    raise _ToolError("cookies load requires value=path")
                source = Path(text).expanduser()
                try:
                    state = json.loads(source.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise _ToolError(f"could not read {source}: {exc}") from exc
                except json.JSONDecodeError as exc:
                    raise _ToolError(f"{source} is not valid JSON: {exc.msg}") from exc
                return _apply_cookies(session, _cookie_list(json.dumps(state)))
            return _apply_cookies(session, _cookie_list(text))
        except Exception as exc:  # noqa: BLE001 - returned as tool status
            payload = _error("cookies", exc)
            payload.update(session.take_diagnostics())
            return payload


def _apply_cookies(session: BrowserSession, cookies: list[dict[str, Any]]) -> dict:
    """Set cookies, defaulting their scope to the current page when unset."""

    def apply(tab: Any) -> dict[str, Any]:
        page_url = None
        for cookie in cookies:
            if "url" not in cookie and "domain" not in cookie:
                if page_url is None:
                    page_url = tab.current_url()
                cookie["url"] = page_url
        tab.set_cookies(cookies)
        return {"status": "ok", "action": "set", "count": len(cookies)}

    return session._run(apply)


def _network_action(
    session: BrowserSession, action: str, value: TextArg | None, limit: int | str
) -> dict:
    action = str(action).strip().lower()
    if action not in ("requests", "clear", "body"):
        return _error("network", _ToolError("action must be requests, clear, or body"))
    text = _text_arg(value)
    with session._lock:
        try:
            if action == "clear":
                session._run(lambda tab: None)
                session._watch.clear_network()
                return {
                    "status": "ok",
                    "action": "clear",
                    **session.take_diagnostics(),
                }
            if action == "body":
                if not text:
                    raise _ToolError(
                        "network body requires value=request id from browser_network"
                    )
                body = session._run(lambda tab: tab.get_network_response_body(text))
                body = "" if body is None else str(body)
                return {
                    "status": "ok",
                    "action": "body",
                    "id": text,
                    "body": body[:_MAX_NETWORK_BODY],
                    "truncated": len(body) > _MAX_NETWORK_BODY,
                }
            session._run(lambda tab: None)
            entries = session._watch.network(_coerce_limit(limit), text)
            return {
                "status": "ok",
                "action": "requests",
                "count": len(entries),
                "requests": entries,
                **session.take_diagnostics(),
            }
        except Exception as exc:  # noqa: BLE001 - returned as tool status
            payload = _error("network", exc)
            payload.update(session.take_diagnostics())
            return payload


def _screenshot_path(path: str | None) -> Path:
    if path:
        target = Path(path).expanduser()
        return (
            target.with_suffix(target.suffix.lower())
            if target.suffix
            else target.with_suffix(".png")
        )
    directory = Path(tempfile.gettempdir()) / "dspy-agent-tools"
    return directory / f"browser-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.png"


def _screenshot_format(path: str | None) -> str:
    if path:
        suffix = Path(path).expanduser().suffix.lstrip(".").lower()
        if suffix:
            return "jpeg" if suffix == "jpg" else suffix
    return "png"


def _screenshot_tab(
    tab: Any,
    path: str | None,
    full_page: bool,
    quality: int | str,
    as_base64: bool,
) -> dict:
    quality = _coerce_quality(quality)
    if as_base64:
        format_path = _screenshot_path(path) if path else Path("screenshot.png")
        data = tab.take_screenshot(
            path=str(format_path),
            quality=quality,
            beyond_viewport=full_page,
            as_base64=True,
        )
        return {"format": _screenshot_format(str(format_path)), "base64": data}
    target = _screenshot_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tab.take_screenshot(
        path=str(target),
        quality=quality,
        beyond_viewport=full_page,
    )
    return {
        "path": str(target),
        "format": _screenshot_format(str(target)),
        "bytes": target.stat().st_size,
    }


def _screenshot(
    session: BrowserSession,
    path: str | None,
    full_page: bool,
    quality: int | str,
    as_base64: bool,
) -> dict:
    try:
        data = session._run(
            lambda tab: _screenshot_tab(tab, path, full_page, quality, as_base64)
        )
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return _error("screenshot", exc)
    return {"status": "ok", "action": "screenshot", **data}


def _register_exit_hook(callback: Callable[[], object]) -> None:
    """Run *callback* before the interpreter tears down thread pools.

    A plain :mod:`atexit` hook is too late for pydoll: ``concurrent.futures``
    shuts its workers down from a :mod:`threading` exit hook, which runs
    before :mod:`atexit`, and ``Chrome.stop()`` needs a worker to end the
    process. Registering through the same pre-shutdown queue puts the browser
    shutdown first, so the process and its temporary profile are released
    instead of being rmtree'd while Chrome is still writing to them.
    """
    register = getattr(threading, "_register_atexit", None)
    if register is None:
        atexit.register(callback)
    else:
        register(callback)


class BrowserSession:
    """A lazily started headless Chrome session.

    The first tool call starts Chrome; later calls reuse the same tab. Use one
    instance per user or session when a shared server must not mix cookies,
    history, or tabs between callers. ``close`` is optional: the browser is
    stopped at interpreter exit and the next call after ``close`` starts a
    fresh one.

    Navigation defaults to ``http``, ``https``, ``about``, and ``data`` URLs.
    Pass ``allowed_schemes`` to extend or restrict that set, or ``url_policy``
    to reject individual normalized URLs. For example, a server can block
    private hosts with a callback while keeping the default scheme policy.

    ``dialog_policy`` decides what happens to ``alert``, ``confirm``,
    ``beforeunload``, and ``prompt`` dialogs: ``dismiss`` (default) cancels
    them and ``accept`` confirms them. Either way the dialog is reported in the
    action result under ``dialog``, so a step that was cancelled by a prompt
    says so instead of hanging.

    ``user_data_dir`` points Chrome at a profile directory that outlives the
    process, so cookies, storage, and history survive a restart. Chrome writes
    that profile during shutdown, so ``close`` asks the browser to exit and
    waits briefly before ending the process; without that wait the cookie
    database is left half-written and the login is lost. Without a directory,
    every run starts from an empty temporary profile.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout: float = _DEFAULT_TIMEOUT_S,
        window_size: tuple[int, int] = _DEFAULT_WINDOW_SIZE,
        allowed_schemes: Iterable[str] | None = None,
        url_policy: Callable[[str], bool] | None = None,
        dialog_policy: DialogPolicy = _DEFAULT_DIALOG_POLICY,
        user_data_dir: str | Path | None = None,
    ) -> None:
        if dialog_policy not in ("dismiss", "accept"):
            raise ValueError("dialog_policy must be dismiss or accept")
        self.headless = headless
        self.timeout = timeout
        self.window_size = window_size
        self.dialog_policy: DialogPolicy = dialog_policy
        self.user_data_dir = (
            Path(user_data_dir).expanduser().resolve() if user_data_dir else None
        )
        if allowed_schemes is None:
            self.allowed_schemes = _DEFAULT_ALLOWED_SCHEMES
        else:
            schemes = (
                (allowed_schemes,)
                if isinstance(allowed_schemes, str)
                else allowed_schemes
            )
            self.allowed_schemes = frozenset(str(scheme).lower() for scheme in schemes)
        self.url_policy = url_policy
        self._lock = threading.RLock()
        self._browser: Any | None = None
        self._tab: Any | None = None
        self._watch = _PageWatch()
        self._watched: set[str] = set()
        self._recorder: _VideoRecorder | None = None
        self.last_recording: dict[str, Any] | None = None
        self._exit_hook_registered = False

    def _check_url(self, url: str) -> str:
        scheme = urlsplit(url).scheme.lower()
        if scheme not in self.allowed_schemes:
            allowed = ", ".join(sorted(self.allowed_schemes)) or "none"
            raise _ToolError(
                f"URL scheme {scheme or '(none)'} is not allowed; allowed schemes: {allowed}"
            )
        if self.url_policy is not None and not self.url_policy(url):
            raise _ToolError("URL rejected by the browser URL policy")
        return url

    def _start(self) -> Any:
        _require_pydoll()
        options = ChromiumOptions()
        options.headless = self.headless
        options.start_timeout = max(10, int(self.timeout))
        options.add_argument(
            f"--window-size={self.window_size[0]},{self.window_size[1]}"
        )
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--hide-scrollbars")
        if self.user_data_dir is not None:
            # pydoll only adds a temporary --user-data-dir when none is set,
            # so this one makes the profile outlive the process.
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            options.add_argument(f"--user-data-dir={self.user_data_dir}")
        browser = Chrome(options=options)
        tab = browser.start()
        self._browser = browser
        self._tab = tab
        self._install_watch(tab)
        if not self._exit_hook_registered:
            _register_exit_hook(self.close)
            self._exit_hook_registered = True
        return tab

    def _install_watch(self, tab: Any) -> None:
        """Subscribe *tab* to console, error, dialog, and network events."""
        watch = self._watch
        target = getattr(tab, "target_id", None)
        if target is not None:
            if target in self._watched:
                return
            self._watched.add(target)

        def responder(dialog: dict[str, Any]) -> str:
            return self._answer_dialog(tab, dialog)

        try:
            tab.enable_page_events()
            tab.enable_runtime_events()
            tab.enable_network_events()
            watch.callback_ids.extend(
                [
                    tab.on(
                        PageEvent.JAVASCRIPT_DIALOG_OPENING,
                        lambda event: watch.on_dialog(event, responder),
                    ),
                    tab.on(RuntimeEvent.CONSOLE_API_CALLED, watch.on_console),
                    tab.on(RuntimeEvent.EXCEPTION_THROWN, watch.on_exception),
                    tab.on(NetworkEvent.REQUEST_WILL_BE_SENT, watch.on_request),
                    tab.on(NetworkEvent.RESPONSE_RECEIVED, watch.on_response),
                    tab.on(NetworkEvent.LOADING_FAILED, watch.on_failed),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics are best-effort
            logger.warning("Could not enable page diagnostics: %s", _format_error(exc))

    def _get_tab(self) -> Any:
        if self._tab is None:
            return self._start()
        return self._tab

    def _tab_list(self) -> list[Any]:
        """Live page tabs, oldest first, each with diagnostics installed."""
        browser = self._browser
        if browser is None:
            return [self._get_tab()]
        tabs = list(browser.get_opened_tabs())
        for tab in tabs:
            self._install_watch(tab)
        active = getattr(self._tab, "target_id", None)
        if active is None or all(
            getattr(tab, "target_id", None) != active for tab in tabs
        ):
            self._tab = tabs[-1] if tabs else None
        return tabs

    def _tab_entry(self, index: int, tab: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {"index": index, **_page_info(tab)}
        active = getattr(self._tab, "target_id", None)
        entry["active"] = active is not None and (
            getattr(tab, "target_id", None) == active
        )
        return entry

    def take_diagnostics(self) -> dict[str, Any]:
        """Return console output, page errors, and a new dialog since the last call."""
        console, errors, dialog = self._watch.take()
        payload: dict[str, Any] = {}
        if console:
            payload["console"] = console
        if errors:
            payload["errors"] = errors
        if dialog is not None:
            payload["dialog"] = dialog
        return payload

    def _answer_dialog(self, tab: Any, dialog: dict[str, Any]) -> str:
        """Answer a dialog per ``dialog_policy``; runs off pydoll's event loop."""
        accept = self.dialog_policy == "accept"
        prompt = None
        if accept and dialog.get("type") == "prompt":
            prompt = dialog.get("default_prompt")
        tab.handle_dialog(accept=accept, prompt_text=prompt)
        return "accepted" if accept else "dismissed"

    def _reset(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            _discard_recording(recorder)
            self.last_recording = _error(
                "record_stop",
                _ToolError("browser disconnected; the recording was discarded"),
            )
        browser, self._browser, self._tab = self._browser, None, None
        self._watch = _PageWatch()
        self._watched = set()
        if browser is not None:
            if self.user_data_dir is not None:
                _close_browser_gracefully(browser)
            try:
                browser.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                logger.warning("Could not stop the browser: %s", _format_error(exc))

    def _run(self, operation: Any) -> Any:
        with self._lock:
            try:
                tab = self._get_tab()
                result = operation(tab)
            except Exception as exc:
                if self._tab is None or not _is_disconnected(exc):
                    raise
                self._reset()
                tab = self._get_tab()
                result = operation(tab)
            if isinstance(result, dict):
                result.update(self.take_diagnostics())
            return result

    def close(self) -> bool:
        """Stop the browser if it is running; return whether one was open."""
        with self._lock:
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                if self._tab is not None:
                    _stop_screencast(self._tab, recorder)
                try:
                    data = _finalize_recording(recorder)
                except Exception as exc:  # noqa: BLE001 - returned by browser_close
                    self.last_recording = _error("record_stop", exc)
                else:
                    self.last_recording = {
                        "status": "ok",
                        "action": "record_stop",
                        **data,
                    }
            else:
                self.last_recording = None
            browser, self._browser, self._tab = self._browser, None, None
            self._watch = _PageWatch()
            self._watched = set()
            if browser is None:
                return False
            if self.user_data_dir is not None:
                _close_browser_gracefully(browser)
            try:
                browser.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                logger.warning("Could not stop the browser: %s", _format_error(exc))
            return True

    def _start_recording(self, recorder: _VideoRecorder) -> None:
        """Install *recorder* on the current tab."""
        with self._lock:
            if self._recorder is not None:
                raise _ToolError(
                    "a video recording is already in progress; stop it before starting another"
                )
            self._run(lambda tab: _start_screencast(tab, recorder))
            self._recorder = recorder
            self.last_recording = None

    def _stop_recording(self) -> dict[str, Any]:
        """Stop the active recording and return its encoding result."""
        with self._lock:
            recorder, self._recorder = self._recorder, None
            if recorder is None:
                raise _ToolError("no video recording is in progress")
            if self._tab is not None:
                _stop_screencast(self._tab, recorder)
        return _finalize_recording(recorder)

    def record_start(
        self,
        path: str | None = None,
        fps: float | str = _DEFAULT_RECORDING_FPS,
        quality: int | str = _DEFAULT_SCREENSHOT_QUALITY,
        max_width: int | str | None = None,
        max_height: int | str | None = None,
    ) -> dict:
        """Start recording the current tab; see ``browser_record_start``."""
        return _record_start(self, path, fps, quality, max_width, max_height)

    def record_stop(self) -> dict:
        """Stop recording and encode the video; see ``browser_record_stop``."""
        return _record_stop(self)

    def take_last_recording(self) -> dict[str, Any] | None:
        """Return and clear the most recent recording result."""
        result, self.last_recording = self.last_recording, None
        return result

    def browser(
        self,
        action: Action,
        target: TextArg | None = None,
        value: TextArg | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> dict:
        """Run one browser action; see the module-level ``browser`` tool."""
        return _browser_action(self, action, target, value, timeout)

    def open(
        self,
        url: TextArg,
        max_nodes: int | str = _DEFAULT_SNAPSHOT_NODES,
        format: SnapshotFormat = _DEFAULT_SNAPSHOT_FORMAT,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> dict:
        """Navigate to ``url`` and snapshot the new page; see ``browser_open``."""
        nav = self.browser("navigate", value=url, timeout=timeout)
        if nav["status"] != "ok":
            return {**nav, "action": "open"}
        payload = {**self.snapshot(max_nodes, format), "action": "open"}
        for key in ("console", "errors"):
            if key in nav:
                payload[key] = nav[key] + payload.get(key, [])
        if "dialog" in nav and "dialog" not in payload:
            payload["dialog"] = nav["dialog"]
        return payload

    def snapshot(
        self,
        max_nodes: int | str = _DEFAULT_SNAPSHOT_NODES,
        format: SnapshotFormat = _DEFAULT_SNAPSHOT_FORMAT,
    ) -> dict:
        """Return the current page as structured nodes; see ``browser_snapshot``."""
        return _snapshot(self, max_nodes, format)

    def screenshot(
        self,
        path: str | None = None,
        full_page: bool = False,
        quality: int | str = _DEFAULT_SCREENSHOT_QUALITY,
        as_base64: bool = False,
    ) -> dict:
        """Capture the current page; see ``browser_screenshot``."""
        return _screenshot(self, path, full_page, quality, as_base64)

    def evaluate(self, script: TextArg, await_promise: bool = True) -> dict:
        """Run JavaScript in the page; see ``browser_evaluate``."""
        return _evaluate(self, script, await_promise)

    def tabs(
        self,
        action: str = "list",
        value: TextArg | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> dict:
        """List, open, switch, or close tabs; see ``browser_tabs``."""
        return _tabs_action(self, action, value, timeout)

    def cookies(self, action: str = "get", value: TextArg | None = None) -> dict:
        """Read, write, clear, save, or load cookies; see ``browser_cookies``."""
        return _cookies_action(self, action, value)

    def network(
        self,
        action: str = "requests",
        value: TextArg | None = None,
        limit: int | str = 20,
    ) -> dict:
        """Inspect recorded requests; see ``browser_network``."""
        return _network_action(self, action, value, limit)

    def tools(
        self,
        *,
        include_search: bool = False,
        include_recording: bool = False,
    ) -> BrowserTools:
        """Return ``dspy.Tool`` objects bound to this session."""
        return _build_tools(
            self,
            include_search=include_search,
            include_recording=include_recording,
        )


class _VideoRecorder:
    """Collect CDP screencast frames and encode them with ffmpeg."""

    def __init__(
        self,
        path: Path,
        ffmpeg: str,
        fps: float,
        quality: int,
        max_width: int | None,
        max_height: int | None,
    ) -> None:
        self.path = path
        self.ffmpeg = ffmpeg
        self.fps = fps
        self.quality = quality
        self.max_width = max_width
        self.max_height = max_height
        self.frames_dir = Path(tempfile.mkdtemp(prefix="dspy-agent-tools-video-"))
        self.frames: list[tuple[float, Path]] = []
        self.lock = threading.Lock()
        self.tab: Any | None = None
        self.callback_id: int | None = None
        self._last_kept_timestamp: float | None = None
        self._frame_index = 0

    def on_frame(self, event: dict) -> None:
        """Store one CDP frame; runs on pydoll's callback thread."""
        params = event.get("params") or {}
        session_id = params.get("sessionId")
        metadata = params.get("metadata") or {}
        try:
            raw_timestamp = metadata.get("timestamp")
            timestamp = time.time() if raw_timestamp is None else float(raw_timestamp)
            if (
                self._last_kept_timestamp is not None
                and abs(timestamp - self._last_kept_timestamp) < 1.0 / self.fps
            ):
                return
            data = params.get("data")
            if not isinstance(data, str):
                raise _ToolError("screencast frame did not contain image data")
            raw = base64.b64decode(data, validate=True)
            with self.lock:
                self._frame_index += 1
                frame_path = self.frames_dir / f"frame-{self._frame_index:06d}.jpg"
                frame_path.write_bytes(raw)
                self.frames.append((timestamp, frame_path))
                self._last_kept_timestamp = timestamp
        except Exception as exc:  # noqa: BLE001 - one bad frame must not end the recording
            logger.warning("Could not store a screencast frame: %s", _format_error(exc))
        finally:
            self._ack(session_id)

    def _ack(self, session_id: Any) -> None:
        if session_id is None or self.tab is None:
            return
        try:
            self.tab.execute_command(PageCommands.screencast_frame_ack(session_id))
        except Exception as exc:  # noqa: BLE001 - acknowledgement is best-effort
            logger.warning(
                "Could not acknowledge a screencast frame: %s", _format_error(exc)
            )


def _start_screencast(tab: Any, recorder: _VideoRecorder) -> None:
    tab.enable_page_events()
    recorder.tab = tab
    recorder.callback_id = tab.on(PageEvent.SCREENCAST_FRAME, recorder.on_frame)
    try:
        tab.execute_command(
            PageCommands.start_screencast(
                ScreencastFormat.JPEG,
                quality=recorder.quality,
                max_width=recorder.max_width,
                max_height=recorder.max_height,
                every_nth_frame=1,
            )
        )
    except Exception:
        recorder.callback_id = _remove_callback(tab, recorder.callback_id)
        raise


def _stop_screencast(tab: Any, recorder: _VideoRecorder) -> None:
    try:
        tab.execute_command(PageCommands.stop_screencast())
    except Exception as exc:  # noqa: BLE001 - captured frames are still usable
        logger.warning("Could not stop the screencast: %s", _format_error(exc))
    finally:
        recorder.callback_id = _remove_callback(tab, recorder.callback_id)


def _remove_callback(tab: Any, callback_id: int | None) -> None:
    if callback_id is None:
        return
    try:
        tab.remove_callback(callback_id)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.warning(
            "Could not remove the screencast callback: %s", _format_error(exc)
        )


def _discard_recording(recorder: _VideoRecorder) -> None:
    if recorder.tab is not None:
        _remove_callback(recorder.tab, recorder.callback_id)
    recorder.callback_id = None
    shutil.rmtree(recorder.frames_dir, ignore_errors=True)


def _frame_durations(frames: list[tuple[float, Path]], fps: float) -> list[float]:
    durations: list[float] = []
    for index, (timestamp, _path) in enumerate(frames):
        if index + 1 < len(frames):
            duration = frames[index + 1][0] - timestamp
        else:
            duration = 1.0 / fps
        durations.append(max(0.001, duration))
    return durations


def _write_concat(
    recorder: _VideoRecorder,
    frames: list[tuple[float, Path]],
    durations: list[float],
) -> Path:
    concat = recorder.frames_dir / "frames.txt"
    lines = ["ffconcat version 1.0"]
    for (_timestamp, frame_path), duration in zip(frames, durations, strict=True):
        lines.append(f"file '{frame_path.as_posix()}'")
        lines.append(f"duration {duration:.6f}")
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat


def _ffmpeg_command(recorder: _VideoRecorder, concat: Path, output: Path) -> list[str]:
    command = [
        recorder.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat),
        "-fps_mode",
        "vfr",
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-an",
    ]
    if output.suffix.lower() == ".webm":
        command.extend(
            ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-pix_fmt", "yuv420p"]
        )
    else:
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
            ]
        )
    command.extend(["-y", str(output)])
    return command


def _finalize_recording(recorder: _VideoRecorder) -> dict[str, Any]:
    output = recorder.path.with_name(f"{recorder.path.stem}.part{recorder.path.suffix}")
    try:
        with recorder.lock:
            frames = list(recorder.frames)
        if not frames:
            raise _ToolError("no video frames were captured")
        durations = _frame_durations(frames, recorder.fps)
        concat = _write_concat(recorder, frames, durations)
        recorder.path.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            _ffmpeg_command(recorder, concat, output),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise _ToolError(
                f"ffmpeg could not encode the video: {detail[-500:] or 'unknown error'}"
            )
        if not output.exists():
            raise _ToolError("ffmpeg did not write the video file")
        output.replace(recorder.path)
        return {
            "path": str(recorder.path),
            "format": recorder.path.suffix.lstrip(".").lower(),
            "bytes": recorder.path.stat().st_size,
            "frames": len(frames),
            "duration_s": round(sum(durations), 3),
        }
    finally:
        output.unlink(missing_ok=True)
        shutil.rmtree(recorder.frames_dir, ignore_errors=True)


def _record_start(
    session: BrowserSession,
    path: str | None,
    fps: float | str,
    quality: int | str,
    max_width: int | str | None,
    max_height: int | str | None,
) -> dict:
    try:
        fps_value = _coerce_fps(fps)
        quality_value = _coerce_quality(quality)
        width = _optional_dimension(max_width, "max_width")
        height = _optional_dimension(max_height, "max_height")
        recorder = _VideoRecorder(
            _video_path(path),
            _ffmpeg_binary(),
            fps_value,
            quality_value,
            width,
            height,
        )
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return _error("record_start", exc)
    try:
        session._start_recording(recorder)
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        _discard_recording(recorder)
        return _error("record_start", exc)
    return {
        "status": "ok",
        "action": "record_start",
        "path": str(recorder.path),
        "fps": fps_value,
        "quality": quality_value,
        "max_width": width,
        "max_height": height,
    }


def _record_stop(session: BrowserSession) -> dict:
    try:
        data = session._stop_recording()
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return _error("record_stop", exc)
    return {"status": "ok", "action": "record_stop", **data}


_default_session = BrowserSession()


def browser(
    action: Action,
    target: TextArg | None = None,
    value: TextArg | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
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
    url: TextArg,
    max_nodes: int | str = _DEFAULT_SNAPSHOT_NODES,
    format: SnapshotFormat = _DEFAULT_SNAPSHOT_FORMAT,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> dict:
    """Open ``url`` and return the new page snapshot in one call.

    Combines ``browser("navigate", value=url)`` with ``browser_snapshot``:
    one tool call per URL instead of two, with fresh ``@refs`` for the page.
    ``format`` selects ``nodes`` (default), a compact ``text`` tree, or
    ``both``. Returns the ``browser_snapshot`` payload under ``action="open"``.
    """
    return _default_session.open(url, max_nodes, format, timeout)


def browser_snapshot(
    max_nodes: int | str = _DEFAULT_SNAPSHOT_NODES,
    format: SnapshotFormat = _DEFAULT_SNAPSHOT_FORMAT,
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
    value: TextArg | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
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
    value: TextArg | None = None,
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
    value: TextArg | None = None,
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
    quality: int | str = _DEFAULT_SCREENSHOT_QUALITY,
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


def browser_evaluate(script: TextArg, await_promise: bool = True) -> dict:
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
    fps: float = _DEFAULT_RECORDING_FPS,
    quality: int | str = _DEFAULT_SCREENSHOT_QUALITY,
    max_width: int | str | None = None,
    max_height: int | str | None = None,
) -> dict:
    """Start recording the current browser tab to an MP4 or WebM video.

    The recording uses Chrome's CDP screencast: frames are captured only when
    the page changes, acked immediately, and encoded with ``ffmpeg`` when
    ``browser_record_stop`` is called. ``fps`` caps how many frames per second
    are kept (1-60); ``quality`` applies to the intermediate JPEG frames.
    ``max_width`` and ``max_height`` downscale the capture when given.

    Without ``path``, an MP4 is written under the system temp directory.
    ``.mp4`` uses H.264 and ``.webm`` uses VP9; other suffixes are rejected.
    ``ffmpeg`` comes from PATH, or from the optional ``[ffmpeg]`` extra when
    PATH has none. The recording is video-only: page audio is not captured.

    Returns ``status``, ``path``, ``fps``, ``quality``, ``max_width``, and
    ``max_height``. Stop with ``browser_record_stop``; ``browser_close`` also
    stops and encodes an active recording.
    """
    return _record_start(_default_session, path, fps, quality, max_width, max_height)


def browser_record_stop() -> dict:
    """Stop the active recording and encode its frames into the video file.

    Returns ``status``, ``path``, ``format``, ``bytes``, ``frames``, and
    ``duration_s``. Fails when no recording is active or when ``ffmpeg``
    cannot encode the captured frames.
    """
    return _record_stop(_default_session)


def _close_payload(session: BrowserSession) -> dict:
    try:
        closed = session.close()
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return _error("close", exc)
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


def _load_tool() -> type[Tool]:
    """Import ``dspy.Tool`` on first use."""
    try:
        from dspy import Tool
    except ImportError as exc:
        raise ImportError(
            "dspy_agent_tools.Browser requires DSPy. Install it with: uv add dspy"
        ) from exc
    return Tool


def _bind(
    function: Callable[..., Any], method: Callable[..., Any]
) -> Callable[..., Any]:
    @functools.wraps(function)
    def bound(*args: Any, **kwargs: Any) -> Any:
        return method(*args, **kwargs)

    return bound


def _bind_close(session: BrowserSession) -> Callable[..., Any]:
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
        self, tools: Iterable[Any] = (), *, session: BrowserSession | None = None
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
    session: BrowserSession,
    *,
    include_search: bool = False,
    include_recording: bool = False,
) -> BrowserTools:
    _require_pydoll()
    Tool = _load_tool()
    functions: list[Callable[..., Any]] = [
        _bind(browser_open, session.open),
        _bind(browser, session.browser),
        _bind(browser_snapshot, session.snapshot),
        _bind(browser_tabs, session.tabs),
        _bind(browser_network, session.network),
        _bind(browser_cookies, session.cookies),
        _bind(browser_screenshot, session.screenshot),
        _bind(browser_evaluate, session.evaluate),
    ]
    if include_recording:
        functions.extend(
            [
                _bind(browser_record_start, session.record_start),
                _bind(browser_record_stop, session.record_stop),
            ]
        )
    functions.append(_bind_close(session))
    if include_search:
        from .search import web_search

        functions.append(web_search)
    return BrowserTools((Tool(function) for function in functions), session=session)


def Browser(
    *,
    session: BrowserSession | None = None,
    include_search: bool = False,
    include_recording: bool = False,
) -> BrowserTools:
    """Return the browser tools as a ready-to-use list of ``dspy.Tool`` objects.

    The default list contains ``browser_open``, ``browser``,
    ``browser_snapshot``, ``browser_tabs``, ``browser_network``,
    ``browser_cookies``, ``browser_screenshot``, ``browser_evaluate``, and
    ``browser_close``. Pass ``include_recording=True`` to add
    ``browser_record_start`` and ``browser_record_stop``, and
    ``include_search=True`` to append ``web_search`` for research agents that
    need both search and a real browser.

    By default the tools use the shared module-level session, matching the
    individual functions. Pass ``session=BrowserSession(...)`` to bind every
    tool, including ``browser_close`` and :meth:`BrowserTools.close`, to an
    isolated per-user or per-request session.
    """
    target_session = session if session is not None else _default_session
    return _build_tools(
        target_session,
        include_search=include_search,
        include_recording=include_recording,
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
