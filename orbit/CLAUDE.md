# orbit/ — core runtime

`agent.py` builds it, `run_task.py` drives it, `task_manager.py` schedules it, `safety_plugin.py` polices
it, `db.py` records it, `degradation.py` is what the user sees when the provider dies.

## Two-lane scheduler (`task_manager.py`)

The lanes are not two speeds of the same thing — they encode different constraints:

- **foreground** is an `asyncio.Lock`: strictly single-flight, queued. There is one mouse and one
  keyboard, so two input-simulating tasks would each land actions in the other's target window.
  This is a correctness requirement.
- **headless** is an `asyncio.Semaphore(5)`: a soft cost/rate-limit cap only. Raising it risks
  spend, not correctness.

Lane is a static property of the skill, never a runtime guess. `submit()` rejects any lane not in
`db.LANES` before doing anything else.

**This lock only protects a task actually submitted under `lane="foreground"`.** Until the
windows-control skill existed, every real caller passed `lane="headless"` (the `run_task()` default)
unconditionally, so this was true in spirit but never load-bearing. It became load-bearing the moment
windows-control (real mouse/keyboard actuation) existed: a task carrying that skill but submitted
under `lane="headless"` would run its input-simulating tool calls through the `Semaphore(5)` instead
of this lock — up to 5 concurrent tasks each trying to drive the real mouse at once, with no
serialization at all. `TaskManager` itself cannot prevent that; it only enforces the lane a caller
already chose. See `agent.py`'s note below for where the actual prevention lives.

## Cancellation is dual, deliberately

`TaskManager.cancel()` fires **both**:

1. the cooperative `CancellationToken`, checked by `SafetyPlugin.before_tool_callback` ahead of
   every single tool call, and
2. `asyncio.Task.cancel()`, a hard interrupt at the current await point.

Neither alone is sufficient: a tool already mid-flight will not necessarily reach another token
check between awaits, and a hard cancel alone can land in the middle of an action. Keep both.
`CancellationToken` is defined here and reused by `orbit/tools/foundation.py` on purpose — one
cancellation story for the whole system, not two that drift.

## `policy.py` is data, `safety_plugin.py` is enforcement — and why they split

They were one file until 2026-09-08. The split is a layering fix: thirteen call sites import
`policy.py` — every MCP server, the GUI, `confirmation.py` — and **every one of them wants only a
`load_*` YAML reader**. None wants the ADK plugin, because ADK runs in the parent process and a tool
server has no agent to police.

The cost of that mismatch was not theoretical. `import google.adk` measured **1.96s of a 2.04s**
import for `orbit.mcp_servers.memory_tools`, and six MCP servers paid it *in parallel* on every
single task, contending for the CPU as they went. Connecting the headless toolset measured **9.44s**
under load. After the split it measures **1.29s**, and is now flat in the number of servers — which
is why the plan to drop the `communication` toolset for speed was abandoned: it would buy 0.15s and
cost a capability.

**Keep `policy.py` free of `google.adk`.** If something there starts needing it, it belongs in
`safety_plugin.py`. `classify_failure`, `_CAP_OVERRIDE` and `_KNOWN_ERROR_KINDS` stayed in
`policy.py` deliberately — they are ADK-free and `safety_plugin.py` imports them back.

## SafetyPlugin's four callbacks (`safety_plugin.py`)

Three route failures; the fourth (`before_model_callback`) is not about failure at all — it is the
context-cost hook, described in its own section below. Which failures land in which of the other
three is the thing people get wrong:

- **`before_tool_callback`** — cancellation check, then the registry hard block, then the tier
  check. The registry check runs *ahead of and separately from* tier logic: an uncatalogued tool
  reaching the model is a bigger problem than a catalogued one at the wrong tier. `high` tier is
  always blocked with `confirmation_required`, never auto-approved, because no confirm channel is
  wired anywhere yet. Do not change that default before one exists.
