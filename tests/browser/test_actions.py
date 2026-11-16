"""Tests for browser actions, actionability, and action verification."""

import time
from types import SimpleNamespace

import pytest
from pydoll.constants import ScrollPosition
from pydoll.exceptions import TimeoutException
from pydoll.sync import Key

from dspy_agent_tools.browser import BrowserSession, actions, dom


class TestNavigate:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("example.com", "https://example.com"),
            ("http://example.com", "http://example.com"),
            ("//example.com", "https://example.com"),
            ("localhost:8000", "http://localhost:8000"),
            ("127.0.0.1:9000", "http://127.0.0.1:9000"),
            ("data:text/html,hi", "data:text/html,hi"),
            ("about:blank", "about:blank"),
        ],
    )
    def test_url_normalization(self, session, value, expected):
        session.browser("navigate", value=value)
        assert session._tab.navigations[-1][0] == expected

    def test_file_url_blocked_by_default(self, session):
        result = session.browser("navigate", value="file:///tmp/a")
        assert result["status"] == "error"
        assert "scheme file" in result["error"]
        assert session._tab is None

    def test_allowed_schemes_can_include_file(self, fake_chrome):
        session = BrowserSession(allowed_schemes=("http", "https", "file"))
        try:
            result = session.browser("navigate", value="file:///tmp/a")
            assert result["status"] == "ok"
            assert session._tab is not None
            assert session._tab.navigations == [("file:///tmp/a", 15)]
        finally:
            session.close()

    def test_url_policy_can_reject_and_accept(self, fake_chrome):
        session = BrowserSession(url_policy=lambda url: "example.com" in url)
        try:
            rejected = session.browser("navigate", value="https://blocked.test")
            assert rejected["status"] == "error"
            assert "URL policy" in rejected["error"]
            accepted = session.browser("navigate", value="https://example.com")
            assert accepted["status"] == "ok"
            assert session._tab is not None
            assert session._tab.navigations == [("https://example.com", 15)]
        finally:
            session.close()

    def test_missing_url(self, session):
        result = session.browser("navigate")
        assert result["status"] == "error"
        assert "value=URL" in result["error"]

    def test_timeout_defaults_when_none(self, session):
        result = session.browser("navigate", value="https://example.com", timeout=None)
        assert result["status"] == "ok"
        assert session._tab.navigations[-1][1] == 15

    def test_timeout_rejects_zero(self, session):
        result = session.browser("navigate", value="https://example.com", timeout=0)
        assert result["status"] == "error"
        assert "greater than zero" in result["error"]

    def test_timeout_rejects_non_number(self, session):
        result = session.browser(
            "navigate", value="https://example.com", timeout="soon"
        )
        assert result["status"] == "error"
        assert "number of seconds" in result["error"]

    def test_page_info_failures_are_omitted(self, session):
        session.browser("navigate", value="https://example.com")
        tab = session._tab
        tab.current_url_error = RuntimeError("no url")
        tab.title_error = RuntimeError("no title")
        result = session.browser("navigate", value="https://example.com")
        assert result["status"] == "ok"
        assert "url" not in result
        assert "title" not in result


