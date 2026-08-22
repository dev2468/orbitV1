# tests/ — philosophy, fixtures, and the real-DB trap

249 tests across fifteen files (248 collected into the default run + 1 opt-in). The vision work added
four: `test_grounding_bench.py` (the benchmark's deterministic half), `test_candidate_source.py`
(candidate generation), `test_set_of_mark.py` (set-of-mark grounding), and
`test_pending_confirmations.py` (the approval data model). None of the four makes a real model call —
`test_grounding_bench.py` drives the harness in `--dry-run`, and the set-of-mark tests monkeypatch
`_call_vision_model` exactly as the older vision tests do. `test_foundation.py`,
`test_policy.py`, `test_memory_tools.py`, `test_browser_policy_tools.py`, `test_filesystem_tools.py`,
`test_windows_control_tools.py`, `test_communication_tools.py`, `test_uia_resolver.py` and
`test_perception_tools.py` are unit tests of individual pieces; `test_adversarial.py` probes whether
the *system* holds under attack.

**`test_windows_control_live.py` is skipped by default** (`ORBIT_RUN_LIVE_UI_TESTS` unset) and is not
part of `pytest tests/ -q`. Unlike every other "not hermetic" test here, it doesn't just need network
or a working API key — it visibly opens a real Notepad window and drives real mouse/keyboard input
while it runs. Set `ORBIT_RUN_LIVE_UI_TESTS=1` to opt in, and don't be at the keyboard mid-run.

**`test_perception_tools.py` is different: it IS in the default run**, even though several of its
tests hit the real desktop (a real screenshot, the real foreground window). That's safe because
perception is read-only by construction — nothing it does can move the mouse, send a keystroke, or
change what's on screen, so unlike `test_windows_control_live.py` there's nothing disruptive to gate
behind an opt-in flag. The **vision-tier tests never make a real model call** — `_call_vision_model`
is monkeypatched in every one of them. What they do exercise for real is the coordinate arithmetic
and the actuation refusal, because those are the two things that can break without anyone noticing:
a crop-offset bug returns answers wrong by a small constant, and a confidence regression would open
a path from a visual guess to a real mouse click. Content-specific assertions are avoided (e.g. PNG magic bytes and response
shape, not what the screenshot shows) so these stay stable regardless of whatever happens to be in
the foreground when the suite runs.

## Assert on state, never on the model's prose

Every adversarial test asserts on the **events table**, the **tool registry**, or **DB state** —
never on what the model said. A model that narrates "I will not comply" while calling the tool
anyway must fail, and a test reading only the text would call that a pass. When you add a test here,
the question is always "which row proves it", not "did it say the right thing".

The strongest version of this is `test_agent_only_reaches_registered_policy_tools`: name checks
alone are worthless because raw Playwright exposes the same tool names as the policy proxy, so it
asserts on **behavioural fingerprints** instead — a blocklisted URL returning
`{"error": "permission_denied"}`, and the proxy's `{ok, final_url, title}` response shape, neither
of which raw Playwright has any concept of. It introspects
`browser_toolset.connection_params.server_params` off the real built object rather than
hand-copying the config, so it cannot drift out of sync with the source.

## conftest.py's `isolated_db` is autouse — and `real_project_db` deliberately opts out

`isolated_db` monkeypatches `db.DB_PATH` to a `tmp_path` file for every test. That works for
anything running in-process.

It does **not** work for anything that spawns an MCP server subprocess — i.e. any test that calls
`run_task()`. A subprocess inherits environment variables, not this process's monkeypatched module
attributes, so subprocess-side `db.log_event`/`db.create_task` always target the real
`data/orbit.db` regardless of what this process's `DB_PATH` says. With `isolated_db` active,
`run_task()` creates the task row in the tmp file and every subprocess tool call then fails with
`FOREIGN KEY constraint failed`, because that task_id does not exist in the file they are actually
writing to. Found on the first live run, not by inspection.

`real_project_db` re-patches `DB_PATH` back to the real path so both sides agree. **The consequence
is that those tests write to production data and leave rows behind** — task rows, event rows, and
seeded `provenance='external'` memory rows. There is no cleanup. Treat any surprising content in
`data/orbit.db` as test residue until proven otherwise, and know that running the suite adds more.

## Never assert on event row counts

A single logical tool call currently writes **three** event rows, from three sites:
`before_tool_callback` and `after_tool_callback` in `orbit/policy.py` (client side), and
`BaseTool._finish` in `orbit/tools/foundation.py` (inside the server process). Consolidating them is
scheduled as Fix 7. Assert on row **content** — which tool, which args, which error — so tests stay
correct on both sides of that change. A count-based assertion silently breaks when it lands.

## `_close_leaked_sessions` is autouse where browsers are involved

`test_browser_policy_tools.py` and `test_adversarial.py` each declare an autouse async fixture
calling `aclose_all_sessions()` on teardown. This is not tidiness: a test that fails an assertion
before reaching its own close call leaves a Playwright subprocess dangling, and its later
async-generator finalization — in a different task context — crashes teardown with an anyio
cancel-scope error that obscures the original failure. Any new test that opens a session needs the
same guarantee.

## Not all of this is hermetic

Several tests spawn real Chrome via `npx`, make real network calls, and run the real LLM
(`test_adversarial.py`'s two injection tests, `test_browser_policy_tools.py`'s round trip, the reaper
tests). They need network, `npx`, and a working `NVIDIA_NIM_API_KEY`, and they cost tokens. A
failure there is as likely to be environmental as a regression — check before assuming the code
broke.

`fixture_server` serves attacker-controlled pages over a local `ThreadingHTTPServer` on a random
port, because our own policy layer correctly refuses `file://` — an attack fixture has to arrive
over real `http://` to reach `browser_navigate` at all.

## Smaller traps

- **No `pytest.ini`, `pyproject.toml` or `setup.cfg` exists.** pytest-asyncio runs in strict mode,
  so every async test needs an explicit `@pytest.mark.asyncio` and every async fixture needs
  `@pytest_asyncio.fixture`. Forgetting either yields a silently skipped or erroring test, not an
  obvious failure.
- `test_no_code_path_deletes_a_memory_row` greps module **source text** for `delete from memory` and
  friends. Moving code between modules can void that guard without changing behaviour — if you
  relocate memory code, relocate the check.
- `test_high_tier_tool_is_blocked_with_confirmation_required` uses a synthetic tool name on purpose:
  nothing real is tier `high` today, so it tests the mechanism rather than depending on a specific
  tool staying high-tier.
- Tests that construct `SafetyPlugin` directly pass explicit small `risk_tiers`/`tool_registry`
  dicts to avoid depending on the live YAML. `test_every_tool_the_agent_exposes_at_build_time_is_registered`
  is the deliberate exception — it must read the real registry to do its job.
