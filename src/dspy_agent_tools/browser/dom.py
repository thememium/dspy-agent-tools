"""Element lookup, probing, and interaction primitives."""

from __future__ import annotations

from typing import Any

from . import actions, config, errors


def _xpath_literal(text: str) -> str:
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in parts) + ")"


def _text_xpath(text: str, *, contains: bool) -> str:
    literal = _xpath_literal(text)
    predicate = (
        f"contains(normalize-space(.), {literal})"
        if contains
        else f"normalize-space(.) = {literal}"
    )
    return f"(//*[{predicate} and ({config._TEXT_XPATH})])[1]"


def _selector(target: str | None) -> str:
    if not isinstance(target, str) or not target.strip():
        raise errors._ToolError(
            "This action requires target=... (an @ref, CSS, or XPath selector)"
        )
    target = target.strip()
    if target.startswith("@"):
        ref = target[1:]
        if not config._REF_RE.fullmatch(ref):
            raise errors._ToolError(
                f"Invalid element ref {target!r}; use an @ref from browser_snapshot"
            )
        return f'[{config._REF_ATTR}="{ref}"]'
    if target.startswith("ref="):
        ref = target[4:].strip()
        if not config._REF_RE.fullmatch(ref):
            raise errors._ToolError(
                f"Invalid element ref {target!r}; use an @ref from browser_snapshot"
            )
        return f'[{config._REF_ATTR}="{ref}"]'
    if target.startswith("css="):
        value = target[4:].strip()
        if not value:
            raise errors._ToolError("css= requires a CSS selector")
        return value
    if target.startswith("xpath="):
        value = target[6:].strip()
        if not value:
            raise errors._ToolError("xpath= requires an XPath expression")
        return value
    if target.startswith("text*="):
        value = target[6:].strip()
        if not value:
            raise errors._ToolError("text*= requires text to match")
        return _text_xpath(value, contains=True)
    if target.startswith("text="):
        value = target[5:].strip()
        if not value:
            raise errors._ToolError("text= requires text to match")
        return _text_xpath(value, contains=False)
    return target


def _element(tab: Any, target: str | None, timeout: float) -> Any:
    selector = _selector(target)
    element = tab.query(selector, timeout=max(1, int(timeout)), raise_exc=False)
    if element is None:
        raise errors._ToolError(
            f"No element matched {target!r}. Call browser_snapshot for fresh @refs, "
            "or pass a CSS/XPath selector."
        )
    return element


def _probe_bool(element: Any, name: str) -> bool:
    """Read one boolean element probe; a probe that cannot run is not a failure."""
    probe = getattr(element, name, None)
    if probe is None:
        return True
    try:
        return bool(probe() if callable(probe) else probe)
    except Exception:  # noqa: BLE001 - a probe that cannot run must not block
        return True


def _require_actionable(element: Any, action: str) -> None:
    """Refuse an action a user could not perform, before it silently no-ops."""
    if not _probe_bool(element, "is_visible"):
        raise errors._ToolError(
            "target is not visible; it may be hidden, zero-size, or scrolled away"
        )
    if action == "hover":
        return
    if not _probe_bool(element, "is_enabled"):
        raise errors._ToolError(
            "target is disabled; wait for it to become enabled and retry"
        )
    if action not in ("click", "type"):
        return
    # ``type`` clicks the field to focus it, so both actions need the point.
    try:
        element.scroll_into_view()
    except Exception:  # noqa: BLE001, S110 - pydoll scrolls again inside click
        pass
    if not _probe_bool(element, "is_on_top"):
        raise errors._ToolError(
            "target is covered by another element at its centre point; dismiss "
            "the overlay (cookie banner, modal, sticky header) or target the "
            "covering control"
        )


def _click(element: Any) -> tuple[str, str | None]:
    """Click natively, falling back to a JavaScript click that is reported."""
    try:
        element.click()
        return "native", None
    except Exception as native_error:  # noqa: BLE001 - fall back to a JS click
        try:
            element.click_using_js()
        except Exception:  # noqa: BLE001 - report the original native failure
            raise native_error from None
        return "js", (
            "native click failed; used a JavaScript click, which ignores "
            "overlays and pointer-events"
        )


def _probe(tab: Any) -> dict[str, Any] | None:
    """Read the page's mutation counter and URL, installing the counter once."""
    try:
        data = actions._remote_value(
            tab.execute_script(
                config._PROBE_JS,
                return_by_value=True,
                allow_unsafe_eval_blocked_by_csp=True,
            )
        )
    except Exception:  # noqa: BLE001 - the effect probe is optional
        return None
    return data if isinstance(data, dict) else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _effect(tab: Any, before: dict[str, Any] | None) -> dict[str, Any]:
    """Describe what an action changed, when the page can say."""
    after = _probe(tab)
    if before is None or after is None:
        return {}
    if after.get("url") != before.get("url"):
        return {"changed": True, "effect": "navigation"}
    mutations = _count(after.get("mutations")) - _count(before.get("mutations"))
    if mutations > 0:
        return {"changed": True, "effect": "dom", "mutations": mutations}
    return {"changed": False, "effect": "none", "warning": config._NO_EFFECT_WARNING}


def _field_state(element: Any) -> dict[str, Any] | None:
    """Read a form field's live value so typing can be verified."""
    script = config._FIELD_STATE_JS.replace(
        "__MAX_TEXT__", str(config._MAX_CONSOLE_TEXT)
    )
    try:
        data = actions._remote_value(
            element.execute_script(script, return_by_value=True)
        )
    except Exception:  # noqa: BLE001 - verification is best-effort
        return None
    return data if isinstance(data, dict) else None


def _verify_typed(state: dict[str, Any] | None, expected: str) -> dict[str, Any]:
    """Report whether the field holds the typed text after typing it."""
    if state is None:
        return {}
    actual = state.get("value")
    if actual is None:
        actual_text = str(state.get("text") or "")
        verified = actual_text == expected
    else:
        actual_text = str(actual)
        verified = actual_text == expected
    if verified:
        return {"verified": True}
    payload: dict[str, Any] = {
        "verified": False,
        "actual_length": len(actual_text),
        "warning": (
            "the field value differs after typing; the page may mask, truncate, "
            "or reject the text"
        ),
    }
    if str(state.get("type") or "") != "password":
        payload["actual"] = actual_text[:80]
    return payload
