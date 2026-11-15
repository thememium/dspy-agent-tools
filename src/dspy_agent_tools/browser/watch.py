"""Page console, error, and dialog collection for one tab."""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from . import config, errors


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
    return " ".join(parts)[: config._MAX_CONSOLE_TEXT]


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
    return merged[-config._MAX_CONSOLE_ITEMS :]


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
            maxlen=config._MAX_CONSOLE_ENTRIES
        )
        self._errors: deque[tuple[int, dict[str, Any]]] = deque(
            maxlen=config._MAX_CONSOLE_ENTRIES
        )
        self._dialogs: deque[tuple[int, dict[str, Any]]] = deque(
            maxlen=config._MAX_CONSOLE_ENTRIES
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
            config.logger.debug(
                "Could not record a console event: %s", errors._format_error(exc)
            )

    def on_exception(self, event: dict) -> None:
        try:
            params = event.get("params") or {}
            details = params.get("exceptionDetails") or {}
            exception = details.get("exception") or {}
            text = exception.get("description") or details.get("text") or ""
            text = " ".join(str(text).split())[: config._MAX_CONSOLE_TEXT]
            if not text:
                return
            with self._lock:
                self._errors.append((self._seq(), {"text": text}))
        except Exception as exc:  # noqa: BLE001 - one event must not stop the collector
            config.logger.debug(
                "Could not record a page error: %s", errors._format_error(exc)
            )

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
                while len(self._network_order) > config._MAX_NETWORK_ENTRIES:
                    self._network.pop(self._network_order.popleft(), None)
        except Exception as exc:  # noqa: BLE001 - one event must not stop the collector
            config.logger.debug(
                "Could not record a request: %s", errors._format_error(exc)
            )

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
            config.logger.debug(
                "Could not record a response: %s", errors._format_error(exc)
            )

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
            config.logger.debug(
                "Could not record a failure: %s", errors._format_error(exc)
            )

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
            "message": str(params.get("message") or "")[: config._MAX_DIALOG_CHARS],
            "handled": "pending",
        }
        prompt = params.get("defaultPrompt")
        if prompt:
            entry["default_prompt"] = str(prompt)[: config._MAX_DIALOG_CHARS]
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
            handled = f"failed: {errors._format_error(exc)}"
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
