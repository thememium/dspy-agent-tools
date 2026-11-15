"""Recorded network request inspection."""

from __future__ import annotations

from . import (
    coerce,
    config,
    errors,
    session,
    tabs,
)


def _network_action(
    session: session.BrowserSession,
    action: str,
    value: config.TextArg | None,
    limit: int | str,
) -> dict:
    action = str(action).strip().lower()
    if action not in ("requests", "clear", "body"):
        return errors._error(
            "network", errors._ToolError("action must be requests, clear, or body")
        )
    text = coerce._text_arg(value)
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
                    raise errors._ToolError(
                        "network body requires value=request id from browser_network"
                    )
                body = session._run(lambda tab: tab.get_network_response_body(text))
                body = "" if body is None else str(body)
                return {
                    "status": "ok",
                    "action": "body",
                    "id": text,
                    "body": body[: config._MAX_NETWORK_BODY],
                    "truncated": len(body) > config._MAX_NETWORK_BODY,
                    **session.take_diagnostics(),
                }
            session._run(lambda tab: None)
            entries = session._watch.network(tabs._coerce_limit(limit), text)
            return {
                "status": "ok",
                "action": "requests",
                "count": len(entries),
                "requests": entries,
                **session.take_diagnostics(),
            }
        except Exception as exc:  # noqa: BLE001 - returned as tool status
            payload = errors._error("network", exc)
            payload.update(session.take_diagnostics())
            return payload