- **`after_tool_callback`** — where MCP tool failures *actually* arrive. The server catches its own
  error inside `BaseTool.execute` and returns a normal, successful JSON-RPC response carrying
  `{"error": kind, "message": ...}` as data with `isError` false. From ADK's point of view the call
  succeeded. Confirmed empirically: two live `permission_denied` `browser_navigate` calls produced
  zero `on_tool_error_callback` invocations. `_extract_structured_failure` is the only place in the
  system that sees these.
- **`on_tool_error_callback`** — transport/protocol only: server subprocess crash, stdio timeout,
  malformed MCP response. A real Python exception that propagated up through ADK itself.

Both failure paths share one counter and one cap decision via `_record_failure`, so a tool that
fails once through each path counts as two consecutive failures, not one forgiven per path.

`_extract_structured_failure` matches deliberately narrowly — only a dict with *exactly*
`{"error", "message"}` and a recognized `ErrorKind`. A false negative just means a failure does not
trip the cap; a false positive would trip the cap on legitimate results, which is worse.

**Retry caps.** Default 2. `permission_denied`, `reasoning_failure` and `cancelled` are capped at 1,
because a second identical attempt is pointless by definition: a deterministic policy block will
refuse identically, "re-plan don't re-execute" means an identical retry ignored the guidance, and a
cancelled task has nothing to retry into.

**`classify_failure()` is for the transport path only.** Never widen it to re-guess the kind of a
structured server failure — that information already crossed the process boundary as data.
Its keyword heuristic is ordered specific-before-generic: state_failure keywords are checked before
the transport bucket, and 5xx requires a precise `\b5\d{2}\b` match. It used to contain a bare `"5"`
checked first, so "element 5 not found" classified as `tool_failure` (blind retry) instead of
`state_failure` (re-observe). Between HTTP codes, element indexes and timestamps that was a large
share of real errors in the wrong bucket.

## Context cost: `before_model_callback` and the cache breakpoint

Two mechanisms, in two different files, both aimed at the same fact: **an LLM API is stateless.**
Every turn re-sends the system instruction, every tool schema, and the entire conversation. A large
tool result is therefore not billed once — it is billed again on every remaining turn of the task.
Measured from this build's own event log: `browser_snapshot` averaged 76,811 characters per call
with a worst case of 621,442 (~155,000 tokens), and accounted for 12.0M of 13.8M total result
characters ever produced.

**`SafetyPlugin.before_model_callback` compacts history.** It replaces `function_response` payloads
that are both stale (older than `keep_full_results`, default 3) and large (over
`stale_result_char_limit`, default 2000) with an explicit note naming the tool and the size. It
touches *only* function responses — never user text, never model reasoning, never tool **arguments**
(small, and the record of what was already tried, so dropping them invites repetition). The note is
deliberate: a model that can see something was elided re-calls the tool, whereas one shown an
unexplained gap confabulates over it. It never raises — a context optimisation must not fail a task.

**`orbit/agent.py`'s `_CachingLiteLLMClient` marks a prompt-cache breakpoint.** Gemini honours only
the last breakpoint, so one marker on the final message caches everything before it. Read that
function's docstring before touching it: three more obvious routes (ADK's `LlmRequest.cache_config`,
injecting from `before_model_callback`, and litellm's `cache_control_injection_points` kwarg) are all
**silently inert** on the installed versions — the kwarg does not exist in litellm 1.96.0 at all.
The one working route was confirmed by intercepting the outgoing HTTP body, not from docs.

**They do not stack cleanly, and that is expected.** Caching pays only on a byte-identical prefix,
and eliding a result rewrites the middle of the history. Turns where a new elision happens take a
cache miss; the rest hit. The trade still favours compaction — dropping a 155,000-token payload beats
paying 0.25x to keep re-sending it. Compaction runs first (ADK-side), caching marks the compacted
result, which is the correct order.

Payload caps live at the MCP edge rather than here — `url_policy.yaml`'s `max_content_chars` for
browser content, and `perception_server.py`'s node pruning and screenshot-to-disk. See
`orbit/mcp_servers/CLAUDE.md`. `tests/test_llm_cost.py` pins all of it, because every one of these
fails silently: the task still succeeds and the only symptom is a larger bill.

