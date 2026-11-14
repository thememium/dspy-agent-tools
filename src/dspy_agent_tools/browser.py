"""Headless browser tools backed by pydoll.

The module exposes plain functions that can be passed straight to a DSPy
agent. Chrome starts on the first call and the same tab is reused afterwards,
so callers never create, enter, or clean up a browser session::

    import dspy
    from dspy_agent_tools import browser, browser_snapshot

    agent = dspy.ReAct(
        "task -> result",
        tools=[browser, browser_snapshot],
    )

``browser`` performs one action, ``browser_snapshot`` returns the current page
as structured nodes with ``@eN`` refs, ``browser_screenshot`` captures an image,
``browser_record_start``/``browser_record_stop`` capture a video,
``browser_evaluate`` runs JavaScript, and ``browser_close`` releases the
browser early. The browser is headless by default and is also stopped at
interpreter exit. Video recording needs ``ffmpeg`` on PATH.

For a server that needs one browser per user or session, create a
``BrowserSession`` per scope and call its methods instead of the module-level
functions::

    session = BrowserSession()
    session.browser("navigate", value="https://example.com")
    ...
    session.close()
"""

from __future__ import annotations

import atexit
import base64
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydoll.commands import PageCommands
from pydoll.constants import ScrollPosition
from pydoll.exceptions import BrowserException, ConnectionException, TargetCrashed
from pydoll.protocol.page.types import ScreencastFormat
from pydoll.protocol.runtime.types import CallArgument
from pydoll.sync import Chrome, ChromiumOptions, Key, PageEvent, SyncError

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

_DEFAULT_TIMEOUT_S = 15.0
_MAX_TIMEOUT_S = 120.0
_MAX_WAIT_S = 30.0
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
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_REF_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_INT_RE = re.compile(r"^[+-]?\d+$")
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
    if value is None or value == "":
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise _ToolError("wait requires a number of seconds in value") from exc
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
    if binary is None:
        raise _ToolError("video recording requires ffmpeg on PATH")
    return binary


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


def _click(element: Any) -> str:
    try:
        element.click()
        return "native"
    except Exception as native_error:  # noqa: BLE001 - fall back to a JS click
        try:
            element.click_using_js()
        except Exception:  # noqa: BLE001 - report the original native failure
            raise native_error from None
        return "js"


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


def _perform(
    tab: Any, action: str, target: str | None, value: str | None, timeout: float
) -> dict:
    if action == "navigate":
        url = _normalize_url(value)
        tab.go_to(url, timeout=max(1, int(timeout)))
        return {"status": "ok", "action": "navigate", **_page_info(tab)}

    if action == "click":
        element = _element(tab, target, timeout)
        method = _click(element)
        return {
            "status": "ok",
            "action": "click",
            "target": target,
            "method": method,
            **_page_info(tab),
        }

    if action == "type":
        if value is None:
            raise _ToolError("type requires value=text")
        element = _element(tab, target, timeout)
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
        return {
            "status": "ok",
            "action": "type",
            "target": target,
            "value_length": len(value),
            "method": method,
            **_page_info(tab),
        }

    if action == "press":
        _press(tab, value)
        return {"status": "ok", "action": "press", "key": value, **_page_info(tab)}

    if action == "hover":
        element = _element(tab, target, timeout)
        element.hover()
        return {"status": "ok", "action": "hover", "target": target, **_page_info(tab)}

    if action == "select":
        if value is None:
            raise _ToolError("select requires value=option value")
        element = _element(tab, target, timeout)
        selected = _select(element, value)
        return {
            "status": "ok",
            "action": "select",
            "target": target,
            "selected": selected,
            **_page_info(tab),
        }

    if action == "scroll":
        _scroll(tab, value)
        return {"status": "ok", "action": "scroll", "value": value, **_page_info(tab)}

    if action in ("back", "forward"):
        tab.execute_script(f"history.{action}()")
        time.sleep(0.25)
        return {"status": "ok", "action": action, **_page_info(tab)}

    if action == "reload":
        tab.refresh()
        return {"status": "ok", "action": "reload", **_page_info(tab)}

    if action == "wait":
        seconds = _coerce_seconds(value, 1.0)
        time.sleep(seconds)
        return {"status": "ok", "action": "wait", "seconds": seconds, **_page_info(tab)}

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
        return session._run(lambda tab: _perform(tab, action, target, value, timeout_s))
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return _error(action, exc, **extra)


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


