# Browser tools

`dspy_agent_tools.browser` drives a locally installed Chrome in headless mode
through pydoll and exposes it as ordinary `dspy.Tool` functions. Every call
returns a dict whose `status` is `ok` or `error`; check it before reading the
rest of the payload.

The browser is a real one: it has the caller's network reach, keeps cookies,
and runs page JavaScript. Treat page content as untrusted input.

## Install

```bash
uv add 'dspy-agent-tools[browser]'
# or: pip install 'dspy-agent-tools[browser]'
```

The extra installs DSPy and pydoll. Importing `Browser` works without it;
calling `Browser()` raises `ImportError` with the install hint. Chrome must be
installed locally — pydoll starts it directly, with no separate browser
download step.

Video recording uses `ffmpeg` from `PATH` when present. To let the package
supply a binary, install the optional extra:

```bash
uv add 'dspy-agent-tools[ffmpeg]'
```

The extra uses `imageio-ffmpeg`'s bundled binary. Without either one,
`browser_record_start` returns an error instead of silently producing no file.

## Quick start

`Browser()` returns the browser toolset as one ready-made list of `dspy.Tool`
objects. Nothing is created or cleaned up by hand:

```python
import dspy
from dspy_agent_tools import Browser

agent = dspy.ReAct("task -> result", tools=Browser(), max_iters=12)
result = agent(task="Open https://example.com and summarize the page")
```

`Browser()` includes:

| Tool | Purpose |
| --- | --- |
| `browser_open` | Navigate and snapshot the new page in one call. |
| `browser` | Run one action: navigate, click, type, press, hover, select, scroll, back, forward, reload, or wait. |
| `browser_snapshot` | Read the current page as structured nodes or a text tree. |
| `browser_tabs` | List, open, switch, and close tabs. |
| `browser_network` | Inspect the requests the page made and fetch a response body. |
| `browser_wait_response` | Wait for a matching response or failed request. |
| `browser_pdf` | Print the current page to a PDF file. |
| `browser_save_tabs` / `browser_restore_tabs` | Save and reopen tab URLs. |
| `browser_cookies` | Read, write, clear, save, and load cookies. |
| `browser_screenshot` | Capture a PNG, JPEG, or WebP. |
| `browser_evaluate` | Run JavaScript in the page and return its value. |
| `browser_close` | Stop this tool list's browser session early. |

Two flags extend the list:

```python
Browser(include_recording=True)  # adds browser_record_start/stop
Browser(include_recording=True, show_cursor=True, humanize=True)
Browser(include_search=True)  # appends web_search
```

The individual functions (`browser`, `browser_snapshot`, …) remain available
when you want a hand-picked list. They share a module-level session; each
`Browser()` call instead creates its own isolated session. To share deliberately,
pass the same `BrowserSession` to each bundle.

## Browser lifecycle

The browser starts on the first tool call, not when `Browser()` is called.
Later calls reuse the same tab until the session is closed. These options apply
to both headless and visible (`headless=False`) browsers:

- Call `browser_tools.close()` after the agent finishes, as in the README.
  If the agent raises an exception, that call will be skipped unless it is in
  a `finally` block.
- Use `with Browser() as browser_tools:` to close the session when the block
  exits, including when an exception occurs.
- Rely on automatic cleanup at normal interpreter exit. The browser stays open
  after the agent finishes until the process exits; abrupt termination may
  prevent cleanup.

For example, use a context manager:

```python
from dspy_agent_tools import Browser

with Browser() as tools:
    agent = dspy.ReAct("task -> result", tools=tools)
    result = agent(task="Open https://example.com and summarize the page")
# the session was closed on exit
```

Or use `finally` without a context manager:

```python
browser_tools = Browser(headless=False)
try:
    agent = dspy.ReAct("task -> result", tools=browser_tools)
    result = agent(task="Open https://example.com and summarize the page")
finally:
    browser_tools.close()
```

The `browser_close` tool and `BrowserSession.close()` also close their session.
After closing, a later browser tool call starts a fresh session.

## Sessions and isolation

One `BrowserSession` is one lazily started browser. On a shared server, give
each user or request its own session so cookies, history, and tabs cannot leak
between callers:

```python
import dspy
from dspy_agent_tools import Browser, BrowserSession

session = BrowserSession()
tools = Browser(session=session)  # or session.tools()
agent = dspy.ReAct("task -> result", tools=tools)

result = agent(task="Open https://example.com and summarize the page")
tools.close()  # stops this session only
```