class TestActions:
    def test_click_by_ref(self, session):
        result = session.browser("click", target="@e3")
        assert result["status"] == "ok"
        assert result["method"] == "native"
        assert session._tab.queries[0][0] == '[data-dspy-agent-ref="e3"]'
        assert session._tab.element.click_calls == 1
        assert session._tab.network_idle_calls == [(0.2, 2.0)]

    def test_click_falls_back_to_js(self, session):
        session._get_tab()
        element = session._tab.element
        element.click_error = RuntimeError("blocked")
        result = session.browser("click", target="#go")
        assert result["method"] == "js"
        assert element.js_click_calls == 1

    def test_click_reports_native_error_when_both_fail(self, session):
        session._get_tab()
        element = session._tab.element
        element.click_error = RuntimeError("blocked")
        element.js_click_error = RuntimeError("also blocked")
        result = session.browser("click", target="#go")
        assert result["status"] == "error"
        assert "blocked" in result["error"]

    def test_click_requires_target(self, session):
        result = session.browser("click")
        assert result["status"] == "error"
        assert "requires target" in result["error"]

    @pytest.mark.parametrize(
        ("target", "selector"),
        [
            ("ref=e3", '[data-dspy-agent-ref="e3"]'),
            ("css=#main", "#main"),
            ("xpath=//button", "//button"),
            ("text=Sign in", "(//*[normalize-space(.) = 'Sign in'"),
            ("text*=Sign", "(//*[contains(normalize-space(.), 'Sign')"),
            (".card > button", ".card > button"),
        ],
    )
    def test_target_syntax(self, session, target, selector):
        session.browser("click", target=target)
        assert session._tab.queries[0][0].startswith(selector)

    @pytest.mark.parametrize(
        "target",
        ["@bad ref", "ref=", "ref=bad ref", "css=", "xpath=", "text=", "text*="],
    )
    def test_invalid_targets(self, session, target):
        result = session.browser("click", target=target)
        assert result["status"] == "error"

    def test_element_not_found(self, session):
        session._get_tab()
        session._tab.query_result = None
        result = session.browser("click", target="#missing")
        assert result["status"] == "error"
        assert "No element matched" in result["error"]

    def test_type_clears_then_types(self, session):
        result = session.browser("type", target="#email", value="hi@example.com")
        assert result["status"] == "ok"
        assert result["value_length"] == len("hi@example.com")
        assert result["method"] == "keys"
        assert session._tab.element.clear_calls == 1
        assert session._tab.element.typed == ["hi@example.com"]

    def test_type_falls_back_to_js_insertion(self, session):
        session._get_tab()
        element = session._tab.element
        element.type_error = RuntimeError("covered")
        result = session.browser("type", target="#email", value="hi")
        assert result["status"] == "ok"
        assert result["method"] == "js"
        assert element.focus_calls == 1
        assert element.inserted == ["hi"]

    def test_type_reports_original_error_when_fallback_fails(self, session):
        session._get_tab()
        element = session._tab.element
        element.type_error = RuntimeError("covered")
        element.insert_error = RuntimeError("not editable")
        result = session.browser("type", target="#email", value="hi")
        assert result["status"] == "error"
        assert "covered" in result["error"]

    def test_type_requires_value(self, session):
        result = session.browser("type", target="#email")
        assert result["status"] == "error"
        assert "requires value" in result["error"]

    def test_press_single_key(self, session):
        result = session.browser("press", value="Enter")
        assert result["status"] == "ok"
        assert session._tab.keyboard.presses == [Key.ENTER]

    def test_press_hotkey(self, session):
        session.browser("press", value="Control+A")
        assert session._tab.keyboard.hotkeys == [(Key.CONTROL, Key.A)]

    def test_press_three_key_hotkey(self, session):
        session.browser("press", value="Control+Shift+A")
        assert session._tab.keyboard.hotkeys == [(Key.CONTROL, Key.SHIFT, Key.A)]

    @pytest.mark.parametrize(
        ("value", "key"),
        [("Cmd", Key.META), ("Esc", Key.ESCAPE), ("a", Key.A), (" ", Key.SPACE)],
    )
    def test_press_aliases(self, session, value, key):
        session.browser("press", value=value)
        assert session._tab.keyboard.presses == [key]

    def test_press_cmd_alias_in_hotkey(self, session):
        session.browser("press", value="Cmd+C")
        assert session._tab.keyboard.hotkeys == [(Key.META, Key.C)]

    def test_press_unknown_key(self, session):
        result = session.browser("press", value="F13")
        assert result["status"] == "error"
        assert "Unknown key" in result["error"]

    def test_press_requires_value(self, session):
        result = session.browser("press")
        assert result["status"] == "error"
        assert "requires value" in result["error"]

    def test_press_rejects_too_many_keys(self, session):
        result = session.browser("press", value="A+B+C+D")
        assert result["status"] == "error"
        assert "up to three" in result["error"]

    def test_hover(self, session):
        result = session.browser("hover", target="#menu")
        assert result["status"] == "ok"
        assert session._tab.element.hover_calls == 1

    def test_select(self, session):
        result = session.browser("select", target="#country", value="ca")
        assert result["status"] == "ok"
        assert result["selected"] == "selected"
        script, kwargs = session._tab.element.scripts[0]
        assert "dispatchEvent" in script
        assert kwargs["arguments"] == [{"value": "ca"}]

    def test_select_requires_value(self, session):
        result = session.browser("select", target="#country")
        assert result["status"] == "error"
        assert "requires value" in result["error"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("down", ("by", ScrollPosition.DOWN, 600, False)),
            ("up", ("by", ScrollPosition.UP, 600, False)),
            ("top", ("top", False)),
            ("bottom", ("bottom", False)),
            ("250", ("by", ScrollPosition.DOWN, 250, False)),
            ("-250", ("by", ScrollPosition.UP, 250, False)),
        ],
    )
    def test_scroll(self, session, value, expected):
        result = session.browser("scroll", value=value)
        assert result["status"] == "ok"
        assert session._tab.scroll.calls == [expected]

    def test_scroll_zero(self, session):
        result = session.browser("scroll", value="0")
        assert result["status"] == "error"
        assert "cannot be zero" in result["error"]

    def test_scroll_invalid(self, session):
        result = session.browser("scroll", value="sideways")
        assert result["status"] == "error"
        assert "scroll value" in result["error"]

    @pytest.mark.parametrize("action", ["back", "forward"])
    def test_history(self, session, action):
        result = session.browser(action)
        assert result["status"] == "ok"
        assert session._tab.scripts[-1][0] == f"history.{action}()"
        assert len(session._tab.url_wait_calls) == 1
        idle_time, wait_timeout = session._tab.network_idle_calls[0]
        assert idle_time == 0.2
        assert 0.1 <= wait_timeout <= 2.0

    def test_reload(self, session):
        result = session.browser("reload")
        assert result["status"] == "ok"
        assert session._tab.refresh_calls == 1
        assert session._tab.network_idle_calls == [(0.2, 2.0)]

    def test_settle_errors_are_best_effort(self, session):
        session._get_tab()
        session._tab.wait_error = RuntimeError("timed out")
        assert (
            actions._settle(session._tab, 15, previous_url="https://example.com/old")
            is False
        )

    def test_wait(self, session, monkeypatch):
        slept = []
        monkeypatch.setattr(time, "sleep", slept.append)
        result = session.browser("wait", value="2.5")
        assert result["status"] == "ok"
        assert result["seconds"] == 2.5
        assert slept == [2.5]

    def test_wait_defaults(self, session, monkeypatch):
        slept = []
        monkeypatch.setattr(time, "sleep", slept.append)
        result = session.browser("wait")
        assert result["seconds"] == 1.0
        assert slept == [1.0]

    def test_wait_rejects_negative(self, session):
        result = session.browser("wait", value="-1")
        assert result["status"] == "error"
        assert "cannot be negative" in result["error"]

    def test_wait_rejects_unknown_condition(self, session):
        result = session.browser("wait", value="soon")
        assert result["status"] == "error"
        assert "wait value must be" in result["error"]

    def test_numeric_value_is_text(self, session):
        result = session.browser("scroll", value=300)
        assert result["status"] == "ok"
        assert session._tab.scroll.calls == [("by", ScrollPosition.DOWN, 300, False)]

    def test_numeric_target_is_text(self, session):
        result = session.browser("click", target=2)
        assert result["status"] == "ok"
        assert session._tab.queries[0][0] == "2"

    def test_unknown_action(self, session):
        result = session.browser("teleport")
        assert result["status"] == "error"
        assert "Unknown action" in result["error"]


