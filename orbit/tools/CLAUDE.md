# orbit/tools/ — the BaseTool contract

`foundation.py` is not a tool. It is the contract every real tool inherits, and the single most
important invariant in the project lives here.

## execute() is the entry point. run() is the only thing you write.

`execute()` is public and is **never overridden**. Subclasses implement `run(args, token)` and
nothing else. Everything that must happen on every call happens in `execute`, which is precisely
why a tool author cannot forget any of it:

- cancellation pre-check before the body runs at all
- `asyncio.wait_for(self.run(...), timeout=self.timeout_s)`
- every exception converted to a `ToolError` — the model never sees a traceback
- `redact_secrets()` over both args and data before anything is written
- one event row via `db.log_event`, on success *and* on failure, with the `event_id` returned on
  the `ToolResult`

If you find yourself wanting to override `execute`, or to call `run()` directly from anywhere, the
design has gone wrong. Do neither.

`run()` returns a `(data, confidence)` tuple. `confidence` may be `None` where grounding does not
apply. Use the `Confidence` constants (`API_SUCCESS`, `UIA_AUTOMATION_ID`, `UIA_NAME_MATCH`,
`OCR_MATCH`, `VISION_INFERRED`) rather than bare floats — they encode *how* an action was grounded,
which is the whole point of the number.

## Raise ClassifiedToolError when you know the kind

`execute`'s catch-all can only ever produce `kind="tool_failure"`. That collapses genuinely
different failure modes into one bucket and defeats the failure routing built on top of it —
`tool_failure` retries within the cap, `state_failure` re-observes, `reasoning_failure` re-plans,
and the retry cap itself differs by kind. So whenever the correct `ErrorKind` is already known at
the raise site, raise `ClassifiedToolError(kind, message, retryable=..., details=...)`:

- a policy refusal → `permission_denied`
- a page that navigated out from under us → `state_failure`
- the caller invented an argument value (e.g. a context name that matches nothing) →
  `reasoning_failure`, because the right response is to check config, not to retry the same string

`ClassifiedToolError.kind` is passed through untouched by `orbit/policy.py`'s `classify_failure`,
never re-guessed from the message — including when the message text would classify differently.

## Timeouts

`default_timeout_s = 30.0`. **Override it on any tool that spawns a subprocess.** 30s is sized for
an in-process call; a tool that launches a process behind a process will time out mid-startup and
report `timeout` for what was really a slow but healthy launch. `browser_open` is the live example
of this being wrong — see the root `CLAUDE.md` open-issues list. Pass `timeout_s=` to the
constructor, or set `default_timeout_s` on the subclass.

The outer `wait_for` is a ceiling, not an interrupt. A `run()` that does long work must call
`token.raise_if_cancelled()` between its own await points — otherwise cancellation cannot land
until the body next yields.

## The metadata is enforcement, not documentation

`ToolMetadata` is validated at construction: `risk_tier="high"` with `requires_confirmation=False`
raises `ValidationError`. A high-risk tool that forgot to require confirmation cannot be built.

`ToolError.kind` has **no default**. This is intentional — the recovery system routes on it, so an
author must decide it rather than inherit a wrong one silently.

`returns_untrusted_content` is a static per-tool flag. It is not the only taint mechanism: memory
tools declare it `False` (retrieved rows are structurally our own data) while still wrapping
individual `provenance='external'` rows inline. Do not read the flag as "this tool's output is
always safe".

## Secret redaction

`redact_secrets` walks dicts/lists/strings and blanks two ways: any *key* matching
password/secret/token/api_key/credential/auth, and any *value* matching a known key shape. The
value patterns include a bare `\b[0-9a-f]{32,40}\b`, because Deepgram keys have no fixed prefix —
they are plain lowercase hex, and a real one obtained during this build was 40 chars even though
the docs' own example is 32. That pattern also matches a stray MD5/SHA-1 hash; over-redacting a
hash is the accepted cost of not leaking a real key that appears outside a suspiciously-named
field. Do not narrow it to 32 on the strength of the doc example.

## Where the event row goes

`_finish` writes to the same `events` table as the rest of the system (`orbit/db.py`), not a second
store — one audit trail, not two that disagree. On failure it records `f"{kind}: {message}"` in
`error` and leaves `result` null; on success the reverse. This write happens inside the MCP server
process, so it lands under whatever `task_id` that server resolved; see
`orbit/mcp_servers/CLAUDE.md`.

## `element_ref.py` — Contract 3

`ElementRef` (`{element_id, role, name, bounds, state, source, confidence}`) is the shape a resolved
UI element takes everywhere in this codebase — windows-control's `_resolve_click_target` and
screen-perception's `perception_find_element` both produce and consume exactly this, via a shared
resolver (`orbit/mcp_servers/uia_resolver.py`) rather than each server inventing its own. It was
written here, in the shared foundation layer, before screen-perception existed — the point of
defining the contract early is that the second implementation didn't have to guess the first one's
shape.

`confidence` always comes from `Confidence`'s constants above, never a bare float invented in the
resolver — that's what lets `windows_control_tools._require_confidence` compare an `ElementRef`
against a policy threshold regardless of whether it came from a fresh UIA lookup or was handed in
pre-resolved from a prior `perception_find_element` call. `.center()` is the only behavior on the
model — the (x, y) midpoint of `bounds`, used by every actuation tool that needs a single point to
click/drag rather than a rectangle.
