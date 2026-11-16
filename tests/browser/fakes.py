"""Shared fakes and event helpers for the browser tests."""

import base64
import importlib
from pathlib import Path
from typing import ClassVar

from pydoll.protocol.network.events import NetworkEvent
from pydoll.protocol.runtime.events import RuntimeEvent
from pydoll.sync import PageEvent

browser_module = importlib.import_module("dspy_agent_tools.browser")


class FakeElement:
    def __init__(self):
        self.click_calls = 0
        self.js_click_calls = 0
        self.clear_calls = 0
        self.typed = []
        self.inserted = []
        self.focus_calls = 0
        self.hover_calls = 0
        self.scripts = []
        self.click_error = None
        self.js_click_error = None
        self.type_error = None
        self.insert_error = None
        self.visible = True
        self.enabled = True
        self.on_top = True
        self.editable = True
        self.scroll_calls = 0
        self.uploads = []
        self.script_error = None
        self.field_state = {
            "tag": "input",
            "value": None,
            "text": None,
            "type": "text",
            "disabled": False,
            "readonly": False,
        }
        self.field_state_error = None
        self.mirror_typing = True

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def is_on_top(self):
        return self.on_top

    def is_editable(self):
        return self.editable

    def scroll_into_view(self):
        self.scroll_calls += 1

    def click(self):
        self.click_calls += 1
        if self.click_error:
            raise self.click_error

    def click_using_js(self):
        self.js_click_calls += 1
        if self.js_click_error:
            raise self.js_click_error

    def clear(self):
        self.clear_calls += 1

    def type_text(self, value):
        if self.type_error:
            raise self.type_error
        self.typed.append(value)
        if self.mirror_typing:
            self.field_state["value"] = value

    def focus(self):
        self.focus_calls += 1

    def insert_text(self, value):
        if self.insert_error:
            raise self.insert_error
        self.inserted.append(value)
        self.field_state["value"] = value

    def hover(self):
        self.hover_calls += 1

    def set_input_files(self, path):
        self.uploads.append(path)

    def execute_script(self, script, **kwargs):
        self.scripts.append((script, kwargs))
        if self.script_error:
            raise self.script_error
        if "readOnly" in script:
            if self.field_state_error:
                raise self.field_state_error
            return {"result": {"result": {"value": self.field_state}}}
        return {"result": {"result": {"type": "string", "value": "selected"}}}


class FakeKeyboard:
    def __init__(self):
        self.presses = []
        self.hotkeys = []

    def press(self, key):
        self.presses.append(key)

    def hotkey(self, *keys):
        self.hotkeys.append(keys)


class FakeScroll:
    def __init__(self):
        self.calls = []

    def by(self, position, distance, smooth=True):
        self.calls.append(("by", position, distance, smooth))

    def to_top(self, smooth=True):
        self.calls.append(("top", smooth))

    def to_bottom(self, smooth=True):
        self.calls.append(("bottom", smooth))