`Browser()` forwards these keywords to the session it creates:

| Keyword | Default | Effect |
| --- | --- | --- |
| `headless` | `True` | Run Chrome without a visible window. |
| `timeout` | `15` | Seconds for element lookup, page load, and the settle wait. |
| `dialog_policy` | `"dismiss"` | Answer page dialogs with dismiss or accept. |
| `user_data_dir` | `None` | Persistent Chrome profile directory. |
| `cookie_state` | `None` | JSON cookie file loaded at startup and rewritten on cookie changes and close. |
| `allowed_schemes` | `http`, `https`, `about`, `data` | URL schemes the browser may open. |
| `url_policy` | `None` | Filters URLs passed to explicit navigate and new-tab calls. |
| `humanize` | `False` | Use pydoll humanized click, type, hover, and scroll. |
| `stealth` | `False` | Enable humanized input; optionally provide a fingerprint. Pydoll already uses CDP without WebDriver. |
| `fingerprint` | `None` | Pydoll fingerprint profile applied before navigation on each installed tab. |

Passing any of them together with `session=` raises `ValueError`, because the
session would be built twice. `BrowserSession` also accepts `window_size`
(default `(1440, 900)`).

`file:`, `chrome:`, and `view-source:` are blocked unless you add them to
`allowed_schemes`. A policy can reject individual hosts while the default
scheme set stays in place:

```python
session = BrowserSession(
    url_policy=lambda url: not url.startswith("https://internal.example")
)
tools = Browser(session=session)
```

**These URL checks are not a security boundary.** They do not prevent redirects,
links, JavaScript, popups, or subresource requests from reaching other URLs.
Use an egress proxy or network isolation to enforce destination restrictions.

Pydoll does not provide a single stealth switch or bundled fingerprint profiles.
`stealth=True` enables humanized input, and `fingerprint` accepts a profile for
pydoll’s `apply_fingerprint`. A profile must match the host and browser; neither
setting guarantees a site will accept the browser. Fingerprints on popups are
applied when the tab is discovered, which may be after its first navigation.

Navigation fills in a missing scheme: `example.com` becomes
`https://example.com`, while `localhost` and loopback addresses use `http`.

## Actions

`browser(action, target=None, value=None, timeout=15)` runs one action.

| Action | `target` | `value` |
| --- | --- | --- |
| `navigate` | — | URL. |
| `click` | Element to click. | — |
| `upload` | File input. | Path to an existing local file. |
| `type` | Input, textarea, or contenteditable element. | Text to type. The field is cleared first. |
| `press` | — | Key name or combination: `Enter`, `Tab`, `Escape`, `ArrowDown`, `Control+A`. |
| `hover` | Element to move over. | — |
| `select` | `<select>` element. | Option value. |
| `scroll` | — | `down`, `up`, `top`, `bottom`, or a pixel count. |
| `back` / `forward` | — | — |
| `reload` | — | — |
| `wait` | Selector to become visible. | Condition, or seconds. |

`target` accepts an `@ref` from `browser_snapshot`, a CSS selector, an XPath
expression, `css=...`, `xpath=...`, `text=Exact text`, or `text*=Partial text`.
`target` and `value` also accept numbers, converted to text; `scroll` with
`value=300` scrolls 300 pixels.

### What a click does

A click is refused before it fires when the element is not visible, is
disabled, or is covered by another element at its centre point. The error
names the reason, so the agent can dismiss the overlay or target the covering
control instead of retrying blindly.

A click that runs reports what changed:

```python
browser("click", target="@e3")
# {"status": "ok", "method": "native", "changed": True, "effect": "dom", ...}
```

`effect` is `dom`, `navigation`, or `none`. `none` adds a `warning`, because a
click that changes nothing usually means the target was inert or an overlay
ate it. A native click that fails falls back to a JavaScript click, and the
result says so in `warning` (`method` becomes `"js"`).

### What typing does

`type` clears the field, types the text, then reads the field back:

```python
browser("type", target="#email", value="hi@example.com")
# {"status": "ok", "verified": True, ...}
```

`verified` is `True` only when the field now holds exactly `value`. A
maxlength, mask, or input handler adds `verified: False`, `actual_length`, and
a `warning`; `actual` carries the read-back text unless the field is a
password. `select` verifies the same way and reports the selected option.