class TestActionability:
    def test_click_refuses_hidden(self, session):
        session._get_tab()
        session._tab.element.visible = False
        result = session.browser("click", target="#go")
        assert result["status"] == "error"
        assert "not visible" in result["error"]
        assert session._tab.element.click_calls == 0

    def test_click_refuses_disabled(self, session):
        session._get_tab()
        session._tab.element.enabled = False
        result = session.browser("click", target="#go")
        assert result["status"] == "error"
        assert "disabled" in result["error"]
        assert session._tab.element.click_calls == 0

    def test_click_refuses_covered(self, session):
        session._get_tab()
        session._tab.element.on_top = False
        result = session.browser("click", target="#go")
        assert result["status"] == "error"
        assert "covered" in result["error"]
        assert session._tab.element.scroll_calls == 1
        assert session._tab.element.click_calls == 0

    def test_click_probe_errors_do_not_block(self, session):
        session._get_tab()

        def broken():
            raise RuntimeError("no probe")

        session._tab.element.is_visible = broken
        session._tab.element.is_on_top = None
        result = session.browser("click", target="#go")
        assert result["status"] == "ok"
        assert session._tab.element.click_calls == 1

    def test_scroll_into_view_error_is_ignored(self, session):
        session._get_tab()

        def broken():
            raise RuntimeError("no scroll")

        session._tab.element.scroll_into_view = broken
        assert session.browser("click", target="#go")["status"] == "ok"

    def test_hover_skips_enabled_check(self, session):
        session._get_tab()
        session._tab.element.enabled = False
        result = session.browser("hover", target="#menu")
        assert result["status"] == "ok"
        assert session._tab.element.hover_calls == 1

    def test_type_refuses_covered_field(self, session):
        session._get_tab()
        session._tab.element.on_top = False
        result = session.browser("type", target="#email", value="hi")
        assert result["status"] == "error"
        assert "covered" in result["error"]
        assert session._tab.element.typed == []

    def test_type_refuses_non_editable(self, session):
        session._get_tab()
        session._tab.element.editable = False
        result = session.browser("type", target="#note", value="hi")
        assert result["status"] == "error"
        assert "does not accept text" in result["error"]

    def test_probe_bool_accepts_properties(self):
        element = SimpleNamespace(is_enabled=True)
        assert dom._probe_bool(element, "is_enabled") is True
        assert dom._probe_bool(element, "is_visible") is True


