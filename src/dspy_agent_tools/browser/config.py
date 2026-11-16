"""Constants, type aliases, and JavaScript snippets for the browser tools."""

from __future__ import annotations

import logging
import re
from typing import Literal

TextArg = str | int | float


Action = Literal[
    "navigate",
    "click",
    "upload",
    "type",
    "press",
    "hover",
    "select",
    "scroll",
    "back",
    "forward",
    "reload",
    "wait",
]


SnapshotFormat = Literal["nodes", "text", "both"]


DialogPolicy = Literal["dismiss", "accept"]


_DEFAULT_TIMEOUT_S = 15.0


_MAX_TIMEOUT_S = 120.0


_MAX_WAIT_S = 30.0


_SETTLE_IDLE_S = 0.2


_SETTLE_TIMEOUT_S = 2.0


_DEFAULT_SNAPSHOT_FORMAT: SnapshotFormat = "nodes"


_DEFAULT_ALLOWED_SCHEMES = frozenset({"http", "https", "about", "data"})


_MAX_SNAPSHOT_NODES = 1000


_DEFAULT_SNAPSHOT_NODES = 400


_MAX_NAME_CHARS = 300


_DEFAULT_SCROLL_PX = 600


_DEFAULT_SCREENSHOT_QUALITY = 80


_DEFAULT_RECORDING_FPS = 10.0


_MAX_RECORDING_FPS = 60.0


_VIDEO_SUFFIXES = (".mp4", ".webm")


_REF_ATTR = "data-dspy-agent-ref"


_DEFAULT_WINDOW_SIZE = (1440, 900)


_DEFAULT_DIALOG_POLICY: DialogPolicy = "dismiss"


_MAX_DIALOG_CHARS = 200


_MAX_CONSOLE_ENTRIES = 200


_MAX_CONSOLE_ITEMS = 8


_MAX_CONSOLE_TEXT = 300


_PROFILE_FLUSH_S = 0.5


_MAX_NETWORK_ENTRIES = 300


_MAX_NETWORK_ITEMS = 100


_MAX_NETWORK_BODY = 4000


_MAX_COOKIE_ITEMS = 200


_COOKIE_FIELDS = (
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
)


_NO_EFFECT_WARNING = (
    "the page did not change; the target may be inert, or its effect is not "
    "visible in the DOM"
)


_ACTIONS = (
    "navigate",
    "click",
    "upload",
    "type",
    "press",
    "hover",
    "select",
    "scroll",
    "back",
    "forward",
    "reload",
    "wait",
)


_TAB_ACTIONS = frozenset({"click", "press", "navigate"})


_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


_REF_RE = re.compile(r"^[A-Za-z0-9_-]+$")


_INT_RE = re.compile(r"^[+-]?\d+$")


_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)$")


_KEY_ALIASES = {
    "CTRL": "CONTROL",
    "CMD": "META",
    "COMMAND": "META",
    "ESC": "ESCAPE",
    "RETURN": "ENTER",
    "DEL": "DELETE",
}


_TEXT_XPATH = (
    "self::a or self::button or self::input or self::textarea or self::select "
    "or self::summary or @role='button' or @role='link' or @role='tab' "
    "or @role='menuitem'"
)


logger = logging.getLogger(__name__)


