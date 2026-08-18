# orbit/mcp_servers/ — the MCP servers

Six FastMCP servers, each a thin wrapper whose every tool call goes through a `BaseTool`:
`browser-policy` (proxy over Playwright MCP), `memory` (over `orbit/db.py`), `filesystem` (over the
real local filesystem, scoped by `orbit/config/filesystem_policy.yaml`), `windows-control` (real
mouse/keyboard actuation via pywinauto/pywin32, gated by `orbit/config/windows_control_policy.yaml`),
`communication` (email/calendar, against a swappable backend — see its own section below), and
`screen-perception` (read-only screen/UI observation — see its own section below). Each
`*_server.py` is the process entry point; each `*_tools.py` holds the implementations so they are
unit-testable without the stdio transport. `uia_resolver.py` is neither of those — it's a shared,
non-server module both `windows-control` and `screen-perception` import (see the screen-perception
section for why).

**Adding an `@mcp.tool()` here does not expose it to the model.** A tool becomes reachable only
when its name is added to a skill's `tool_filter` *and* to `orbit/config/risk_tiers.yaml`; miss
either and it is invisible or hard-blocked. Several tools here are intentionally server-side only.

## Both servers return the bare payload, not the ToolResult envelope

`_payload()` unwraps to `result.data` on success and a compact `{error, message}` on failure. The
envelope — `ok`/`confidence`/`untrusted`/`duration_ms`/`event_id` — exists for the system, and
handing it to an LLM forces it to dig for `data.title` instead of `title` on every call, which
measurably degrades tool-calling reliability on mid-tier models. Nothing is lost: `BaseTool.execute`
has already written the complete envelope to the event log before the wrapper unwraps it. The memory
server originally returned `result.model_dump()` and was changed for exactly this reason. Do not
"fix" either server by returning the full envelope.

## browser-policy: a proxy, never a reimplementation

Playwright MCP already does browser automation well. This server exists so profile selection, URL
policy and untrusted-content tagging are enforced somewhere the model cannot route around. Never
reimplement a Playwright primitive here — proxy it.

**One Playwright MCP subprocess per `browser_open`**, bound to the resolved profile's
`--user-data-dir`. Sessions live in the module-level `SESSIONS` dict.

**`_session_owner` exists because of anyio's task rule.** `stdio_client` opens an anyio task group,
and anyio requires the task group be exited by the *same task* that entered it. The previous design
stashed an `AsyncExitStack` in a module-level dict and unwound it from whichever task later called
`browser_close` — illegal, and it deadlocks with "Attempted to exit cancel scope in a different task
than it was entered in" / "generator didn't stop after athrow()". So one task owns each session for
its whole lifetime and waits on a `close_event`.

In-process pytest cannot catch this: pytest-asyncio ran open and close inside a single task, so
enter/exit matched by accident. Under FastMCP every tool call is its own task, so it only appears
once the server is genuinely wired into the agent. Calling *methods* on the `ClientSession` from
other tasks is fine — only the enter/exit of the task group is task-bound.

**Teardown is structural, via the reaper.** `_reap_once` closes on two triggers, neither of which
depends on the model: `task_terminal` (the owning task reached COMPLETED/FAILED/CANCELLED, which
covers the failure path for free) and `idle` (no call for `SESSION_IDLE_TIMEOUT_S`, default 180s —
the only thing that reaps a session abandoned *mid*-task, which a shutdown handler cannot do while
the process is still alive). The model skipped `browser_close` in 2 of 4 measured runs, so anything
model-driven is not a teardown guarantee. Both timings are env-overridable for tests.

`aclose_all_sessions()` **must be awaited on the loop the sessions were created on**. It runs from
the FastMCP `lifespan`, which is inside the server's own loop. The old `asyncio.run(...)` in a
`__main__` finally block spun up a fresh loop and could never have worked — it would have left
orphaned Chrome processes holding profile locks.

## Untrusted content

`_wrap_untrusted` wraps snapshot and extract output in
`<untrusted_web_content source="...">…</untrusted_web_content>`. Snapshot content is page text and
is **never** trusted, regardless of how it is phrased. On the memory side, `_tag_if_external` wraps
any row with `provenance='external'` in `<untrusted_external_content task_id="...">` — provenance
survives retrieval no matter how long the row has sat in our own DB.

