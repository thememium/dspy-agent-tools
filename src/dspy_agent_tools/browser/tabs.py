"""Browser tab management."""

from __future__ import annotations

from typing import Any

from . import (
    actions,
    coerce,
    config,
    errors,
    session,
)


def _tab_count(session: session.BrowserSession) -> int | None:
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
        raise errors._ToolError("limit must be an integer") from exc
    return max(1, min(limit, config._MAX_NETWORK_ITEMS))


def _tab_text(tab: Any) -> str:
    """Lowercased URL and title of a tab, for matching by substring."""
    parts: list[str] = []
    for name in ("current_url", "title"):
        try:
            parts.append(str(getattr(tab, name)()))
        except Exception:  # noqa: BLE001, S112 - a tab may have gone away
            continue
    return " ".join(parts).lower()


def _resolve_tab(
    session: session.BrowserSession, tabs: list[Any], value: str | None
) -> Any:
    if not tabs:
        raise errors._ToolError("no tabs are open; use browser_tabs with action=new")
    text = (value or "").strip()
    if not text:
        return session._tab if session._tab is not None else tabs[-1]
    if config._INT_RE.fullmatch(text):
        index = int(text)
        if not 0 <= index < len(tabs):
            raise errors._ToolError(
                f"tab index {index} is out of range; {len(tabs)} tabs are open"
            )
        return tabs[index]
    needle = text.lower()
    matches = [tab for tab in tabs if needle in _tab_text(tab)]
    if not matches:
        raise errors._ToolError(
            f"no tab matches {value!r}; call browser_tabs to list them"
        )
    if len(matches) > 1:
        raise errors._ToolError(
            f"{len(matches)} tabs match {value!r}; pass an index from browser_tabs"
        )
    return matches[0]


def _tabs_action(
    session: session.BrowserSession,
    action: str,
    value: config.TextArg | None,
    timeout: float,
) -> dict:
    action = str(action).strip().lower()
    if action not in ("list", "new", "switch", "close"):
        return errors._error(
            "tabs", errors._ToolError("action must be list, new, switch, or close")
        )
    text = coerce._text_arg(value)
    with session._lock:
        try:
            timeout_s = coerce._coerce_timeout(timeout, session.timeout)
            tabs = session._tab_list()
            closed_all = False
            if action == "new":
                browser = session._browser
                assert browser is not None  # _tab_list started it above
                url = session._check_url(coerce._normalize_url(text)) if text else None
                tab = browser.new_tab()
                session._tab = tab
                session._install_watch(tab)
                if url is not None:
                    tab.go_to(url, timeout=max(1, int(timeout_s)))
                    actions._settle(tab, timeout_s)
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
            payload = errors._error("tabs", exc)
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
