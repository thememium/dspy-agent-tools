"""Browser action implementations."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from . import (
    _pydoll,
    coerce,
    config,
    dom,
    errors,
    session,
    tabs,
)


def _wait(
    tab: Any, target: str | None, value: str | None, timeout: float
) -> dict[str, Any]:
    """Sleep, or wait for a selector, text, or URL prefix."""
    budget = min(timeout, config._MAX_WAIT_S)
    if isinstance(target, str) and target.strip():
        return _wait_for(tab, "selector", dom._selector(target), target, budget)
    text = (value or "").strip()
    if not text or config._INT_RE.fullmatch(text) or config._FLOAT_RE.fullmatch(text):
        seconds = coerce._coerce_seconds(text or None, 1.0)
        time.sleep(seconds)
        return {
            "status": "ok",
            "action": "wait",
            "seconds": seconds,
            **session._page_info(tab),
        }
    if text.startswith("selector="):
        selector = text[len("selector=") :].strip()
        if not selector:
            raise errors._ToolError("selector= requires a CSS or XPath selector")
        return _wait_for(tab, "selector", selector, selector, budget)
    for prefix in ("text*=", "text="):
        if text.startswith(prefix):
            needle = text[len(prefix) :]
            if not needle:
                raise errors._ToolError(f"{prefix} requires text to wait for")
            return _wait_for(tab, "text", needle, f"text {needle!r}", budget)
    if text.startswith("url=") or config._SCHEME_RE.match(text):
        prefix = text[len("url=") :].strip() if text.startswith("url=") else text
        prefix = prefix.rstrip("*")
        if not prefix:
            raise errors._ToolError("url= requires a URL prefix to wait for")
        return _wait_for(tab, "url", prefix, f"url starting with {prefix!r}", budget)
    raise errors._ToolError(
        "wait value must be seconds, text=..., url=..., or selector=...; "
        "or pass target=selector"
    )


def _wait_for(
    tab: Any, kind: str, needle: str, description: str, timeout: float
) -> dict[str, Any]:
    if kind == "selector":
        script = config._WAIT_SELECTOR_JS.replace("__SELECTOR__", json.dumps(needle))
    elif kind == "text":
        script = config._WAIT_TEXT_JS.replace("__TEXT__", json.dumps(needle))
    else:
        script = config._WAIT_URL_JS.replace("__PREFIX__", json.dumps(needle))
    try:
        tab.wait_for_script(script, timeout=timeout)
    except _pydoll.TimeoutException as exc:
        raise errors._ToolError(
            f"timed out after {timeout:g}s waiting for {description}"
        ) from exc
    return {
        "status": "ok",
        "action": "wait",
        "waited_for": description,
        "timeout_s": timeout,
        **session._page_info(tab),
    }


def _press(tab: Any, value: str | None) -> None:
    if not isinstance(value, str) or value == "":
        raise errors._ToolError(
            "press requires value=Key, for example Enter or Control+A"
        )
    parts = (
        [" "]
        if value == " "
        else [part.strip() for part in value.split("+") if part.strip()]
    )
    if not 1 <= len(parts) <= 3:
        raise errors._ToolError(
            "press accepts one key or a combination of up to three, e.g. Enter or Control+A"
        )
    keys = [_key(part) for part in parts]
    if len(keys) == 1:
        tab.keyboard.press(keys[0])
    elif len(keys) == 2:
        tab.keyboard.hotkey(keys[0], keys[1])
    else:
        tab.keyboard.hotkey(keys[0], keys[1], keys[2])


def _key(value: str) -> _pydoll.Key:
    if value == " ":
        return _pydoll.Key.SPACE
    raw = value.strip()
    normalized = re.sub(r"[\s_-]+", "", raw).upper()
    normalized = config._KEY_ALIASES.get(normalized, normalized)
    key = _pydoll.Key.__members__.get(normalized)
    if key is not None:
        return key
    raise errors._ToolError(
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
        position = (
            _pydoll.ScrollPosition.UP
            if direction == "up"
            else _pydoll.ScrollPosition.DOWN
        )
        tab.scroll.by(position, config._DEFAULT_SCROLL_PX, smooth=False)
        return
    if config._INT_RE.fullmatch(direction):
        pixels = int(direction)
        if pixels == 0:
            raise errors._ToolError("scroll pixels cannot be zero")
        position = (
            _pydoll.ScrollPosition.DOWN if pixels > 0 else _pydoll.ScrollPosition.UP
        )
        tab.scroll.by(position, abs(pixels), smooth=False)
        return
    raise errors._ToolError(
        "scroll value must be down, up, top, bottom, or a pixel count"
    )


def _select(element: Any, value: str) -> Any:
    response = element.execute_script(
        "this.value = arguments[0];"
        "this.dispatchEvent(new Event('input', {bubbles: true}));"
        "this.dispatchEvent(new Event('change', {bubbles: true}));"
        "return this.value;",
        arguments=[_pydoll.CallArgument(value=value)],
        return_by_value=True,
    )
    return _remote_value(response)


def _remote_value(response: Any) -> Any:
    if not isinstance(response, dict):
        raise errors._ToolError("Browser returned an unexpected script response")
    payload = response.get("result")
    if not isinstance(payload, dict):
        raise errors._ToolError("Browser returned an unexpected script response")
    if "exceptionDetails" in payload:
        details = payload["exceptionDetails"]
        exception = details.get("exception") or {}
        text = (
            exception.get("description")
            or details.get("text")
            or "JavaScript exception"
        )
        raise errors._ToolError(f"JavaScript error: {text}")
    remote = payload.get("result") or {}
    if "value" in remote:
        return remote["value"]
    if "unserializableValue" in remote:
        return remote["unserializableValue"]
    return remote.get("description")


def _settle(tab: Any, timeout: float, *, previous_url: str | None = None) -> bool:
    """Wait briefly for navigation and in-flight requests after an action."""
    budget = max(0.1, min(timeout, config._SETTLE_TIMEOUT_S))
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
            idle_time=min(config._SETTLE_IDLE_S, remaining), timeout=remaining
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
            **session._page_info(tab),
        }

    if action == "click":
        element = dom._element(tab, target, timeout)
        dom._require_actionable(element, "click")
        before = dom._probe(tab)
        method, method_warning = dom._click(element)
        settled = _settle(tab, timeout)
        effect = dom._effect(tab, before)
        payload: dict[str, Any] = {
            "status": "ok",
            "action": "click",
            "target": target,
            "method": method,
            "settled": settled,
            **session._page_info(tab),
            **effect,
        }
        warnings = [text for text in (method_warning, effect.get("warning")) if text]
        if warnings:
            payload["warning"] = "; ".join(warnings)
        return payload

    if action == "type":
        if value is None:
            raise errors._ToolError("type requires value=text")
        element = dom._element(tab, target, timeout)
        dom._require_actionable(element, "type")
        if not dom._probe_bool(element, "is_editable"):
            raise errors._ToolError(
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
            **session._page_info(tab),
        }
        payload.update(dom._verify_typed(dom._field_state(element), value))
        return payload

    if action == "press":
        before = dom._probe(tab)
        _press(tab, value)
        settled = _settle(tab, timeout)
        return {
            "status": "ok",
            "action": "press",
            "key": value,
            "settled": settled,
            **session._page_info(tab),
            **dom._effect(tab, before),
        }

    if action == "hover":
        element = dom._element(tab, target, timeout)
        dom._require_actionable(element, "hover")
        element.hover()
        return {
            "status": "ok",
            "action": "hover",
            "target": target,
            **session._page_info(tab),
        }

    if action == "select":
        if value is None:
            raise errors._ToolError("select requires value=option value")
        element = dom._element(tab, target, timeout)
        dom._require_actionable(element, "select")
        selected = _select(element, value)
        settled = _settle(tab, timeout)
        payload = {
            "status": "ok",
            "action": "select",
            "target": target,
            "selected": selected,
            "settled": settled,
            **session._page_info(tab),
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
        return {
            "status": "ok",
            "action": "scroll",
            "value": value,
            **session._page_info(tab),
        }

    if action in ("back", "forward"):
        previous_url = session._page_info(tab).get("url")
        tab.execute_script(f"history.{action}()")
        settled = _settle(tab, timeout, previous_url=previous_url)
        return {
            "status": "ok",
            "action": action,
            "settled": settled,
            **session._page_info(tab),
        }

    if action == "reload":
        tab.refresh()
        settled = _settle(tab, timeout)
        return {
            "status": "ok",
            "action": "reload",
            "settled": settled,
            **session._page_info(tab),
        }

    if action == "wait":
        return _wait(tab, target, value, timeout)

    raise errors._ToolError(
        f"Unknown action {action!r}"
    )  # pragma: no cover - validated earlier


def _browser_action(
    session: session.BrowserSession,
    action: str,
    target: config.TextArg | None,
    value: config.TextArg | None,
    timeout: float | None,
) -> dict:
    action = str(action).strip().lower()
    target = coerce._text_arg(target)
    value = coerce._text_arg(value)
    if action not in config._ACTIONS:
        return errors._error(
            action,
            errors._ToolError(
                f"Unknown action {action!r}. Use one of: {', '.join(config._ACTIONS)}."
            ),
        )
    extra = {"target": target} if target else {}
    try:
        timeout_s = coerce._coerce_timeout(timeout, session.timeout)
        url = (
            session._check_url(coerce._normalize_url(value))
            if action == "navigate"
            else None
        )
        before = tabs._tab_count(session) if action in config._TAB_ACTIONS else None
        payload = session._run(
            lambda tab: _perform(tab, action, target, value, timeout_s, url=url)
        )
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        extra.update(session.take_diagnostics())
        return errors._error(action, exc, **extra)
    if before is not None and payload.get("status") == "ok":
        after = tabs._tab_count(session)
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