## Snapshots and refs

`browser_snapshot()` returns the page as structured nodes by default:

```python
{
    "status": "ok",
    "url": "https://example.com/",
    "title": "Example",
    "node_count": 42,
    "truncated": False,
    "nodes": [
        {
            "ref": "@e0123456789abcdef0123456789abcdef_1",
            "role": "heading",
            "name": "Example",
            "tag": "h1",
            "depth": 0,
        },
        {
            "ref": "@e0123456789abcdef0123456789abcdef_3",
            "role": "textbox",
            "name": "Email",
            "tag": "input",
            "depth": 2,
        },
    ],
}
```

Each node carries an opaque `ref` (such as `@e..._1`), `role`, `name` (estimated accessible name), `tag`, and
`depth`; form controls and links add `value`, `href`, `checked`, `disabled`,
`placeholder`, or `expanded` when relevant. The same `ref` works as the
`target` of the next action:

```python
from dspy_agent_tools import browser, browser_snapshot

snapshot = browser_snapshot()
email = next(node for node in snapshot["nodes"] if node["role"] == "textbox")
browser("type", target=email["ref"], value="hello@example.com")
browser("click", target="text=Sign in")
browser("press", value="Enter")
```

`format` selects the representation:

- `nodes` (default) — structured list.
- `text` — only the compact indented tree, which costs fewer tokens.
- `both` — both.

`max_nodes` defaults to 400 and is capped at 1000. Interactive elements and
landmarks are kept first when a long page is truncated; `truncated` says
whether anything was cut. Password input values are omitted. The snapshot walks
open shadow roots and discovers iframe documents, including cross-origin frames
when pydoll can access them. `frames_unavailable` reports frames that could not
be read. Closed shadow roots and frames nested in shadow roots are not included.
Every snapshot clears and reassigns refs, so take a
fresh one after navigation or DOM changes. A stale `@ref` fails with a message
asking for a new snapshot.

## Waiting

Wait for a condition instead of sleeping:

```python
browser("wait", target="#results")  # selector becomes visible
browser("wait", value="text=Order confirmed")  # body text
browser("wait", value="text*=Order")  # body text substring
browser("wait", value="url=https://example.com/next")  # URL prefix
browser("wait", value="selector=#done")  # selector= form
browser("wait", value=2)  # seconds
```

A bare number sleeps that many seconds (default 1, capped at 30). A timed-out
wait returns `status="error"` naming the condition, rather than letting the
next action run against a half-loaded page. Each action's `timeout` defaults
to 15 seconds and is capped at 120; a wait additionally caps its own budget at
30 seconds.

## Diagnostics and dialogs

Every result may carry diagnostics collected since the previous call, so a
failed step explains itself without a second request:

- `console` — page console output, with repeated messages collapsed into
  `count`.
- `errors` — uncaught page exceptions.
- `dialog` — a dialog that opened, with its `type`, `message`, and `handled`
  value.

JavaScript dialogs are answered automatically. The default policy dismisses
them, so a `confirm()` never hangs the session, and the result reports it:

```python
browser("click", target="text=Delete")
# {"status": "ok", ..., "dialog": {"type": "confirm",
#  "message": "Delete everything?", "handled": "dismissed"}}
```

Dialogs are answered as they open, from a separate thread, because the command
that triggered one — a click, a navigation — does not return until the dialog
closes. That is why the policy is fixed per session rather than decided per
dialog: pass `BrowserSession(dialog_policy="accept")` to confirm instead of
cancel. A page that asks "discard your changes?" needs `accept` to let the
navigation through.

## Tabs

`browser_tabs` lists tabs, and `new`, `switch`, and `close` change which one
every other tool acts on:

```python
browser_tabs()
# {"status": "ok", "count": 1,
#  "tabs": [{"index": 0, "url": ..., "title": ..., "active": True}], ...}
browser_tabs("switch", "checkout")  # by URL or title substring, or by index
browser_tabs("new", "https://example.com/report")
browser_tabs("close")  # close the active tab
```

- `list` returns every page tab, oldest first, with `index`, `url`, `title`,
  and `active`.
- `new` opens a tab and makes it active; `value` is an optional URL.
- `switch` takes an index or a URL/title substring. An ambiguous match fails
  with the candidate list instead of guessing.
- `close` closes the tab named by `value` (default: the active one) and
  activates the newest remaining tab. Closing the last tab stops the browser;
  `closed_all` is set, and the next call starts a fresh one.