_SNAPSHOT_JS = r"""
return (
  function () {
  const MAX_NODES = __MAX_NODES__;
  const GENERATION = "__GENERATION__";
  const MAX_NAME = __MAX_NAME__;
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "META", "LINK", "HEAD", "TITLE"]);
  const TEXT_TAGS = new Set(["H1","H2","H3","H4","H5","H6","P","LI","TD","TH","PRE","CODE","BLOCKQUOTE","DD","DT","FIGCAPTION","LABEL","IMG"]);
  const LANDMARK_ROLES = new Set(["main","navigation","banner","contentinfo","complementary","search","form","region"]);
  const INTERACTIVE_TAGS = new Set(["A","BUTTON","INPUT","SELECT","TEXTAREA","SUMMARY","DETAILS"]);
  const INTERACTIVE_ROLES = new Set(["button","link","checkbox","radio","tab","menuitem","menuitemcheckbox","menuitemradio","option","switch","textbox","searchbox","combobox","slider","spinbutton"]);

  function clearRefs(root) {
    for (const el of root.querySelectorAll("*")) {
      el.removeAttribute("data-dspy-agent-ref");
      if (el.shadowRoot) clearRefs(el.shadowRoot);
      if (el.tagName === "IFRAME") {
        try { if (el.contentDocument) clearRefs(el.contentDocument); } catch (_) {}
      }
    }
  }
  clearRefs(document);

  const candidates = [];
  let order = 0;

  function clean(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  function isVisible(el) {
    if (typeof el.checkVisibility === "function") {
      try {
        if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
      } catch (e) {}
    }
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
    if (parseFloat(style.opacity) === 0) return false;
    return true;
  }

  function accessibleName(el) {
    const aria = clean(el.getAttribute("aria-label"));
    if (aria) return aria;
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/).map(id => {
        const target = document.getElementById(id);
        return target ? clean(target.innerText || target.textContent) : "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    if (el.tagName === "IMG") return clean(el.getAttribute("alt"));
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      if (el.labels && el.labels.length) {
        const label = Array.from(el.labels).map(node => clean(node.innerText || node.textContent)).filter(Boolean).join(" ");
        if (label) return label;
      }
      if (el.tagName === "SELECT") {
        const selected = el.selectedOptions && el.selectedOptions[0];
        if (selected) return clean(selected.textContent);
      }
      return clean(el.getAttribute("placeholder") || el.getAttribute("name") || el.getAttribute("title"));
    }
    return clean(el.innerText || el.textContent || el.getAttribute("title"));
  }

  function roleOf(el) {
    const explicit = clean(el.getAttribute("role"));
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === "A") return el.hasAttribute("href") ? "link" : "generic";
    if (tag === "BUTTON" || tag === "SUMMARY") return "button";
    if (tag === "INPUT") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "submit" || type === "button" || type === "reset") return "button";
      if (type === "search") return "searchbox";
      return "textbox";
    }
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "SELECT") return "combobox";
    if (tag === "IMG") return "img";
    if (tag === "H1" || tag === "H2" || tag === "H3" || tag === "H4" || tag === "H5" || tag === "H6") return "heading";
    if (tag === "NAV") return "navigation";
    if (tag === "MAIN") return "main";
    if (tag === "FORM") return "form";
    if (tag === "TABLE") return "table";
    if (tag === "UL" || tag === "OL") return "list";
    if (tag === "LI") return "listitem";
    if (tag === "P") return "paragraph";
    if (tag === "PRE" || tag === "CODE") return "code";
    return "generic";
  }

  function isInteractive(el) {
    if (INTERACTIVE_TAGS.has(el.tagName)) return true;
    if (INTERACTIVE_ROLES.has(roleOf(el))) return true;
    return el.hasAttribute("onclick") || el.hasAttribute("tabindex") || el.isContentEditable;
  }

  function isLandmark(el) {
    return LANDMARK_ROLES.has(roleOf(el)) || ["NAV","MAIN","HEADER","FOOTER","ASIDE","FORM"].includes(el.tagName);
  }

  function collect(root, depth) {
    for (const el of root.children || []) {
      if (SKIP.has(el.tagName) || !isVisible(el)) continue;
      const tag = el.tagName;
      const interactive = isInteractive(el);
      const landmark = isLandmark(el);
      const textTag = TEXT_TAGS.has(tag);
      const name = (interactive || landmark || textTag) ? accessibleName(el) : "";
      if ((interactive || landmark || textTag) && (interactive || landmark || name)) {
        candidates.push({
          el,
          depth,
          order: order++,
          interactive,
          landmark,
          role: roleOf(el),
          name: name.slice(0, MAX_NAME),
          tag: tag.toLowerCase(),
        });
      }
      collect(el, depth + 1);
      if (el.shadowRoot) collect(el.shadowRoot, depth + 1);
    }
  }

  collect(document.body || document.documentElement, 0);

  const selected = candidates
    .slice()
    .sort((a, b) => {
      const ap = (a.interactive || a.landmark || a.role === "heading") ? 0 : 1;
      const bp = (b.interactive || b.landmark || b.role === "heading") ? 0 : 1;
      return ap - bp || a.order - b.order;
    })
    .slice(0, MAX_NODES)
    .sort((a, b) => a.order - b.order);

  const nodes = selected.map((candidate, index) => {
    const ref = "e" + GENERATION + "_" + (index + 1);
    candidate.el.setAttribute("data-dspy-agent-ref", ref);
    const node = {
      ref: "@" + ref,
      role: candidate.role,
      name: candidate.name,
      tag: candidate.tag,
      depth: candidate.depth,
    };
    if (candidate.el.value !== undefined && ["input", "textarea", "select"].includes(candidate.tag) &&
        !(candidate.tag === "input" && (candidate.el.type || "").toLowerCase() === "password")) {
      node.value = String(candidate.el.value).slice(0, 200);
    }
    if (candidate.tag === "a" && candidate.el.getAttribute("href")) {
      node.href = candidate.el.getAttribute("href");
    }
    if (candidate.tag === "input") {
      node.input_type = (candidate.el.getAttribute("type") || "text").toLowerCase();
      if (candidate.el.checked) node.checked = true;
    }
    if (candidate.el.disabled) node.disabled = true;
    const placeholder = candidate.el.getAttribute("placeholder");
    if (placeholder) node.placeholder = placeholder;
    const expanded = candidate.el.getAttribute("aria-expanded");
    if (expanded !== null) node.expanded = expanded === "true";
    return node;
  });

  return {
    url: location.href,
    title: document.title,
    nodes,
    truncated: candidates.length > selected.length,
  };
  }
)();
"""