**The turn loop is where the cost is, and a conversation makes it longer.** A one-off task gets a
fresh `InMemoryRunner` and session; a conversation reuses one runner across its turns (see
"Conversations reuse their runner" below), so its session — every earlier turn's tool calls and
results — keeps growing. The two mechanisms above are what bound that: compaction elides stale large
results, and the cache breakpoint keeps the unchanged prefix cheap. Nothing bounds a session beyond
them; a very long conversation still re-sends everything that has not been compacted.

## The adhoc- task row and the foreign key that forces it

`db.get_connection()` sets `PRAGMA foreign_keys=ON` on **every** connection, and `events.task_id`
references `tasks(task_id)`. So `log_event` against a task_id with no `tasks` row raises
`IntegrityError` — it does not silently insert an orphan. That is why `SafetyPlugin._task_id`
materializes a real `adhoc-<session.id>` task row when `tool_context.state` carries no
`orbit_task_id`, rather than assuming one exists. Found by running the Prompt 0 test suite, not by
inspection. The MCP servers carry their own variants of this fallback for the same reason.

## Conversations reuse their runner — and therefore their MCP servers

`run_task` caches one `InMemoryRunner` per `conversation_id` (`_RUNNER_CACHE`). Turn two of a
conversation continues turn one's actual ADK session: the same tool calls, results and browser
session, not the text summary `_build_conversation_context` produces.

Measured 2026-09-08 in a `--serve` worker: turn one 13.2s, turns two and three 4.5s and 4.9s. The
speedup is a side effect — the six MCP servers are no longer respawned per turn — and it is the
reason the separately-planned "make MCP servers persistent" work was never needed as its own change.

**Two properties make it safe, and losing either breaks it:**

1. **Tasks in one worker are strictly sequential.** `_serve` awaits each goal before reading the
   next line. The headless lane's `Semaphore(5)` permits concurrency in principle, so a caller
   running conversation turns concurrently would share one browser session between them — the
   "browser is already in use" collision the eval harness found once already. Do not make this cache
   concurrent without solving that.
2. **Every tool call carries its true task_id.** `SafetyPlugin.before_tool_callback` injects it into
   `tool_args`, using the `task_id: str = ""` parameter every MCP tool already declares. Without
   that, a long-lived server would keep the `ORBIT_TASK_ID` it was spawned with and file every
   later turn's events under the first task.

**The text summary is a bridge, not a duplicate.** It is injected *only* when no cached runner
exists — the first turn after a worker restart, where the DB remembers the conversation but the
session is gone. When a session exists, injecting a paraphrase on top would show the model every
turn twice.

`close_conversation()` releases a conversation's runner and its subprocesses; the GUI sends
`{"close_conversation": ...}` when the user starts a new chat, the REPL calls it on `/new`, and
`close_all_conversations()` runs when the worker's stdin closes. A one-off task with no
conversation_id is untouched: fresh runner, closed in `finally`, exactly as before.

## `run_task.py` gotchas

- **The run loop is `runner.run_async`, not `run_debug`, and that is load-bearing.** `run_debug` is
  ADK's own debug helper — its docstring says to use `run_async` in production — and it collects
  every event into a list it returns only when the task is over. Under it a task was silent from
  submission to completion, which is why the GUI's step rail could not fill in until there was
  nothing left to watch. Each interesting event is now handed to `run_task`'s `on_event` sink as ADK
  yields it; see the Progress events block at the top of the module for the event shapes and for who
  supplies which sink. `StreamingMode.SSE` additionally splits the answer into incremental deltas —
  it does **not** speed up the first token (measured 2.23s SSE vs 2.24s NONE, MCP connect excluded).
- **The `result` event is emitted before the `finally`.** `runner.close()` measured 2-3s tearing down
  six MCP subprocesses, and the user has no reason to wait through it with a locked input once the
  answer exists. Do not "tidy" that emit into the exit path.