A click that opens a popup reports `tabs_changed` and a warning naming
`browser_tabs`, so "nothing changed" never means "a tab opened behind you".

## Network

`browser_network` reports what the page actually fetched. Requests are
recorded for every tab from the moment the browser starts, so no second click
is needed:

```python
browser_network("clear")
browser("click", target="text=Load orders")
browser_network()
# {"status": "ok", "count": 1, "requests": [
#   {"id": "...", "method": "GET", "url": ".../orders", "status": 200,
#    "type": "fetch", "mime": "application/json"}]}
browser_network("body", "<id>")  # the response body for one request
```

- `requests` (default) returns the newest `limit` requests, oldest first, with
  `id`, `method`, `url`, `type`, `status`, `mime`, and `failed` when the load
  errored. `value` filters by URL substring or exact request id.
- `body` returns one response body, truncated to 4000 characters with
  `truncated` set when it was cut.
- `clear` forgets recorded requests, so the next action's traffic is easy to
  spot.

## Cookies and saved logins

`browser_cookies` reads and writes cookies for the current browser context:

```python
browser_cookies()  # every cookie
browser_cookies("get", "example")  # filter by name or domain substring
browser_cookies("set", '{"cookies": [{"name": "a", "value": "1"}]}')
browser_cookies("set", "a=1; b=2")  # raw Cookie: header form
browser_cookies("clear")
```

`get` returns `name`, `value`, `domain`, `path`, `expires`, `httpOnly`,
`secure`, and `sameSite`. `set` accepts a JSON list of cookie objects (or an
object with a `cookies` list) or a raw `Cookie:` header; cookies without a
`domain` or `url` are scoped to the current page.

`save` and `load` move that state to a file:

```python
browser_cookies("save", "state.json")  # {"url": ..., "cookies": [...]}
browser_cookies("load", "state.json")  # in a later run
```

`cookie_state=` does it automatically. The file is loaded when the browser
starts and rewritten after a cookie tool changes the cookies and on close,
with no profile needed:

```python
agent = dspy.ReAct("task -> result", tools=Browser(cookie_state="state.json"))
```

That is the same format `browser_cookies("save", path)` writes, so a state
captured by hand can be handed to the constructor, and the other way round.
Only load files you produced: `load` re-checks nothing.

## Persistent profiles

For a session that keeps its own logins, storage, and history, give the
browser a profile directory — directly on the ready-made tool list, or on a
session you manage yourself:

```python
agent = dspy.ReAct("task -> result", tools=Browser(user_data_dir="./browser-profile"))

session = BrowserSession(user_data_dir="./browser-profile", dialog_policy="accept")
tools = Browser(session=session)
```

Chrome writes that profile on shutdown, so `close()` asks it to exit and waits
briefly before ending the process — without that wait the cookie database is
left half-written and the login the profile was meant to keep is lost.
Without a directory, every run starts from an empty temporary profile.

## Video recording

Record the session by bracketing the actions:

```python
from dspy_agent_tools import browser, browser_record_start, browser_record_stop

browser_record_start(path="checkout.mp4", fps=20, show_cursor=True)
browser("navigate", value="https://example.com/checkout")
browser("click", target="text=Pay")
video = browser_record_stop()
# {"status": "ok", "path": ..., "format": "mp4", "bytes": ...,
#  "frames": 37, "duration_s": 3.7}
```

`browser_record_start(path=None, fps=10, quality=80, max_width=None,
max_height=None, show_cursor=False)`:

- `path` defaults to a temp file. `.mp4` uses H.264 and `.webm` uses VP9;
  other suffixes are rejected.
- `fps` caps how many frames per second are kept (1–60).
- `quality` applies to the intermediate JPEG frames (0–100).
- `max_width` / `max_height` downscale the capture.
- `show_cursor` draws a pointer and click indicator in the recorded tab. Use
  `Browser(humanize=True, include_recording=True)` or
  `BrowserSession(humanize=True)` for gradual mouse paths. Without humanized
  input, the pointer can jump directly to an element. The overlay is part of
  the page while recording, so it may also appear in screenshots. It follows
  mouse events, not JavaScript-only clicks, and is limited to the recorded tab.
  Set `show_cursor=True` on `Browser()` (or `session.tools()`) to make it the
  default for that tool list; `browser_record_start(show_cursor=False)` can
  override it. Recording still needs to be started and stopped explicitly.