Never strip these markers, and never make them conditional on the content looking benign. Beyond
their safety job they are the only runtime evidence that this proxy is in the call path at all —
`orbit/skills/CLAUDE.md` explains why a clean event log is not.

## URL policy is enforced inside the tool, not by tier

`_check_url_policy` runs at the top of `NavigateTool.run`, before the session is even looked up:
scheme allowlist (http/https only, hardcoded) then the `url_policy.yaml` keyword blocklist, both
raising `permission_denied`. Tier assignment does not and cannot do this — `browser_navigate` is
tier `low`. Do not move this check into the tier layer.

`sess.approved_url` is set from the *final* URL after redirects, read back structurally rather than
assumed from the requested URL.

## `_parse_evaluate_result` slices on section markers

Playwright MCP's `browser_evaluate` response is not bare JSON — it is
`### Result\n<json>\n### Ran Playwright code\n```js\n<echoed source>\n````. A greedy first-`{`-to-
last-`}` match picked up a stray `}` from inside the **echoed source** (e.g. `document.title})`) and
corrupted every navigate call's title/final_url. Found by running it against a live page. Split on
the markers; do not go back to brace balancing.

`NavigateTool` retries the evaluate up to 3 times with a 0.3s gap — right after navigation the
execution context can still be mid-transition ("execution context was destroyed").

## Profile resolution: no silent fallbacks, in either tool

`OpenSessionTool` and `GetPolicyTool` resolve a context the same way and must stay mirrored — the
family-profile check was fixed in `browser_open` once and the fix was never carried to
`memory_get_policy`, which then had no owner check at all and happily returned the default
profile's config for `context="mom"`. Both now:

- match by contexts list **and** by profile name, so a family member's profile is refused either way
  → `permission_denied`
- raise `reasoning_failure` for a context matching nothing at all, rather than falling back to the
  profile marked `default: true`. Silently doing the wrong thing is worse than an explicit error.

## task_id resolution

Each server resolves `task_id` as: explicit tool argument → `ORBIT_TASK_ID` from its own
environment → a lazily-materialized `adhoc-*` task row. The environment path is the one that
actually works in production and is what makes session-to-task binding structural rather than
dependent on the model threading an argument through; see `orbit/skills/CLAUDE.md` for how it gets
there. Note `_ORBIT_TASK_ID` is read **once at import**, so a server subprocess serves exactly one
task.

## memory server

There is **no delete path anywhere**, by design — an agent that can quietly erase its own history
is one whose history you cannot trust. `tests/test_memory_tools.py` enforces this by grepping the
module source, so moving code between modules can silently void that guard.

`memory_search_tasks` searches the **tasks** table via FTS5, not the memory table;
`memory_get_context` searches the **memory** table via substring LIKE. They are different stores
with different matching behaviour — pick deliberately. `memory_search_tasks`'s `memory_type`/
`project` params are accepted for forward-compat and are currently no-ops.

`memory_server.py` calls `db.init_db()` in `__main__` because a fresh subprocess has no guarantee
the schema exists; the browser server does the same from its `lifespan`.

## filesystem server

This build runs directly on the user's real Windows machine — there is no container between the
agent and the real filesystem — so unlike the other two servers, the load-bearing safety property
here isn't "wrap untrusted output" but "never let a resolved path leave the sandbox in the first
place." `_resolve_scoped_path` (`filesystem_tools.py`) is the single choke point every tool calls
before touching disk: it resolves `..` traversal and symlinks via `Path.resolve()` *before* either
check runs, then refuses anything outside `filesystem_policy.yaml`'s `allowed_roots`, and separately
refuses anything matching `denylist_keywords` — checked first, and winning even for a path that *is*
inside an allowed root. `allowed_roots` defaults to `data/fs_workspace`, a sandbox the agent owns,
not the user's real Documents/Desktop — widening it is a deliberate config edit, never a per-call
argument.

`fs_write_file`'s `mode='create'` (the default) fails if the target already exists rather than
silently overwriting it — the tool catalog flagged `mode='overwrite'` as a real data-loss risk, and
this is how that risk is contained without a tier this file can't express per-call (the same
"enforce inside the tool, not by tier" precedent as `browser_navigate`'s URL policy).