- The top-level catch is narrowed by failure class: `CancelledError` → CANCELLED, connection/OS
  errors → `graceful_degradation_message()` ("the model provider call failed"), everything else →
  its own type name. An earlier revision routed *everything* to the provider-outage message, so a
  tool bug and a DB error both surfaced as a provider problem; that is fixed, and the fix is worth
  keeping — the message is only honest if the catch is specific.
- `await runner.close()` in the `finally` is required for a task with **no conversation_id** — each
  such run spawns its own Playwright MCP subprocess holding a Chrome profile lock, and a leak
  collides with the next task ("browser is already in use"). Found by the eval harness. A
  conversation's runner deliberately survives instead (see above); the same collision cannot happen
  because the next turn reuses that exact subprocess rather than spawning a rival.
- It sets `os.environ["ORBIT_TASK_ID"]` too, but that is belt-and-suspenders — see
  `orbit/skills/CLAUDE.md` for the mechanism that actually delivers it.
- stdout/stderr are reconfigured to UTF-8 because the Windows console codepage renders model output
  (em-dashes, curly quotes, accented names) as mojibake.

## `agent.py`

One `LlmAgent` carrying three toolsets always (ResearchProduct, Memory, Filesystem) plus a fourth
(WindowsControl) only when `lane="foreground"` — deliberately *not* Section 3's
Parallel/Sequential/Loop/Coordinator composition, which is still the right target once there are
skills complex enough to warrant separate prompts.

**`build_agent`'s `lane` parameter is the actual enforcement point for the foreground-lock invariant
above**, not `TaskManager`. `TaskManager.submit()` only serializes whatever lane a caller already
chose — it has no way to stop a headless task from carrying tools that simulate real input.
`build_agent(lane="headless")` (the default) never adds `orbit.skills.windows_control`'s toolset or
its instruction block at all, so a headless-lane agent has no function declaration for
`windows_click` etc. to call, full stop — the same "hard block ahead of tier logic, not a soft
default" philosophy `risk_tiers.yaml`'s tool registry already uses, applied one layer earlier (tool
*visibility*, not just tool *permission*). `run_task()` threads its own `lane` argument straight into
this; `run_task.py`'s CLI exposes it as `--foreground`, off by default so existing invocations are
unaffected.

**Reasoning is the thing to watch when changing `DEFAULT_MODEL`.** A model that thinks before it
speaks pays that cost on *every turn of the agent loop*, and the loop is where all the turns are.
`gemini-3.7-flash` was the default until 2026-09-07 at roughly 3s per turn — 225 reasoning tokens
spent on "count from 1 to 20" — and OpenRouter refuses to disable it for that endpoint at all
(`reasoning: {max_tokens: 0}` → "Reasoning is mandatory for this endpoint"). The default is now
`gemini-2.5-flash`. An older revision of this file documented a `_MODEL_EXTRA_BODY` carrying
Nemotron's `chat_template_kwargs.enable_thinking=False` for the same class of problem; that constant
no longer exists anywhere in the codebase.

Every model in `KNOWN_MODELS` was checked to support tool calling before being listed; one that does
not cannot drive this agent at all.

`select_model()` fails loudly with the exact `.env` line to add when a provider's key is missing.
Note the first LiteLLM call takes ~18s (warm-up) and subsequent ones ~1s — not a hang.

## `db.py`

- FTS5 over `tasks(title, goal, result)` is an **external-content** table kept in sync by three
  triggers (`tasks_ai`/`tasks_ad`/`tasks_au`), not rebuilt per query. Changing the `tasks` schema
  means revisiting all three.
- `_fts_query` quotes every token and joins with OR, because raw natural-language input contains
  FTS5 operators (parens, colons) that otherwise raise a syntax error.
- `search_task_history` (over `memory`) is a plain `LIKE '%query%'` substring match, **not**
  tokenized — a caller searching a phrase the row does not literally contain gets nothing back.
  This bites test fixtures and anything that assumes it behaves like the FTS path.
