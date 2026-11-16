"""Opt-in integration coverage against locally installed Chrome and pydoll."""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dspy_agent_tools.browser import BrowserSession

pytestmark = pytest.mark.skipif(
    os.environ.get("DSPY_BROWSER_LIVE_TESTS") != "1",
    reason="set DSPY_BROWSER_LIVE_TESTS=1 to run real Chrome integration tests",
)


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