class TestEffect:
    def test_click_reports_dom_change(self, session):
        session._get_tab()
        url = "https://example.com/page"
        session._tab.probe_responses = [
            {"url": url, "mutations": 0},
            {"url": url, "mutations": 4},
        ]
        result = session.browser("click", target="#go")
        assert result["changed"] is True
        assert result["effect"] == "dom"
        assert result["mutations"] == 4
        assert "warning" not in result

    def test_click_reports_navigation(self, session):
        session._get_tab()
        session._tab.probe_responses = [
            {"url": "https://example.com/a", "mutations": 0},
            {"url": "https://example.com/b", "mutations": 0},
        ]
        result = session.browser("click", target="#go")
        assert result["effect"] == "navigation"
        assert result["changed"] is True

    def test_click_warns_when_nothing_changed(self, session):
        result = session.browser("click", target="#go")
        assert result["changed"] is False
        assert result["effect"] == "none"
        assert "did not change" in result["warning"]

    def test_js_fallback_warning_is_reported(self, session):
        session._get_tab()
        session._tab.element.click_error = RuntimeError("blocked")
        result = session.browser("click", target="#go")
        assert result["method"] == "js"
        assert "JavaScript click" in result["warning"]

    def test_probe_failure_omits_effect(self, session):
        session._get_tab()
        session._tab.probe_error = RuntimeError("no probe")
        result = session.browser("click", target="#go")
        assert result["status"] == "ok"
        assert "changed" not in result

    def test_press_reports_effect(self, session):
        session._get_tab()
        url = "https://example.com/page"
        session._tab.probe_responses = [
            {"url": url, "mutations": 0},
            {"url": url, "mutations": 2},
        ]
        result = session.browser("press", value="Enter")
        assert result["effect"] == "dom"
        assert "warning" not in result

    def test_press_warns_when_nothing_changed(self, session):
        result = session.browser("press", value="Enter")
        assert result["changed"] is False
        assert "did not change" in result["warning"]


class TestTypeVerification:
    def test_verified(self, session):
        result = session.browser("type", target="#email", value="hi@example.com")
        assert result["verified"] is True

    def test_mismatch_reports_actual(self, session):
        session._get_tab()
        element = session._tab.element
        element.mirror_typing = False
        element.field_state["value"] = "hi@example.co"
        result = session.browser("type", target="#email", value="hi@example.com")
        assert result["verified"] is False
        assert result["actual"] == "hi@example.co"
        assert result["actual_length"] == len("hi@example.co")
        assert "differs after typing" in result["warning"]

    def test_password_mismatch_is_redacted(self, session):
        session._get_tab()
        element = session._tab.element
        element.mirror_typing = False
        element.field_state["type"] = "password"
        element.field_state["value"] = "masked"
        result = session.browser("type", target="#pw", value="secret")
        assert result["verified"] is False
        assert "actual" not in result

    def test_contenteditable_rejects_partial_text_match(self, session):
        session._get_tab()
        element = session._tab.element
        element.mirror_typing = False
        element.field_state["tag"] = "div"
        element.field_state["value"] = None
        element.field_state["text"] = "draft hello world"
        result = session.browser("type", target="#note", value="hello")
        assert result["verified"] is False
        assert result["actual"] == "draft hello world"

    def test_unreadable_field_skips_verification(self, session):
        session._get_tab()
        session._tab.element.field_state_error = RuntimeError("no state")
        result = session.browser("type", target="#email", value="hi")
        assert result["status"] == "ok"
        assert "verified" not in result


