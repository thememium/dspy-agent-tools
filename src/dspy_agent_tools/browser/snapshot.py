"""Page snapshot and JavaScript evaluation."""

from __future__ import annotations

import json
from typing import Any

from . import actions, config, errors, session


def _clamp_nodes(value: int | str) -> int:
    try:
        nodes = int(value)
    except (TypeError, ValueError) as exc:
        raise errors._ToolError("max_nodes must be an integer") from exc
    return max(1, min(nodes, config._MAX_SNAPSHOT_NODES))


def _snapshot_tab(tab: Any, max_nodes: int) -> dict:
    script = config._SNAPSHOT_JS.replace("__MAX_NODES__", str(max_nodes)).replace(
        "__MAX_NAME__", str(config._MAX_NAME_CHARS)
    )
    response = tab.execute_script(
        script,
        return_by_value=True,
        allow_unsafe_eval_blocked_by_csp=True,
    )
    data = actions._remote_value(response)
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise errors._ToolError("Snapshot script returned an unexpected shape")
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


def _coerce_snapshot_format(
    value: config.SnapshotFormat | str,
) -> config.SnapshotFormat:
    if value not in ("nodes", "text", "both"):
        raise errors._ToolError("format must be nodes, text, or both")
    return value


def _snapshot(
    session: session.BrowserSession,
    max_nodes: int | str,
    format: config.SnapshotFormat | str,
) -> dict:
    try:
        nodes_limit = _clamp_nodes(max_nodes)
        view = _coerce_snapshot_format(format)
        data = session._run(lambda tab: _snapshot_tab(tab, nodes_limit))
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return errors._error("snapshot", exc)
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


def _evaluate_tab(tab: Any, script: config.TextArg, await_promise: bool) -> dict:
    if not isinstance(script, str) or not script.strip():
        raise errors._ToolError("evaluate requires a non-empty JavaScript string")
    response = tab.execute_script(
        script,
        return_by_value=True,
        await_promise=await_promise,
        allow_unsafe_eval_blocked_by_csp=True,
    )
    value = actions._remote_value(response)
    payload = response.get("result", {})
    remote = payload.get("result", {})
    data: dict[str, Any] = {
        "result": value,
        "type": remote.get("type"),
    }
    data.update(session._page_info(tab))
    return data


def _evaluate(
    session: session.BrowserSession, script: config.TextArg, await_promise: bool
) -> dict:
    try:
        data = session._run(lambda tab: _evaluate_tab(tab, script, await_promise))
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return errors._error("evaluate", exc)
    return {"status": "ok", "action": "evaluate", **data}