_PROBE_JS = r"""
return (
  function () {
  const existing = window.__dspyAgentProbe;
  if (existing && existing.observer) {
    return { url: location.href, mutations: existing.mutations };
  }
  const probe = { mutations: 0, observer: null };
  try {
    probe.observer = new MutationObserver(function (records) {
      probe.mutations += records.length;
    });
    probe.observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
  } catch (error) {
    probe.observer = null;
  }
  window.__dspyAgentProbe = probe;
  return { url: location.href, mutations: 0 };
  }
)();
"""


_WAIT_SELECTOR_JS = r"""
return (
  function (selector) {
  let element = null;
  try {
    element = selector.startsWith("/") || selector.startsWith("(")
      ? document.evaluate(selector, document, null, 9, null).singleNodeValue
      : document.querySelector(selector);
  } catch (error) {
    return false;
  }
  if (!element) return false;
  const rect = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  return (
    rect.width > 0 &&
    rect.height > 0 &&
    style.display !== "none" &&
    style.visibility !== "hidden" &&
    parseFloat(style.opacity) > 0
  );
  }
)(__SELECTOR__);
"""


_WAIT_TEXT_JS = r"""
return (
  function (text) {
  const body = document.body;
  if (!body) return false;
  const haystack = body.innerText || body.textContent || "";
  return haystack.indexOf(text) !== -1;
  }
)(__TEXT__);
"""


_WAIT_URL_JS = r"""
return location.href.indexOf(__PREFIX__) === 0;
"""


_FIELD_STATE_JS = r"""
const el = this;
return (
  function () {
  const raw = el.value;
  const value = raw === undefined || raw === null ? null : String(raw);
  return {
    tag: el.tagName ? el.tagName.toLowerCase() : "",
    value: value,
    text: value === null ? (el.textContent || "").slice(0, __MAX_TEXT__) : null,
    type: el.getAttribute ? (el.getAttribute("type") || "").toLowerCase() : "",
    disabled: !!el.disabled,
    readonly: !!el.readOnly,
  };
  }
)();
"""