class TestSelectVerification:
    def test_verified(self, session):
        result = session.browser("select", target="#size", value="selected")
        assert result["verified"] is True

    def test_mismatch_warns(self, session):
        result = session.browser("select", target="#size", value="large")
        assert result["verified"] is False
        assert "after selecting" in result["warning"]

    def test_missing_return_value_is_not_verified(self, session):
        session._get_tab()
        session._tab.element.execute_script = lambda *_a, **_k: {
            "result": {"result": {"type": "undefined"}}
        }
        result = session.browser("select", target="#size", value="large")
        assert result["status"] == "ok"
        assert "verified" not in result


class TestWaitConditions:
    def test_wait_for_target_selector(self, session):
        result = session.browser("wait", target="#done")
        assert result["status"] == "ok"
        assert result["waited_for"] == "#done"
        script, timeout = session._tab.wait_script_calls[0]
        assert "document.querySelector" in script
        assert '"#done"' in script
        assert timeout == 15.0

    def test_wait_for_text(self, session):
        result = session.browser("wait", value="text=Done")
        assert result["waited_for"] == "text 'Done'"
        assert '"Done"' in session._tab.wait_script_calls[0][0]

    def test_wait_for_partial_text(self, session):
        result = session.browser("wait", value="text*=Don")
        assert result["waited_for"] == "text 'Don'"

    def test_wait_for_url_prefix(self, session):
        result = session.browser("wait", value="url=https://example.com/next")
        assert result["waited_for"].startswith("url starting with")
        assert "location.href.indexOf" in session._tab.wait_script_calls[0][0]

    def test_wait_for_bare_url(self, session):
        result = session.browser("wait", value="https://example.com/next*")
        assert result["status"] == "ok"
        assert '"https://example.com/next"' in session._tab.wait_script_calls[0][0]

    def test_wait_for_selector_prefix(self, session):
        result = session.browser("wait", value="selector=#done")
        assert result["waited_for"] == "#done"

    def test_wait_clamps_timeout(self, session):
        session.browser("wait", target="#done", timeout=120)
        assert session._tab.wait_script_calls[0][1] == 30.0

    def test_wait_timeout_is_reported(self, session):
        session._get_tab()
        session._tab.wait_script_error = TimeoutException("too slow")
        result = session.browser("wait", value="text=Done", timeout=5)
        assert result["status"] == "error"
        assert "timed out after 5s waiting for text 'Done'" in result["error"]

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("selector=", "selector= requires"),
            ("text=", "text= requires"),
            ("text*=", "text*= requires"),
            ("url=", "url= requires"),
        ],
    )
    def test_wait_rejects_empty_condition(self, session, value, message):
        result = session.browser("wait", value=value)
        assert result["status"] == "error"
        assert message in result["error"]


class TestUpload:
    def test_upload_requires_a_file_path(self, session):
        result = session.browser("upload", target="#file")
        assert result["status"] == "error"
        assert "upload requires value=local file path" in result["error"]

    def test_upload_rejects_a_missing_file(self, session, tmp_path):
        result = session.browser(
            "upload", target="#file", value=str(tmp_path / "missing.txt")
        )
        assert result["status"] == "error"
        assert "upload path must be an existing file" in result["error"]

    def test_upload_sets_the_resolved_file_input(self, session, tmp_path):
        file = tmp_path / "upload.txt"
        file.write_text("hello")
        result = session.browser("upload", target="#file", value=str(file))
        assert result["status"] == "ok"
        assert result["action"] == "upload"
        assert result["filename"] == "upload.txt"
        assert session._tab.element.uploads == [str(file.resolve())]


class TestStaleRefs:
    def test_hash_ref_from_a_previous_snapshot_is_rejected(self, session):
        stale = "@e" + "0" * 32 + "_1"
        result = session.browser("click", target=stale)
        assert result["status"] == "error"
        assert "stale element ref" in result["error"]
        assert session._tab.element.click_calls == 0