class FakeTab:
    def __init__(self, element=None):
        self.element = element or FakeElement()
        self.target_id = "target-1"
        self.closed = False
        self.cookies = [
            {"name": "sid", "value": "abc", "domain": "example.com", "path": "/"}
        ]
        self.set_cookie_calls = []
        self.set_cookie_error = None
        self.network_body = '{"ok": true}'
        self.url = "https://example.com/page"
        self.page_title = "Example"
        self.navigations = []
        self.queries = []
        self.scripts = []
        self.keyboard = FakeKeyboard()
        self.scroll = FakeScroll()
        self.refresh_calls = 0
        self.screenshot_calls = []
        self.query_error = None
        self.query_result = self.element
        self.script_response = None
        self.script_error = None
        self.current_url_error = None
        self.title_error = None
        self.screenshot_error = None
        self.network_idle_calls = []
        self.url_wait_calls = []
        self.wait_error = None
        self.page_events = 0
        self.runtime_events = 0
        self.network_events = 0
        self.events_error = None
        self.callbacks = {}
        self.removed_callbacks = []
        self.commands = []
        self.command_error = None
        self.command_response = {"result": {}}
        self.fingerprints = []
        self.remove_callback_error = None
        self._next_callback_id = 0
        self.probe_responses = []
        self.probe_error = None
        self.wait_script_calls = []
        self.wait_script_error = None
        self.dialogs = []

    def go_to(self, url, timeout=300):
        self.navigations.append((url, timeout))
        self.url = url

    def current_url(self):
        if self.current_url_error:
            raise self.current_url_error
        return self.url

    def title(self):
        if self.title_error:
            raise self.title_error
        return self.page_title

    def wait_for_network_idle(self, idle_time=0.5, timeout=30):
        self.network_idle_calls.append((idle_time, timeout))
        if self.wait_error:
            raise self.wait_error

    def wait_for_url(self, url, timeout=30):
        self.url_wait_calls.append((url, timeout))
        if self.wait_error:
            raise self.wait_error
        return self.url

    def query(self, expression, timeout=0, find_all=False, raise_exc=True):
        self.queries.append((expression, timeout, find_all, raise_exc))
        if self.query_error:
            raise self.query_error
        return self.query_result

    def execute_script(self, script, **kwargs):
        self.scripts.append((script, kwargs))
        if self.script_error:
            raise self.script_error
        if "__dspyAgentProbe" in script:
            if self.probe_error:
                raise self.probe_error
            if self.probe_responses:
                value = self.probe_responses.pop(0)
            else:
                value = {"url": self.url, "mutations": 0}
            return {"result": {"result": {"value": value}}}
        if self.script_response is not None:
            return self.script_response
        return {"result": {"result": {"type": "undefined"}}}

    def refresh(self):
        self.refresh_calls += 1

    def take_screenshot(
        self, path=None, quality=100, beyond_viewport=False, as_base64=False
    ):
        self.screenshot_calls.append((path, quality, beyond_viewport, as_base64))
        if self.screenshot_error:
            raise self.screenshot_error
        if as_base64:
            return "ZmFrZQ=="
        assert path is not None
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-image")

    def enable_page_events(self):
        self.page_events += 1
        if self.events_error:
            raise self.events_error

    def enable_runtime_events(self):
        self.runtime_events += 1
        if self.events_error:
            raise self.events_error

    def enable_network_events(self):
        self.network_events += 1
        if self.events_error:
            raise self.events_error

    def close(self):
        self.closed = True

    def get_cookies(self):
        return list(self.cookies)

    def set_cookies(self, cookies):
        if self.set_cookie_error:
            raise self.set_cookie_error
        self.set_cookie_calls.append([dict(cookie) for cookie in cookies])

    def delete_all_cookies(self):
        self.cookies = []

    def get_network_response_body(self, request_id):
        self.requested_body = request_id
        return self.network_body

    def wait_for_script(self, script, timeout=30):
        self.wait_script_calls.append((script, timeout))
        if self.wait_script_error:
            raise self.wait_script_error
        return True

    def handle_dialog(self, accept, prompt_text=None):
        self.dialogs.append((accept, prompt_text))

    def apply_fingerprint(self, fingerprint):
        self.fingerprints.append(dict(fingerprint))

    def on(self, event_name, callback, temporary=False):
        self._next_callback_id += 1
        self.callbacks[self._next_callback_id] = (event_name, callback, temporary)
        return self._next_callback_id

    def remove_callback(self, callback_id):
        self.removed_callbacks.append(callback_id)
        if self.remove_callback_error:
            raise self.remove_callback_error
        self.callbacks.pop(callback_id, None)

    def execute_command(self, command, timeout=60):
        self.commands.append(command)
        if self.command_error:
            raise self.command_error
        return self.command_response


class FakeBrowserImpl:
    def __init__(self):
        self.releases = 0

    def _release_browser(self):
        self.releases += 1
        return "release-browser"


class FakeChrome:
    instances: ClassVar[list["FakeChrome"]] = []
    start_error = None
    command_error = None

    def __init__(self, options=None, connection_port=None):
        self.options = options
        self.started = False
        self.stopped = False
        self.tab = FakeTab()
        self.tabs = [self.tab]
        self.opened_urls = []
        self.connections = []
        self.runs = []
        self._impl = FakeBrowserImpl()
        FakeChrome.instances.append(self)

    def start(self):
        if FakeChrome.start_error:
            raise FakeChrome.start_error
        self.started = True
        return self.tab

    def connect(self, ws_address):
        self.connections.append(ws_address)
        self.started = True
        return self.tab

    def _run(self, command):
        self.runs.append(command)
        return command

    def new_tab(self, url="", browser_context_id=None):
        tab = FakeTab()
        tab.target_id = f"target-{len(self.tabs) + 1}"
        self.tabs.append(tab)
        self.opened_urls.append(url)
        if url:
            tab.go_to(url)
        return tab

    def get_opened_tabs(self):
        return [tab for tab in self.tabs if not tab.closed]

    def execute_command(self, command, timeout=60):
        self.commands = getattr(self, "commands", [])
        self.commands.append(command)
        if FakeChrome.command_error:
            raise FakeChrome.command_error

    def stop(self):
        self.stopped = True