`fs_move`/`fs_copy` check `src` and `dest` against `_resolve_scoped_path` **independently** — a move
starting inside scope and targeting outside it must fail outright, never partially succeed.

`fs_delete` quarantines: it moves the target into `filesystem_policy.yaml`'s `quarantine_dir` with a
timestamped name plus a `.meta.json` sidecar (`original_path`, `deleted_at`, `ttl_expires_at`)
rather than unlinking it. Nothing sweeps expired entries yet — same accepted-gap pattern as
`db.purge_old_events` (root `CLAUDE.md`'s open issues). It's tiered `high` in `risk_tiers.yaml`
specifically so `SafetyPlugin` blocks it unconditionally with `confirmation_required` until a real
confirm channel exists — the same structural gate the tool catalog specs for `email_send`. It's
fully implemented, not a stub, and deliberately left out of the filesystem skill's `tool_filter` for
the same reason `browser_close` was held back from `research_product`'s: exposing a call that can
never succeed today just costs tool-selection surface area.

`fs_read_file` wraps its output in `<untrusted_local_content path="...">` — a file's bytes are just
as capable of carrying an injection payload as a web page's, so it gets the same treatment
`browser_snapshot` gives page text.

`task_id` resolution and the `adhoc-*` fallback row follow the exact same pattern as `memory_server.py`
(see "task_id resolution" above) — nothing filesystem-specific there.

## windows-control server

This is the actuation half of the perception/actuation pair Section 11 describes. Two structural
properties stand in for the sandboxing filesystem gets from scoped roots, since a mouse click can't
be scoped the way a file path can:

1. **Confidence gating wins over tier.** `_require_confidence` (`windows_control_tools.py`) refuses
   any target below `windows_control_policy.yaml`'s `min_actuation_confidence` (default 0.70) with
   `permission_denied`, checked *before* `windows_click`/`windows_drag` touch pywinauto at all. Raw
   `{x, y}` coordinates are always scored at `Confidence.VISION_INFERRED` (0.50) by
   `_resolve_click_target`, so they never clear that floor — there is no confirm channel to route a
   guessed click through, so this is a hard stop, not Section 7's softer "reverify" state. This reuses
   `orbit.tools.foundation.Confidence`'s existing constants/threshold rather than inventing new ones.
2. **A fail-closed key-combo denylist** (`_is_blocked_combo`) for `windows_key` — Alt+F4,
   Ctrl+Alt+Delete, Win+L are refused outright regardless of tier, the same "enforce inside the tool,
   not by tier" pattern `fs_write_file`'s overwrite handling and `browser_navigate`'s URL policy
   already established. Matched on a canonicalized (lowercased, sorted, `+`-joined) form so
   `"Alt+F4"`/`"f4+alt"`/`"ALT + F4"` all match the same entry.

**ElementRef resolution lives in `uia_resolver.py`, shared with screen-perception, not duplicated
between them.** `resolve_uia_element` builds an `orbit.tools.element_ref.ElementRef` directly from a
pywinauto UIA lookup scoped to a caller-supplied `window_handle` — the same technique
`automation_spikes/pywinauto_notepad_spike.py` proved out, originally written inline in this file
before screen-perception existed and extracted once `perception_get_uia_tree`/
`perception_find_element` needed the identical logic. Ambiguous locators (multiple UIA matches) raise
`reasoning_failure` asking for a narrower `automation_id`/`control_type` rather than guessing which
match was meant — same philosophy as `GetPolicyTool`'s refusal to guess a chrome profile.

**`_resolve_click_target` (still local to `windows_control_tools.py`) is Contract 3's actual
completion point.** `windows_click`/`windows_drag`'s `target` argument accepts three shapes now, not
two: raw `{x, y}` (always vision-tier, always refused), a locator (`{window_handle, automation_id/
name}`, resolved fresh via `resolve_uia_element`), or — new — an ALREADY-resolved `ElementRef` dict
(has `bounds`/`source`/`confidence` keys already set, e.g. straight out of a prior
`perception_find_element` call), used as-is with no second UIA round-trip. That third case is what
makes "call `perception_find_element`, then hand its output straight to `windows_click`" — the
sequence the tool catalog describes for Contract 3 — actually true rather than aspirational.
`tests/test_perception_tools.py::test_perception_get_uia_tree_output_is_windows_click_compatible`
verifies this by round-tripping a real `ElementRef.model_dump()` through
`_resolve_click_target` and asserting `resolve_uia_element` is never called — not just that the two
shapes look similar by inspection.

`windows_type` pastes via the clipboard (`Ctrl+V`) rather than simulating keystrokes character by
character — the pywinauto spike's finding that fast `type_keys()` gets corrupted by autocomplete/IME
behavior on modern (MSIX-packaged) Windows apps applies here too. The clipboard's prior contents are
restored afterward.

`windows_open_app` uses `os.startfile` (ShellExecute — the same resolution a real double-click uses:
App Paths registry, PATH, file associations) rather than `subprocess.Popen`, and has **no allowlist**
by explicit design decision — any path/app name the model provides is launched. This is real code
execution capability; it's kept at the tool catalog's plain medium tier without extra restriction.

`windows_focus_window` is fully implemented but tiered `high` — the tool catalog itself flags
stealing OS focus mid-task as a real UX problem ("only run when idle / always ask first / show a
countdown") with no mitigation built anywhere in this codebase, so it's structurally blocked by
`SafetyPlugin` the same way `fs_delete`/`email_send` are, rather than shipped at a tier that would let
it fire silently.

Horizontal scroll and Win-key combinations are unimplemented, not silently approximated — the same
"honest scope note" pattern `browser_policy_tools.py`'s module docstring uses for
`browser_click`/`browser_type`.

**This server is `lane: foreground` end to end, and that gate is enforced in `orbit/agent.py`, not
here or in the skill.** See the root `CLAUDE.md`'s note on `build_agent`'s `lane` parameter — a task
running in the `headless` lane must never be able to see these tools at all, because
`orbit/task_manager.py`'s single-flight foreground lock (the thing that actually prevents two
input-simulating tasks from colliding) is only held for `lane="foreground"` tasks.

## communication server

Unlike every other server here, this one talks to something that fundamentally requires a human to
provision first: a real mailbox needs either an OAuth consent grant (Gmail/Google Calendar API) or an
app-specific password (IMAP/SMTP), and neither can be obtained by an agent acting on its own — that's
account access, not a config value. So the server is built against a **swappable backend interface**
(`communication_backend.py`'s `MailBackend` Protocol) rather than against any specific provider, and
today the only implementation is `LocalMailBackend` — a genuinely-working local SQLite store (its own
file, `data/communication_local.db`, deliberately NOT `orbit/db.py`'s task/event/memory schema) that
exercises the full draft/send/search/read/list contract for real, it just never talks to an actual
mail server. This is an honest stand-in, not a pile of stub methods that return empty lists to satisfy
the Protocol's shape — drafts persist, `send` actually inserts a "sent" message, `search`/`read`
genuinely query what's there.

**A real backend drops in by implementing the same `MailBackend` Protocol** and registering itself in
`get_backend`'s `_BACKENDS` dict; nothing in `communication_tools.py` needs to change shape.
`get_backend` raises a hard `ValueError` for any unregistered name (e.g. `"gmail"` before that backend
exists) rather than silently falling back to `"local"` — same "no silent fallback" rule
`chrome_profiles.yaml`'s profile resolution already established, now applied to backend selection too.

**Account context resolution mirrors `browser_policy_tools.py`'s non-owner-profile pattern exactly**,
including the same bug class it was built to close: `_resolve_account` (`communication_tools.py`)
checks by both `contexts` membership and account name, so a `mom`/`dad`/`sister` context is refused
either way it's phrased, and an unrecognized context is an explicit `reasoning_failure` rather than a
silent fallback to the default account.

**`email_send` is blocked at TWO independent layers, not one.** Every other high-tier tool in this
codebase (`fs_delete`, `windows_focus_window`) relies on `risk_tiers.yaml`'s tier assignment alone —
`SafetyPlugin` blocks it before it runs, full stop. `EmailSendTool.run` goes further: even called
directly, bypassing `SafetyPlugin` entirely (a test, or a hypothetical registry misconfiguration), it
unconditionally raises `permission_denied` without even inspecting `approval_token` — there is no
confirm channel anywhere in this build that could have minted a valid one, so no token value should
ever be treated as a reason to proceed. This is the tool the whole "high tier = unconditionally
blocked absent a confirm channel" design rule (Section 5 of the catalog) was written around.
`LocalMailBackend.send` itself IS fully implemented and directly unit-tested (bypassing the blocked
tool) — unblocking `email_send` later is a one-line change in the tool, not new backend work.

`email_read` wraps its body in `<untrusted_email_content>` markers unconditionally, same as every
other content-bearing tool (`browser_snapshot`, `fs_read_file`) — an email body is just as valid an
injection vector as a webpage, regardless of which backend actually produced it.

`task_id` resolution follows the exact same pattern as `memory_server.py` (see "task_id resolution"
above). This server is `lane: headless` — no OS actuation happens here, so unlike windows-control it
needs no lane gating in `orbit/agent.py` and is wired in unconditionally.

## screen-perception server

The read-only half of the perception/actuation pair Section 11 describes, built AFTER
windows-control — which mattered, because windows-control had already solved UIA resolution on its
own (`_resolve_uia_element`, written when this server didn't exist yet). Building this server meant
extracting that logic into `uia_resolver.py` (shared by both, imported by neither's *tool* code — see
that module's own docstring) rather than writing a second implementation that could drift from the
first. `windows_control_tools.py` was refactored to import from there; its own test suite
(`test_windows_control_tools.py`, `test_windows_control_live.py`) was re-run after the extraction to
confirm the refactor didn't change behavior, not just that it compiled.

**Two of the catalog's seven tools are deliberately absent, not stubbed:**

- `perception_read_text_region` (OCR) — the catalog names PaddleOCR; no OCR engine is installed in
  this environment (Tesseract needs a separate system-level binary install outside pip; PaddleOCR/
  EasyOCR pull in a multi-hundred-MB ML stack). Installing one unattended is a materially bigger,
  more consequential call than the `mss` screenshot library actually added (small, pure-Python, no
  system dependency) — the kind of thing to confirm rather than assume, so it wasn't done.
- `perception_vision_locate` — the catalog is explicit that this one's signature should NOT be
  guessed: "the representation decision belongs in the [grounding] spike's output, not guessed here."
  No such spike has run in this build, and whether vision calls would even go through Nemotron or a
  dedicated UI-TARS deployment is unconfirmed (root `CLAUDE.md`'s known issues). Guessing now risks
  exactly the throwaway-and-redo work the catalog is warning against.

Neither is faked with placeholder output — both are simply not in this server's tool set, the same
"honest scope note" pattern `browser_policy_tools.py` used for `browser_click`/`browser_type` and
`windows_control_tools.py` used for horizontal scroll and Win-key combos.

**`perception_find_element` only resolves the `uia` tier.** `tier_order` is still accepted (forward
compatibility with the eventual 3-tier version) but any non-`"uia"` entry is reported back in
`tiers_unavailable` rather than silently ignored — the caller can tell the difference between "no
match" and "that tier doesn't exist yet." An element with no UI Automation representation at all
(some custom-drawn UI) simply cannot be resolved by this tool in this build; there is no OCR/vision
fallback to catch it.

**`perception_get_uia_tree` is depth- and node-capped** (`uia_resolver.get_uia_tree`, defaults
`max_depth=6`, `max_nodes=200`) for the same reason `fs_read_file`/`memory_get_context` cap their
own output — the catalog's "answers most questions for free" framing only holds if one call can't
flood the model's context with an entire native app's control tree. Walk order is depth-first,
root-first, so truncation returns a sensibly-rooted partial tree, not an arbitrary slice — verified
in `test_uia_resolver.py` against a fake element tree (deterministic, not dependent on whatever
happens to be the real foreground window during a test run).

`perception_capture_screenshot`/`perception_wait_for_visual_change` use `mss` (`MSS`, not the
deprecated `mss.mss()` alias), not the catalog's named DXCam/`Windows.Graphics.Capture` — see the
module docstring for why. Screenshots return base64 PNG bytes directly, never a file path; nothing is
written to disk by this server. `perception_wait_for_visual_change` compares raw RGB bytes between
successive grabs with no fuzz threshold — any byte difference counts as a change.

Every tool here is `risk_tier: low` and `lane: headless`, and — like `communication` — needs no lane
gating in `orbit/agent.py`: nothing simulates input, so it's wired in unconditionally, matching
Section 11's "perception free and always-on, actuation gated" framing exactly.