- `get_daily_cost` sums `events.cost_usd` for a tool_call within a day, meant to be checked *before*
  spending rather than after so a cap actually stops spend. It has **no callers anywhere in this
  codebase** — its one caller was the old voice runtime's daily transcription-cost cap, removed with
  that code. **Voice spend is now capped elsewhere** —
  `gui/spend.py` guards both directions (Aura characters, Nova-3 seconds) in its own file, because the
  GUI never writes event rows. So this function still has no callers, and is still the right home
  for a cap on a *tool's* spend.
- Event logging is duplicated across `safety_plugin.py` and `orbit/tools/foundation.py` (Fix 7 pending);
  `tests/CLAUDE.md` has the rule that follows from it.

## `pending_confirmations` — the approval channel

Added for the vision-tier confirmation flow. It exists because the vision tier can **see** a control
it is not allowed to **click**: a `Confidence.VISION_INFERRED` (0.50) ElementRef sits below
`min_actuation_confidence` (0.70), so actuation refuses it. That refusal is correct and this table
does not relax it. What was missing was a *channel* — no way for a human to look at a guess and say
yes — so the only available answer was "no", forever.

**`approval_token` is a capability, not a record**, and every constraint on it is load-bearing:

- minted **only** on approval, never on rejection — a refusal leaves nothing to leak or replay;
- **short-lived** (`token_expires_at`): an approval is consent about a screenshot of a *moment*, and
  replaying it later aims a click at a screen that has since changed;
- **single-use** (`token_consumed_at`), kept as its own column rather than folded into `status`
  because "what the human decided" and "was the capability spent" are different facts;
- **bound to one row**, therefore to one proposed action, so one yes can never authorise a second
  click.

`resolve_pending_confirmation` updates **conditionally on the row still being PENDING** and checks
`rowcount`. That is what stops a REJECTED confirmation from later being flipped to APPROVED — a plain
UPDATE would do exactly that and mint a token for an action a human refused. It raises `KeyError`
rather than returning None on a missing or already-decided row: both must stay distinguishable from
"approved, here is your token".

`consume_approval_token` returns `None` for *every* failure — unknown, spent, expired, never
approved. The caller is deliberately not told which, because the answer to "may I act" is identical
in all four cases and distinguishing them turns the function into an oracle for probing which tokens
exist. Expiry is enforced inside it, never trusted from a caller-supplied clock.

`_ensure_confirmation_task` materializes the `adhoc-confirmation` task **only when no task_id was
given at all**. An explicitly-supplied unknown id is passed straight through so the foreign key
fails loudly — inventing a row for a caller that passed the wrong id would file the approval under a
task nobody is watching.

`screenshot_path` stores a **path, not the image**. Base64 PNGs of every confirmation would grow this
DB without bound, and the GUI can read a file.

**The channel is fully wired now** — an earlier revision of this file said nothing wrote to this
table, which is no longer true. `orbit/confirmation.py` is the writer:
`request_confirmation()` records the row **before** asking anyone, then asks either the console
(`y/N`) or the GUI approvals drawer, and resolves the row either way. That ordering is what makes
the audit trail complete — a refusal leaves a REJECTED row, an unattended run leaves a REJECTED row,
and a crash mid-prompt leaves a PENDING row a human can still see, rather than all three being
indistinguishable from "never happened".

The consumer is `windows_control_tools._require_confidence`, which validates the token **inside the
tool process** via `db.consume_approval_token` rather than trusting whatever the caller passed. So
the round trip a vision-guessed click now takes is: below-floor element → `request_confirmation` →
human yes → single-use token → `_require_confidence` spends it → the one action runs. The floor and
the element's confidence are untouched throughout; see the root `CLAUDE.md`'s vision section, which
is the canonical description of that path.

`_console_is_interactive()` decides which channel is used. With no TTY it waits on the GUI for
`approval_gui_wait_seconds` — 0 when the key is absent (fail closed immediately), 30 in the shipped
YAML, after which an unanswered request is refused. `prompt` is injectable so
`tests/test_confirmation_flow.py` can drive decisions without a TTY.

`CREATE TABLE IF NOT EXISTS` in `_SCHEMA` is the entire migration story, which works because
`init_db()` runs at every entry point (`run_task`, every MCP server, the GUI, conftest).