class FakeFrameDocument:
    """Page or iframe document for shadow-root and frame traversal.

    ``frames`` is returned for ``find_all`` queries and may be any value so a
    test can pose a parent whose iframe query is not a list. ``body`` answers
    single-element queries, which is how an iframe yields its document.
    """

    def __init__(
        self,
        nodes=(),
        *,
        frames=None,
        body=None,
        shadow_roots=(),
        url="",
        query_error=None,
        script_error=None,
        truncated=False,
    ):
        self.nodes = [dict(node) for node in nodes]
        self.frames = frames
        self.body = body
        self.shadow_roots = list(shadow_roots)
        self.url = url
        self.query_error = query_error
        self.script_error = script_error
        self.truncated = truncated
        self.queries = []
        self.scripts = []

    def find_shadow_roots(self, deep=True):
        return list(self.shadow_roots)

    def query(self, expression, timeout=0, find_all=False, raise_exc=True):
        self.queries.append((expression, find_all))
        if self.query_error is not None:
            raise self.query_error
        return self.frames if find_all else self.body

    def execute_script(self, script, **kwargs):
        self.scripts.append((script, kwargs))
        if self.script_error is not None:
            raise self.script_error
        value = {"url": self.url, "nodes": [dict(node) for node in self.nodes]}
        if self.truncated:
            value["truncated"] = True
        return {"result": {"result": {"value": value}}}


class FakeShadowRoot:
    """Shadow root stand-in whose queries answer from one fixed element."""

    def __init__(self, element=None):
        self.element = element
        self.queries = []

    def query(self, expression, timeout=0, find_all=False, raise_exc=True):
        self.queries.append(expression)
        return self.element


def frame_event(data=b"frame", timestamp=1.0, session_id=1):
    params = {"data": base64.b64encode(data).decode(), "sessionId": session_id}
    if timestamp is not None:
        params["metadata"] = {"timestamp": timestamp}
    return {"params": params}


def screencast_callback(tab):
    for event, callback, _temporary in tab.callbacks.values():
        if event == PageEvent.SCREENCAST_FRAME:
            return callback
    raise AssertionError("no screencast callback registered")


def screencast_callback_id(tab):
    for callback_id, (event, _callback, _temporary) in tab.callbacks.items():
        if event == PageEvent.SCREENCAST_FRAME:
            return callback_id
    raise AssertionError("no screencast callback registered")


def snapshot_response(nodes, *, truncated=False):
    return {
        "result": {
            "result": {
                "value": {
                    "url": "https://example.com/page",
                    "title": "Example",
                    "nodes": nodes,
                    "truncated": truncated,
                }
            }
        }
    }


def watch_callback(tab, event):
    for name, callback, _temporary in tab.callbacks.values():
        if name == event:
            return callback
    raise AssertionError(f"no {event} callback registered")


def fire_console(tab, *args, level="log"):
    watch_callback(tab, RuntimeEvent.CONSOLE_API_CALLED)(
        {"params": {"type": level, "args": list(args)}}
    )


def fire_error(tab, text="Uncaught TypeError: boom"):
    watch_callback(tab, RuntimeEvent.EXCEPTION_THROWN)(
        {"params": {"exceptionDetails": {"exception": {"description": text}}}}
    )


def fire_dialog(tab, message="Are you sure?", kind="confirm", prompt=None):
    params = {"type": kind, "message": message}
    if prompt is not None:
        params["defaultPrompt"] = prompt
    watch_callback(tab, PageEvent.JAVASCRIPT_DIALOG_OPENING)({"params": params})


def fire_request(tab, request_id="1", url="https://example.com/api", method="GET"):
    watch_callback(tab, NetworkEvent.REQUEST_WILL_BE_SENT)(
        {
            "params": {
                "requestId": request_id,
                "type": "Fetch",
                "request": {"method": method, "url": url},
            }
        }
    )


def fire_response(tab, request_id="1", status=200, mime="application/json", **extra):
    response = {"status": status, "mimeType": mime, **extra}
    watch_callback(tab, NetworkEvent.RESPONSE_RECEIVED)(
        {"params": {"requestId": request_id, "response": response}}
    )