Frames come from Chrome's CDP screencast and are encoded with `ffmpeg` when
recording stops. `fps` is a capture cap, not a guarantee: Chrome only emits
frames when the page changes, and the video preserves those timestamps. The
recording is video-only; page audio is not captured. `browser_close` also stops
and encodes an active recording and returns it under `recording`.

## JavaScript

`browser_evaluate(script, await_promise=True)` runs JavaScript in the current
page. Use a `return` statement:

```python
browser_evaluate("return document.title")
browser_evaluate("return fetch('/api/orders').then(r => r.json())")
```

The value is converted by value, so objects and arrays come back as Python
dicts and lists. `await_promise` waits for a returned promise. Exceptions
become `status="error"` with the JavaScript message.

## Screenshots

```python
browser_screenshot()  # temp PNG
browser_screenshot("page.jpg", full_page=True)
browser_screenshot(as_base64=True)  # inline image, no file
```

Without `path`, the image is written under the system temp directory and the
returned `path` is absolute. A path without a suffix gets `.png`; `.png`,
`.jpg`/`.jpeg`, and `.webp` are supported. `full_page` captures beyond the
viewport. `quality` applies to JPEG and WebP.

The result carries `path`, `format`, and `bytes`; with `as_base64=True` it
carries `base64` and `format` instead.

## Result contract

Every tool returns `{"status": "ok" | "error", ...}`. On failure, `error`
carries the reason, and diagnostics collected since the previous call are
merged into the same dict. A missing browser is started on demand, so a call
after `browser_close` or a crash starts a fresh one.

`browser_close` returns `closed` (whether a browser was running) and, when a
recording was active, `recording`.

## Safety

- Browser tools drive a real headless Chrome with the caller's network reach.
  `browser_evaluate` runs arbitrary JavaScript in the page.
- Treat page content as untrusted input. A page can ask the agent to run code,
  navigate elsewhere, or reveal data it can read.
- Use one `BrowserSession` per user or session on a shared server so cookies
  and tabs do not leak between callers.
- `allowed_schemes` and `url_policy` filter explicit navigations only. Use
  network and operating-system isolation to enforce access restrictions.
- `dialog_policy="accept"` confirms every dialog, including destructive ones.

## Response waits and file uploads

```python
browser_network("clear")  # discard old matches before triggering an action
browser("click", target="text=Load orders")
response = browser_wait_response("/orders", timeout=15)
browser("upload", target='input[type="file"]', value="./invoice.pdf")
```

A response wait searches the bounded request history first, then waits for
an event; it also reports network failures in `response.failed`. The watcher
starts on tab discovery, so it cannot report requests before attachment or
before a popup was discovered. Clear the network history before the triggering
action when a prior response must not match. Upload paths refer to the machine
running Chrome and must be accessible to it.

## Attach, restore tabs, and PDF

```python
session = BrowserSession(ws_address="ws://127.0.0.1:9222/devtools/browser/...")
tools = Browser(session=session)
# A person can use the same browser while the agent is paused.
session.save_tabs("tabs.json")
session.restore_tabs("tabs.json")  # adds tabs; existing tabs remain open
session.pdf("page.pdf", width=8.5, height=11)  # inches
session.close()  # disconnects, leaving the attached Chrome running
```

The WebSocket address must refer to an already-running Chrome with remote
debugging enabled. Attachment controls a live browser with its existing
credentials. The ordinary dialog policy still applies, and a human and an
agent controlling the same tab can interfere with each other. `ws_address`
cannot be combined with `user_data_dir`, `cookie_state`, or `fingerprint`.
The detach implementation uses Pydoll's private connection cleanup API because
its public `stop()` closes the remote browser; it may need adjustment after a
Pydoll upgrade.

The tab state file contains URLs and the active tab index, not cookies,
storage, tab history, or window arrangement. Use a persistent profile for
logins and storage. Restore validates every URL against the explicit navigation
policy before opening tabs; a navigation error may leave partially opened tabs.
Treat state files as sensitive because URLs can contain secrets. PDF export
prints page backgrounds with zero margins; its destination directory must exist.

For trusted Python callers, `session.cdp("Domain.method", {"parameter": value})`
sends an arbitrary CDP command to the active page and returns Pydoll's raw
response. It is intentionally not exposed as a default agent tool and is not
subject to the explicit-navigation URL checks.
