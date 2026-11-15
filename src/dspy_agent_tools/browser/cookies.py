"""Cookie reading, writing, and persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import coerce, config, errors, session


def _cookie_list(value: str | None) -> list[dict[str, Any]]:
    """Parse a JSON cookie list, a ``Cookie:`` header, or a saved state file."""
    text = (value or "").strip()
    if not text:
        raise errors._ToolError(
            "cookies set requires value=JSON or a Cookie header such as a=1; b=2"
        )
    if text.startswith(("[", "{")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise errors._ToolError(
                f"cookies value is not valid JSON: {exc.msg}"
            ) from exc
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        if not isinstance(data, list) or not data:
            raise errors._ToolError("cookies JSON must be a list of cookie objects")
        cookies: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict) or "name" not in item or "value" not in item:
                raise errors._ToolError("each cookie needs a name and a value")
            cookies.append(
                {key: item[key] for key in config._COOKIE_FIELDS if key in item}
            )
        return cookies
    cookies = []
    for part in text.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if not separator or not name.strip():
            continue
        cookies.append({"name": name.strip(), "value": cookie_value.strip()})
    if not cookies:
        raise errors._ToolError("the cookie header did not contain any name=value pair")
    return cookies


def _cookie_records(cookies: Any) -> list[dict[str, Any]]:
    if not isinstance(cookies, list):
        return []
    return [
        {key: cookie[key] for key in config._COOKIE_FIELDS if key in cookie}
        for cookie in cookies
        if isinstance(cookie, dict)
    ]


def _cookie_state(tab: Any) -> dict[str, Any]:
    """Snapshot the tab's cookies in the on-disk state format."""
    return {"url": tab.current_url(), "cookies": _cookie_records(tab.get_cookies())}


def _write_cookie_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _save_cookie_state(tab: Any, path: Path) -> None:
    _write_cookie_state(path, _cookie_state(tab))


def _load_cookie_state(tab: Any, path: Path) -> None:
    """Restore a state file written by ``_save_cookie_state``."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as exc:
        config.logger.warning(
            "Could not read the cookie state %s: %s", path, errors._format_error(exc)
        )
        return
    raw = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(raw, list) or not raw:
        return
    cookies = _cookie_list(json.dumps({"cookies": raw}))
    default_url = state.get("url")
    for cookie in cookies:
        if "url" not in cookie and "domain" not in cookie and default_url:
            cookie["url"] = default_url
    tab.set_cookies(cookies)


def _persist_cookie_state(session: session.BrowserSession) -> None:
    """Write the session's cookie state file; never fail the calling tool."""
    if session.cookie_state is None or session._tab is None:
        return
    try:
        _save_cookie_state(session._tab, session.cookie_state)
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        config.logger.warning(
            "Could not save the cookie state: %s", errors._format_error(exc)
        )


def _cookies_action(
    session: session.BrowserSession, action: str, value: config.TextArg | None
) -> dict:
    action = str(action).strip().lower()
    if action not in ("get", "set", "clear", "save", "load"):
        return errors._error(
            "cookies",
            errors._ToolError("action must be get, set, clear, save, or load"),
        )
    text = coerce._text_arg(value)
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
                cookies = cookies[-config._MAX_COOKIE_ITEMS :]
                return {
                    "status": "ok",
                    "action": "get",
                    "count": len(cookies),
                    "cookies": cookies,
                    **session.take_diagnostics(),
                }
            if action == "clear":
                session._run(lambda tab: tab.delete_all_cookies())
                _persist_cookie_state(session)
                return {
                    "status": "ok",
                    "action": "clear",
                    **session.take_diagnostics(),
                }
            if action == "save":
                if not text:
                    raise errors._ToolError("cookies save requires value=path")
                target = Path(text).expanduser()
                state = session._run(_cookie_state)
                _write_cookie_state(target, state)
                return {
                    "status": "ok",
                    "action": "save",
                    "path": str(target.absolute()),
                    "count": len(state["cookies"]),
                }
            if action == "load":
                if not text:
                    raise errors._ToolError("cookies load requires value=path")
                source = Path(text).expanduser()
                try:
                    state = json.loads(source.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise errors._ToolError(f"could not read {source}: {exc}") from exc
                except json.JSONDecodeError as exc:
                    raise errors._ToolError(
                        f"{source} is not valid JSON: {exc.msg}"
                    ) from exc
                result = _apply_cookies(session, _cookie_list(json.dumps(state)))
                _persist_cookie_state(session)
                return result
            result = _apply_cookies(session, _cookie_list(text))
            _persist_cookie_state(session)
            return result
        except Exception as exc:  # noqa: BLE001 - returned as tool status
            payload = errors._error("cookies", exc)
            payload.update(session.take_diagnostics())
            return payload


def _apply_cookies(
    session: session.BrowserSession, cookies: list[dict[str, Any]]
) -> dict:
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
