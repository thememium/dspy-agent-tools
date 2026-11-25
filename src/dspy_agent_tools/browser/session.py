"""Browser session lifecycle."""

from __future__ import annotations

import atexit
import json
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import (
    _pydoll,
    actions,
    config,
    cookies,
    errors,
    network,
    recording,
    screenshot,
    snapshot,
    tabs,
    watch,
)

if TYPE_CHECKING:
    from .tools import BrowserTools


def _close_browser_gracefully(browser: Any) -> None:
    """Ask Chrome to exit on its own so a persistent profile flushes to disk.

    pydoll ends the process as soon as ``Browser.close`` returns, which leaves
    the cookie database in a journal and loses the login the profile was meant
    to keep. Waiting here gives Chrome the moment it needs to commit.
    """
    try:
        browser.execute_command(_pydoll.BrowserCommands.close(), timeout=3)
    except Exception:  # noqa: BLE001 - the process is stopped either way
        return
    time.sleep(config._PROFILE_FLUSH_S)


def _is_disconnected(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            _pydoll.BrowserException,
            _pydoll.ConnectionException,
            _pydoll.TargetCrashed,
            _pydoll.SyncError,
        ),
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
    to reject explicit normalized navigation URLs. These checks do not cover
    redirects, clicks, scripts, popups, or subresources and are not a security
    boundary. Use network-level isolation for untrusted browsing.

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

    ``cookie_state`` is the portable alternative: a JSON file of cookies,
    loaded when the browser starts and rewritten after any cookie change and on
    close. It needs no profile, and it is the same format as
    ``browser_cookies("save", path)``, so the two can be used together.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout: float = config._DEFAULT_TIMEOUT_S,
        window_size: tuple[int, int] = config._DEFAULT_WINDOW_SIZE,
        allowed_schemes: Iterable[str] | None = None,
        url_policy: Callable[[str], bool] | None = None,
        dialog_policy: config.DialogPolicy = config._DEFAULT_DIALOG_POLICY,
        user_data_dir: str | Path | None = None,
        cookie_state: str | Path | None = None,
        tabs_state: str | Path | None = None,
        state_path: str | Path | None = None,
        humanize: bool = False,
        stealth: bool = False,
        fingerprint: dict[str, Any] | None = None,
        ws_address: str | None = None,
    ) -> None:
        if dialog_policy not in ("dismiss", "accept"):
            raise ValueError("dialog_policy must be dismiss or accept")
        if ws_address and (user_data_dir or cookie_state or fingerprint):
            raise ValueError(
                "ws_address cannot be combined with profile, cookie state, or fingerprint"
            )
        self.ws_address = ws_address
        self.humanize = humanize or stealth
        self.stealth = stealth
        self.fingerprint = fingerprint
        self.headless = headless
        self.timeout = timeout
        self.window_size = window_size
        self.dialog_policy: config.DialogPolicy = dialog_policy
        self.user_data_dir = (
            Path(user_data_dir).expanduser().resolve() if user_data_dir else None
        )
        self.cookie_state = (
            Path(cookie_state).expanduser().resolve() if cookie_state else None
        )
        self.tabs_state = (
            Path(tabs_state).expanduser().resolve() if tabs_state else None
        )
        self.state_path = (
            Path(state_path).expanduser().resolve() if state_path else None
        )
        if allowed_schemes is None:
            self.allowed_schemes = config._DEFAULT_ALLOWED_SCHEMES
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
        self._watch = watch._PageWatch()
        self._watched: set[str] = set()
        self._snapshot_refs: dict[str, set[str]] = {}
        self._recorder: recording._VideoRecorder | None = None
        self.last_recording: dict[str, Any] | None = None
        self._exit_hook_registered = False

    def _check_url(self, url: str) -> str:
        scheme = urlsplit(url).scheme.lower()
        if scheme not in self.allowed_schemes:
            allowed = ", ".join(sorted(self.allowed_schemes)) or "none"
            raise errors._ToolError(
                f"URL scheme {scheme or '(none)'} is not allowed; allowed schemes: {allowed}"
            )
        if self.url_policy is not None and not self.url_policy(url):
            raise errors._ToolError("URL rejected by the browser URL policy")
        return url

    def _start(self) -> Any:
        _pydoll.require()
        options = _pydoll.ChromiumOptions()
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
        browser = _pydoll.Chrome(options=options)
        tab = browser.connect(self.ws_address) if self.ws_address else browser.start()
        self._browser = browser
        self._tab = tab
        self._install_watch(tab)
        if self.cookie_state is not None:
            try:
                cookies._load_cookie_state(tab, self.cookie_state)
            except Exception as exc:  # noqa: BLE001 - startup must not depend on it
                config.logger.warning(
                    "Could not load the cookie state: %s", errors._format_error(exc)
                )
        if not self._exit_hook_registered:
            _register_exit_hook(self.close)
            self._exit_hook_registered = True
        return tab

    def _install_watch(self, tab: Any) -> None:
        """Subscribe *tab* to console, error, dialog, and network events."""
        watch = self._watch
        target = getattr(tab, "target_id", None)
        if target is not None and target in self._watched:
            return

        if self.fingerprint is not None:
            tab.apply_fingerprint(self.fingerprint)
        if target is not None:
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
                        _pydoll.PageEvent.JAVASCRIPT_DIALOG_OPENING,
                        lambda event: watch.on_dialog(event, responder),
                    ),
                    tab.on(_pydoll.RuntimeEvent.CONSOLE_API_CALLED, watch.on_console),
                    tab.on(_pydoll.RuntimeEvent.EXCEPTION_THROWN, watch.on_exception),
                    tab.on(_pydoll.NetworkEvent.REQUEST_WILL_BE_SENT, watch.on_request),
                    tab.on(_pydoll.NetworkEvent.RESPONSE_RECEIVED, watch.on_response),
                    tab.on(_pydoll.NetworkEvent.LOADING_FAILED, watch.on_failed),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics are best-effort
            config.logger.warning(
                "Could not enable page diagnostics: %s", errors._format_error(exc)
            )

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
            recording._discard_recording(recorder)
            self.last_recording = errors._error(
                "record_stop",
                errors._ToolError("browser disconnected; the recording was discarded"),
            )
        if self._tab is not None:
            cookies._persist_cookie_state(self)
        browser, self._browser, self._tab = self._browser, None, None
        self._watch = watch._PageWatch()
        self._watched = set()
        self._snapshot_refs = {}
        if browser is not None:
            if self.user_data_dir is not None:
                _close_browser_gracefully(browser)
            try:
                if self.ws_address:
                    browser._run(browser._impl._release_browser())
                else:
                    browser.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                config.logger.warning(
                    "Could not stop the browser: %s", errors._format_error(exc)
                )

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
            if self.state_path is not None:
                try:
                    self.save_state()
                except Exception as exc:  # noqa: BLE001 - persistence is best-effort
                    config.logger.warning(
                        "Could not save browser state: %s", errors._format_error(exc)
                    )
            return result

    def _storage_state(self, tab: Any) -> dict[str, Any]:
        try:
            response = tab.execute_script(
                "JSON.stringify({local: Object.fromEntries(Object.entries(localStorage)),"
                " session: Object.fromEntries(Object.entries(sessionStorage))})",
                return_by_value=True,
                await_promise=True,
                allow_unsafe_eval_blocked_by_csp=True,
            )
            value = actions._remote_value(response)
            return json.loads(value) if isinstance(value, str) else {}
        except Exception:  # noqa: BLE001 - storage is best-effort
            return {}

    def capture_state(self) -> dict[str, Any]:
        """Capture tabs, URLs, cookies, and per-tab local/session storage."""
        with self._lock:
            tabs: list[dict[str, Any]] = []
            for index, tab in enumerate(self._tab_list()):
                entry = self._tab_entry(index, tab)
                tabs.append(
                    {
                        "url": entry.get("url"),
                        "active": entry.get("active", False),
                        "storage": self._storage_state(tab),
                    }
                )
            cookie_rows = (
                cookies._cookie_state(self._tab)["cookies"] if self._tab else []
            )
        return {"version": 1, "tabs": tabs, "cookies": cookie_rows}

    def save_state(self, path: str | Path | None = None) -> dict[str, Any]:
        """Write the serializable browser state to ``state_path`` or *path*."""
        target = Path(path).expanduser() if path is not None else self.state_path
        if target is None:
            raise ValueError("save_state() needs a path or state_path=")
        state = self.capture_state()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return {"status": "ok", "action": "save_state", "path": str(target)}

    def restore_state(self, path: str | Path | None = None) -> dict[str, Any]:
        """Restore tabs, cookies, and storage from a captured state file."""
        target = Path(path).expanduser() if path is not None else self.state_path
        if target is None:
            raise ValueError("restore_state() needs a path or state_path=")
        state = json.loads(target.read_text(encoding="utf-8"))
        tabs = state.get("tabs")
        if state.get("version") != 1 or not isinstance(tabs, list) or not tabs:
            raise ValueError("invalid browser state")
        for entry in tabs:
            url = entry.get("url") if isinstance(entry, dict) else None
            if not isinstance(url, str):
                raise TypeError("invalid browser tab state")
            self._check_url(url)
        cookie_rows = state.get("cookies") or []
        with self._lock:
            self._get_tab()
            browser = self._browser
            if browser is None:
                raise RuntimeError("browser failed to start")
            opened = []
            for entry in tabs:
                tab = browser.new_tab()
                self._install_watch(tab)
                opened.append(tab)
                tab.go_to(entry["url"], timeout=max(1, int(self.timeout)))
                if cookie_rows:
                    tab.set_cookies(cookie_rows)
                storage = entry.get("storage") or {}
                local = json.dumps(storage.get("local") or {})
                session = json.dumps(storage.get("session") or {})
                tab.execute_script(
                    f"Object.entries({local}).forEach(([k,v])=>localStorage.setItem(k,v));"
                    f"Object.entries({session}).forEach(([k,v])=>sessionStorage.setItem(k,v));",
                    await_promise=True,
                )
            active = next(
                (
                    index
                    for index, entry in enumerate(tabs)
                    if entry.get("active") is True
                ),
                0,
            )
            self._tab = opened[active]
        return {"status": "ok", "action": "restore_state", "count": len(opened)}

    def recover(self) -> dict[str, Any]:
        """Re-open the browser and restore the last captured state."""
        with self._lock:
            if self.state_path is not None and self.state_path.exists():
                return self.restore_state()
            if self.tabs_state is not None and self.tabs_state.exists():
                return self.restore_tabs(str(self.tabs_state))
            self._get_tab()
        return {"status": "ok", "action": "recover", "browser": True}

    def close(self) -> bool:
        """Stop the browser if it is running; return whether one was open."""
        with self._lock:
            if self.state_path is not None and self._tab is not None:
                try:
                    self.save_state()
                except Exception as exc:  # noqa: BLE001 - persistence is best-effort
                    config.logger.warning(
                        "Could not save browser state: %s", errors._format_error(exc)
                    )
            if self.tabs_state is not None and self._tab is not None:
                self.save_tabs(str(self.tabs_state))
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                if recorder.tab is not None:
                    recording._stop_screencast(recorder.tab, recorder)
                try:
                    data = recording._finalize_recording(recorder)
                except Exception as exc:  # noqa: BLE001 - returned by browser_close
                    self.last_recording = errors._error("record_stop", exc)
                else:
                    self.last_recording = {
                        "status": "ok",
                        "action": "record_stop",
                        **data,
                    }
            else:
                self.last_recording = None
            if self._tab is not None:
                cookies._persist_cookie_state(self)
            browser, self._browser, self._tab = self._browser, None, None
            self._watch = watch._PageWatch()
            self._watched = set()
            self._snapshot_refs = {}
            if browser is None:
                return False
            if self.user_data_dir is not None:
                _close_browser_gracefully(browser)
            try:
                if self.ws_address:
                    browser._run(browser._impl._release_browser())
                else:
                    browser.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                config.logger.warning(
                    "Could not stop the browser: %s", errors._format_error(exc)
                )
            return True

    def _start_recording(self, recorder: recording._VideoRecorder) -> None:
        """Install *recorder* on the current tab."""
        with self._lock:
            if self._recorder is not None:
                raise errors._ToolError(
                    "a video recording is already in progress; stop it before starting another"
                )
            self._run(lambda tab: recording._start_screencast(tab, recorder))
            self._recorder = recorder
            self.last_recording = None

    def _stop_recording(self) -> dict[str, Any]:
        """Stop the active recording and return its encoding result."""
        with self._lock:
            recorder, self._recorder = self._recorder, None
            if recorder is None:
                raise errors._ToolError("no video recording is in progress")
            if recorder.tab is not None:
                recording._stop_screencast(recorder.tab, recorder)
        return recording._finalize_recording(recorder)

    def record_start(
        self,
        path: str | None = None,
        fps: float | str = config._DEFAULT_RECORDING_FPS,
        quality: int | str = config._DEFAULT_SCREENSHOT_QUALITY,
        max_width: int | str | None = None,
        max_height: int | str | None = None,
        show_cursor: bool = False,
    ) -> dict:
        """Start recording the current tab; see ``browser_record_start``."""
        return recording._record_start(
            self, path, fps, quality, max_width, max_height, show_cursor
        )

    def record_stop(self) -> dict:
        """Stop recording and encode the video; see ``browser_record_stop``."""
        return recording._record_stop(self)

    def take_last_recording(self) -> dict[str, Any] | None:
        """Return and clear the most recent recording result."""
        result, self.last_recording = self.last_recording, None
        return result

    def browser(
        self,
        action: config.Action,
        target: config.TextArg | None = None,
        value: config.TextArg | None = None,
        timeout: float = config._DEFAULT_TIMEOUT_S,
    ) -> dict:
        """Run one browser action; see the module-level ``browser`` tool."""
        return actions._browser_action(self, action, target, value, timeout)

    def open(
        self,
        url: config.TextArg,
        max_nodes: int | str = config._DEFAULT_SNAPSHOT_NODES,
        format: config.SnapshotFormat = config._DEFAULT_SNAPSHOT_FORMAT,
        timeout: float = config._DEFAULT_TIMEOUT_S,
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
        max_nodes: int | str = config._DEFAULT_SNAPSHOT_NODES,
        format: config.SnapshotFormat = config._DEFAULT_SNAPSHOT_FORMAT,
    ) -> dict:
        """Return the current page as structured nodes; see ``browser_snapshot``."""
        return snapshot._snapshot(self, max_nodes, format)

    def screenshot(
        self,
        path: str | None = None,
        full_page: bool = False,
        quality: int | str = config._DEFAULT_SCREENSHOT_QUALITY,
        as_base64: bool = False,
    ) -> dict:
        """Capture the current page; see ``browser_screenshot``."""
        return screenshot._screenshot(self, path, full_page, quality, as_base64)

    def evaluate(self, script: config.TextArg, await_promise: bool = True) -> dict:
        """Run JavaScript in the page; see ``browser_evaluate``."""
        return snapshot._evaluate(self, script, await_promise)

    def tabs(
        self,
        action: str = "list",
        value: config.TextArg | None = None,
        timeout: float = config._DEFAULT_TIMEOUT_S,
    ) -> dict:
        """List, open, switch, or close tabs; see ``browser_tabs``."""
        return tabs._tabs_action(self, action, value, timeout)

    def cookies(self, action: str = "get", value: config.TextArg | None = None) -> dict:
        """Read, write, clear, save, or load cookies; see ``browser_cookies``."""
        return cookies._cookies_action(self, action, value)

    def network(
        self,
        action: str = "requests",
        value: config.TextArg | None = None,
        limit: int | str = 20,
    ) -> dict:
        """Inspect recorded requests; see ``browser_network``."""
        return network._network_action(self, action, value, limit)

    def wait_response(self, url: str, timeout: float = 15) -> dict:
        """Wait for a retained or upcoming matching response or failure."""
        try:
            if not url:
                raise ValueError("url substring or request id is required")
            budget = max(0.0, min(float(timeout), config._MAX_TIMEOUT_S))
            self._run(lambda tab: None)
            entry = self._watch.wait_response(url, budget)
            return {"status": "ok", "action": "wait_response", "response": entry}
        except Exception as exc:  # noqa: BLE001 - returned as tool status
            return errors._error("wait_response", exc)

    def cdp(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a raw CDP command to the active page (advanced, trusted callers)."""
        if not method or "." not in method:
            raise ValueError("method must be a CDP Domain.method")
        with self._lock:
            return self._get_tab().execute_command(
                {"method": method, "params": params or {}}
            )

    def pdf(self, path: str, width: float = 8.5, height: float = 11.0) -> dict:
        """Print the active page to PDF with paper dimensions in inches."""
        import base64

        try:
            if width <= 0 or height <= 0:
                raise ValueError("paper dimensions must be positive")
            output = Path(path).expanduser().resolve()
            if output.suffix.lower() != ".pdf":
                raise ValueError("PDF path must end in .pdf")
            response = self.cdp(
                "Page.printToPDF",
                {
                    "paperWidth": width,
                    "paperHeight": height,
                    "marginTop": 0,
                    "marginBottom": 0,
                    "marginLeft": 0,
                    "marginRight": 0,
                    "printBackground": True,
                },
            )
            data = base64.b64decode(response["result"]["data"], validate=True)
            output.write_bytes(data)
            return {
                "status": "ok",
                "action": "pdf",
                "path": str(output),
                "bytes": len(data),
            }
        except Exception as exc:  # noqa: BLE001 - returned as tool status
            return errors._error("pdf", exc)

    def save_tabs(self, path: str) -> dict:
        """Save tab URLs and active tab to a JSON file (not credentials)."""
        import json

        try:
            with self._lock:
                tab_list = self._tab_list()
                entries = [self._tab_entry(i, tab) for i, tab in enumerate(tab_list)]
                urls = [entry.get("url") for entry in entries]
                if not all(isinstance(url, str) for url in urls):
                    raise ValueError("could not read every tab URL")
                active = next(
                    (i for i, entry in enumerate(entries) if entry["active"]), 0
                )
                output = Path(path).expanduser().resolve()
                output.write_text(
                    json.dumps({"version": 1, "urls": urls, "active": active}),
                    encoding="utf-8",
                )
            return {
                "status": "ok",
                "action": "save_tabs",
                "path": str(output),
                "count": len(urls),
            }
        except Exception as exc:  # noqa: BLE001 - returned as tool status
            return errors._error("save_tabs", exc)

    def restore_tabs(self, path: str) -> dict:
        """Open saved tabs in addition to existing tabs after validating all URLs."""
        import json

        try:
            data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
            urls, active = data["urls"], data["active"]
            if (
                data.get("version") != 1
                or not isinstance(urls, list)
                or not urls
                or not all(isinstance(url, str) for url in urls)
                or type(active) is not int
                or not 0 <= active < len(urls)
            ):
                raise ValueError("invalid tab state")
            for url in urls:
                self._check_url(url)
            with self._lock:
                browser = self._browser
                if browser is None:
                    self._get_tab()
                    browser = self._browser
                if browser is None:
                    raise RuntimeError("browser failed to start")
                opened = []
                for url in urls:
                    tab = browser.new_tab()
                    self._install_watch(tab)
                    opened.append(tab)
                    tab.go_to(url, timeout=max(1, int(self.timeout)))
                self._tab = opened[active]
            return {"status": "ok", "action": "restore_tabs", "count": len(opened)}
        except Exception as exc:  # noqa: BLE001 - returned as tool status
            return errors._error("restore_tabs", exc)

    def tools(
        self,
        *,
        include_search: bool = False,
        include_recording: bool = False,
        show_cursor: bool = False,
    ) -> BrowserTools:
        """Return ``dspy.Tool`` objects bound to this session."""
        from . import tools

        return tools._build_tools(
            self,
            include_search=include_search,
            include_recording=include_recording,
            show_cursor=show_cursor,
        )
