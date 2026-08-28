# orbit/ — core runtime

`agent.py` builds it, `run_task.py` drives it, `task_manager.py` schedules it, `policy.py` polices
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

## SafetyPlugin's four callbacks (`policy.py`)

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

**Sessions are already isolated per task.** `run_task()` builds a fresh `InMemoryRunner` and a new
session keyed by `task_id` for every goal, including each line typed at the REPL — so conversation
history never carries from one task into the next. Adding "chat sessions" would not reduce tokens;
the cost is entirely *within* a single task's turn loop, which is what the two mechanisms above
address.

## The adhoc- task row and the foreign key that forces it

`db.get_connection()` sets `PRAGMA foreign_keys=ON` on **every** connection, and `events.task_id`
references `tasks(task_id)`. So `log_event` against a task_id with no `tasks` row raises
`IntegrityError` — it does not silently insert an orphan. That is why `SafetyPlugin._task_id`
materializes a real `adhoc-<session.id>` task row when `tool_context.state` carries no
`orbit_task_id`, rather than assuming one exists. Found by running the Prompt 0 test suite, not by
inspection. The MCP servers carry their own variants of this fallback for the same reason.

## `run_task.py` gotchas

- **The top-level `except Exception` reports every failure as a provider outage.** It routes
  everything to `graceful_degradation_message()`, which says "I can't think right now — the model
  provider call failed". A bug in a tool, a DB error, or a cancelled coroutine all surface to the
  user as a provider problem. Narrow the catch before trusting that message.
- `await runner.close()` in the `finally` is required, not tidiness. Each run spawns its own
  Playwright MCP subprocess holding a Chrome profile lock; a leak collides with the next task
  ("browser is already in use"). Found by the eval harness.
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

Nemotron needs `chat_template_kwargs.enable_thinking=False` (`_MODEL_EXTRA_BODY`). Left on, it
interleaves raw chain-of-thought into returned content — which leaked verbatim into a user-facing
answer during testing — and spends the `max_tokens` budget on thinking instead of the reply. Tool
calling stays clean with it off. Every model in `KNOWN_MODELS` was checked to support tool calling
before being listed; one that does not cannot drive this agent at all.

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
  codebase** — its one caller was the voice runtime's daily transcription-cost cap, removed along
  with all voice code. Left in `db.py` as generic infrastructure (nothing about its implementation
  is voice-specific); the next cost-capped tool_call is what would call it next, not a rebuild.
- Event logging is duplicated across `policy.py` and `orbit/tools/foundation.py` (Fix 7 pending);
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

**Nothing writes to this table yet** — Phase 4 (REPL confirm) and Phase 5 (GUI approve/reject) are
what call it. `CREATE TABLE IF NOT EXISTS` in `_SCHEMA` is the entire migration story, which works
because `init_db()` runs at every entry point (`run_task`, every MCP server, the GUI, conftest).
