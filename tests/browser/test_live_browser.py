"""Opt-in integration coverage against locally installed Chrome and pydoll."""

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dspy_agent_tools.browser import BrowserSession

pytestmark = pytest.mark.skipif(
    os.environ.get("DSPY_BROWSER_LIVE_TESTS") != "1",
    reason="set DSPY_BROWSER_LIVE_TESTS=1 to run real Chrome integration tests",
)


def test_recording_cursor_navigation_and_cleanup(tmp_path):
    from urllib.parse import quote

    page = "data:text/html," + quote('<button id="go">Click me</button>')
    session = BrowserSession(humanize=True)
    try:
        assert session.browser("navigate", value=page)["status"] == "ok"
        assert (
            session.record_start(path=str(tmp_path / "cursor.mp4"), show_cursor=True)[
                "status"
            ]
            == "ok"
        )
        assert session.browser("click", target="#go")["status"] == "ok"
        cursor = "document.querySelector('[data-dspy-recording-cursor]')"
        assert session.evaluate(f"return !!{cursor}")["result"] is True
        assert session.browser("navigate", value=page)["status"] == "ok"
        assert session.browser("click", target="#go")["status"] == "ok"
        assert session.evaluate(f"return !!{cursor}")["result"] is True
        time.sleep(0.3)
        assert session.record_stop()["status"] == "ok"
        for _ in range(20):
            if not session.evaluate(f"return !!{cursor}")["result"]:
                break
            time.sleep(0.05)
        assert session.evaluate(f"return !!{cursor}")["result"] is False
        assert session.browser("navigate", value=page)["status"] == "ok"
        assert session.browser("click", target="#go")["status"] == "ok"
        assert session.evaluate(f"return !!{cursor}")["result"] is False
    finally:
        session.close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<button onclick=\"this.textContent='Clicked frame'\">Frame button</button>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_password_refs_iframe_and_shadow(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    html = tmp_path / "page.html"
    html.write_text(
        '<input type="password" value="not-for-the-model">'
        '<div id="host"></div>'
        f'<iframe src="http://127.0.0.1:{server.server_port}/"></iframe>'
        '<script>host.attachShadow({mode:"open"}).innerHTML='
        "'<button>Shadow button</button>'</script>"
    )
    session = BrowserSession(allowed_schemes=("http", "https", "file"))
    try:
        assert session.browser("navigate", value=html.as_uri())["status"] == "ok"
        snapshot = session.snapshot()
        assert snapshot["status"] == "ok"
        password = next(
            n for n in snapshot["nodes"] if n.get("input_type") == "password"
        )
        assert "value" not in password
        for name in ("Frame button", "Shadow button"):
            node = next(n for n in snapshot["nodes"] if n["name"] == name)
            assert (
                session.browser("click", target=node["ref"], timeout=2)["status"]
                == "ok"
            )
        old_ref = snapshot["nodes"][-1]["ref"]
        assert session.snapshot()["status"] == "ok"
        assert session.browser("click", target=old_ref, timeout=1)["status"] == "error"
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_advanced_browser_operations(tmp_path):
    from urllib.parse import quote

    file = tmp_path / "upload.txt"
    file.write_text("hello")
    session = BrowserSession()
    try:
        page = "data:text/html," + quote('<input id="file" type="file"><h1>Report</h1>')
        assert session.browser("navigate", value=page)["status"] == "ok"
        assert (
            session.browser("upload", target="#file", value=str(file))["status"] == "ok"
        )
        assert (
            session.evaluate("return document.querySelector('#file').files[0].name")[
                "result"
            ]
            == "upload.txt"
        )
        pdf = tmp_path / "report.pdf"
        assert session.pdf(str(pdf))["status"] == "ok"
        assert pdf.read_bytes().startswith(b"%PDF")
        state = tmp_path / "tabs.json"
        assert session.save_tabs(str(state))["status"] == "ok"
        assert session.restore_tabs(str(state))["status"] == "ok"
        assert session.tabs()["count"] == 2
        assert "result" in session.cdp("Page.getNavigationHistory")
    finally:
        session.close()


def test_response_wait_and_attach_detach(tmp_path):
    import json
    import subprocess
    import time
    import urllib.request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not os.path.exists(chrome_path):
        pytest.skip("Chrome executable path is macOS-specific")
    process = subprocess.Popen(
        [
            chrome_path,
            "--headless=new",
            "--remote-debugging-port=0",
            f"--user-data-dir={tmp_path / 'profile'}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session = None
    try:
        port_file = tmp_path / "profile" / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while not port_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        port, browser_path = port_file.read_text().splitlines()[:2]
        session = BrowserSession(ws_address=f"ws://127.0.0.1:{port}{browser_path}")
        assert (
            session.browser(
                "navigate", value=f"http://127.0.0.1:{server.server_port}/"
            )["status"]
            == "ok"
        )
        response = session.wait_response(f"127.0.0.1:{server.server_port}", 2)
        assert response["status"] == "ok" and response["response"]["status"] == 200
        assert session.close()
        assert process.poll() is None
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=2
        ) as result:
            assert json.load(result)["Browser"]
    finally:
        if session:
            session.close()
        process.terminate()
        process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join()