def _snapshot(
    session: BrowserSession, max_nodes: int | str, include_text: bool
) -> dict:
    try:
        nodes_limit = _clamp_nodes(max_nodes)
        data = session._run(lambda tab: _snapshot_tab(tab, nodes_limit))
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return _error("snapshot", exc)
    nodes = data["nodes"]
    payload: dict[str, Any] = {
        "status": "ok",
        "action": "snapshot",
        "url": data.get("url", ""),
        "title": data.get("title", ""),
        "nodes": nodes,
        "node_count": len(nodes),
        "truncated": bool(data.get("truncated")),
    }
    if include_text:
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
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout: float = _DEFAULT_TIMEOUT_S,
        window_size: tuple[int, int] = _DEFAULT_WINDOW_SIZE,
    ) -> None:
        self.headless = headless
        self.timeout = timeout
        self.window_size = window_size
        self._lock = threading.RLock()
        self._browser: Any | None = None
        self._tab: Any | None = None
        self._recorder: _VideoRecorder | None = None
        self.last_recording: dict[str, Any] | None = None
        self._exit_hook_registered = False

    def _start(self) -> Any:
        options = ChromiumOptions()
        options.headless = self.headless
        options.start_timeout = max(10, int(self.timeout))
        options.add_argument(
            f"--window-size={self.window_size[0]},{self.window_size[1]}"
        )
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--hide-scrollbars")
        browser = Chrome(options=options)
        tab = browser.start()
        self._browser = browser
        self._tab = tab
        if not self._exit_hook_registered:
            _register_exit_hook(self.close)
            self._exit_hook_registered = True
        return tab

    def _get_tab(self) -> Any:
        if self._tab is None:
            return self._start()
        return self._tab

    def _reset(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            _discard_recording(recorder)
            self.last_recording = _error(
                "record_stop",
                _ToolError("browser disconnected; the recording was discarded"),
            )
        browser, self._browser, self._tab = self._browser, None, None
        if browser is not None:
            try:
                browser.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                logger.warning("Could not stop the browser: %s", _format_error(exc))

    def _run(self, operation: Any) -> Any:
        with self._lock:
            try:
                return operation(self._get_tab())
            except Exception as exc:
                if self._tab is None or not _is_disconnected(exc):
                    raise
                self._reset()
            return operation(self._get_tab())

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
            if browser is None:
                return False
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
        include_text: bool = True,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> dict:
        """Navigate to ``url`` and snapshot the new page; see ``browser_open``."""
        nav = self.browser("navigate", value=url, timeout=timeout)
        if nav["status"] != "ok":
            return {**nav, "action": "open"}
        return {**self.snapshot(max_nodes, include_text), "action": "open"}

    def snapshot(
        self,
        max_nodes: int | str = _DEFAULT_SNAPSHOT_NODES,
        include_text: bool = True,
    ) -> dict:
        """Return the current page as structured nodes; see ``browser_snapshot``."""
        return _snapshot(self, max_nodes, include_text)

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
    """Control the shared headless browser with one action per call.

    The first call starts headless Chrome; later calls reuse the same tab.
    Every action returns a dict with ``status``: ``ok`` or ``error`` (with an
    ``error`` message). Check ``status`` before relying on other fields.

    Actions:
        navigate: ``value`` is the URL. A missing scheme is filled in
            (``example.com`` -> ``https://example.com``; localhost uses http).
        click: ``target`` is an ``@ref`` from ``browser_snapshot``, a CSS
            selector, an XPath expression, ``css=...``, ``xpath=...``,
            ``text=Exact text``, or ``text*=Partial text``. Falls back to a
            JavaScript click when the native click is blocked.
        type: ``target`` plus ``value`` text. Clears the field first, then
            types, so the field ends up containing exactly ``value``.
        press: ``value`` is a key name (``Enter``, ``Tab``, ``Escape``,
            ``ArrowDown``) or a combination (``Control+A``).
        hover: ``target`` is moved over with the mouse.
        select: ``target`` is a ``<select>`` and ``value`` is the option value;
            fires ``input`` and ``change`` events.
        scroll: ``value`` is ``down``, ``up``, ``top``, ``bottom``, or a pixel
            count (positive scrolls down, negative up).
        back / forward / reload: navigate browser history or reload.
        wait: ``value`` is seconds to sleep (default 1, capped at 30).

    ``timeout`` bounds element lookup and page load (seconds, default 15).
    ``target`` and ``value`` also accept numbers, which are converted to text
    (``scroll`` with ``value=300`` scrolls 300 pixels).
    After navigation or a click, call ``browser_snapshot`` to see the new page
    and get fresh ``@refs``; refs are reassigned by every snapshot.
    """
    return _default_session.browser(action, target, value, timeout)


def browser_open(
    url: TextArg,
    max_nodes: int | str = _DEFAULT_SNAPSHOT_NODES,
    include_text: bool = True,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> dict:
    """Open ``url`` and return the new page snapshot in one call.

    Combines ``browser("navigate", value=url)`` with ``browser_snapshot``:
    one tool call per URL instead of two, with fresh ``@refs`` for the page.
    Returns the ``browser_snapshot`` payload (``url``, ``title``, ``nodes``,
    ``node_count``, ``truncated``, and ``text``) under ``action="open"``.
    """
    return _default_session.open(url, max_nodes, include_text, timeout)


def browser_snapshot(
    max_nodes: int | str = _DEFAULT_SNAPSHOT_NODES,
    include_text: bool = True,
) -> dict:
    """Return the current page as structured nodes for choosing the next action.

    Each node has ``ref`` (``@e1``), ``role``, ``name`` (accessible name),
    ``tag``, and ``depth``; inputs and links also carry ``value``, ``href``,
    ``checked``, ``disabled``, ``placeholder``, or ``expanded`` when relevant.
    Pass a node's ``ref`` as the ``target`` of ``browser`` to click, type,
    hover, or select it.

    Returns ``status``, ``url``, ``title``, ``nodes``, ``node_count``,
    ``truncated``, and, when ``include_text`` is true, ``text``: an indented
    tree of the same nodes that is cheaper to read than the JSON list.
    Interactive elements and landmarks are kept first when ``max_nodes``
    truncates a long page. Every call reassigns refs and drops the previous
    ones, so take a fresh snapshot after navigation or DOM changes.
    """
    return _default_session.snapshot(max_nodes, include_text)


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
    """Start recording the shared browser tab to an MP4 or WebM video.

    The recording uses Chrome's CDP screencast: frames are captured only when
    the page changes, acked immediately, and encoded with ``ffmpeg`` when
    ``browser_record_stop`` is called. ``fps`` caps how many frames per second
    are kept (1-60); ``quality`` applies to the intermediate JPEG frames.
    ``max_width`` and ``max_height`` downscale the capture when given.

    Without ``path``, an MP4 is written under the system temp directory.
    ``.mp4`` uses H.264 and ``.webm`` uses VP9; other suffixes are rejected.
    ``ffmpeg`` must be installed and on PATH. The recording is video-only:
    page audio is not captured.

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


def browser_close() -> dict:
    """Stop the shared browser early and release Chrome and its profile.

    Optional: the browser also stops at interpreter exit, and the next tool
    call after this starts a fresh one. Returns ``closed``: whether a browser
    was running. If a recording was active, it is stopped and encoded, and
    its result is included under ``recording``.
    """
    try:
        closed = _default_session.close()
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return _error("close", exc)
    payload: dict[str, Any] = {
        "status": "ok",
        "action": "close",
        "closed": closed,
    }
    recording = _default_session.take_last_recording()
    if recording is not None:
        payload["recording"] = recording
    return payload


__all__ = [
    "BrowserSession",
    "browser",
    "browser_close",
    "browser_evaluate",
    "browser_open",
    "browser_record_start",
    "browser_record_stop",
    "browser_screenshot",
    "browser_snapshot",
]
