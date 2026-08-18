"""classify_failure() — Section 7 failure routing.

Covers the exact confusions from the bug report: a bare "5" in the keyword
list matched any digit 5 anywhere in the message (element indexes, HTTP
4xx codes, timestamps), and it was checked before the state_failure
keywords, so "element 5 not found" classified as tool_failure (blind
retry) instead of state_failure (re-observe before acting again).

Also covers Fix 8: the retry cap never actually tripped on the live path,
because BaseTool.execute translates every server-side tool failure into a
normal (non-exception) JSON response, so on_tool_error_callback never
fires for it and after_tool_callback used to unconditionally clear the
counter regardless of whether the result was failure-shaped. Confirmed
empirically before fixing (see the Fix 8 investigation): two live
permission_denied browser_navigate calls produced zero on_tool_error_callback
invocations and left _consecutive_failures at {} throughout.
"""

import json
import logging

import pytest

from orbit import db
from orbit.policy import SafetyPlugin, classify_failure, load_tool_registry
from orbit.tools.foundation import ClassifiedToolError


def test_element_index_five_is_not_mistaken_for_a_5xx_status():
    # THE exact confusion from the bug report: a bare digit "5" (from the
    # element index) used to win over "not found" because it was checked
    # first. Must be state_failure now.
    assert classify_failure(Exception("element 5 not found")) == "state_failure"


def test_5xx_status_code_is_tool_failure():
    assert classify_failure(Exception("HTTP 503 Service Unavailable")) == "tool_failure"


def test_connection_reset_is_tool_failure():
    assert classify_failure(Exception("connection reset")) == "tool_failure"


def test_element_detached_after_navigation_is_state_failure():
    assert (
        classify_failure(Exception("page navigated, element detached"))
        == "state_failure"
    )


def test_state_failure_keywords_win_over_incidental_digits():
    # A message that could plausibly contain both an unrelated digit and a
    # state-failure keyword must still resolve to state_failure — proves
    # the reordering (specific before generic), not just the regex fix.
    assert classify_failure(Exception("element 5 is stale, page changed")) == "state_failure"


def test_rate_limit_and_429_still_classify_as_tool_failure():
    # Regression guard: narrowing the "5" match must not have collaterally
    # broken the other transport keywords still in that bucket.
    assert classify_failure(Exception("429 Too Many Requests")) == "tool_failure"
    assert classify_failure(Exception("rate limit exceeded")) == "tool_failure"
    assert classify_failure(Exception("connection timeout")) == "tool_failure"


def test_four_digit_and_two_digit_numbers_do_not_false_positive_as_5xx():
    # \b5\d{2}\b must not match e.g. "45" or "5000" as a 5xx code.
    assert classify_failure(Exception("waited 45 seconds, nothing happened")) == "reasoning_failure"


def test_unclassifiable_message_falls_through_to_reasoning_failure():
    assert classify_failure(Exception("the plan assumed a login form that isn't there")) == "reasoning_failure"


def test_classified_tool_error_kind_is_used_directly_not_reguessed():
    # The precedence case: ClassifiedToolError already knows its own kind
    # and must never be re-guessed from its message string.
    err = ClassifiedToolError("permission_denied", "blocked: URL matches bank blocklist")
    assert classify_failure(err) == "permission_denied"


def test_classified_tool_error_precedence_beats_a_deceptive_message():
    # Adversarial case: the message text itself contains state_failure
    # keywords, but the exception's own declared kind must still win —
    # proves this is real precedence, not just "works when they happen to
    # agree."
    err = ClassifiedToolError("timeout", "element not found: page did not settle in time")
    assert classify_failure(err) == "timeout"


def test_classified_tool_error_all_kinds_pass_through_unmodified():
    for kind in (
        "tool_failure",
        "state_failure",
        "reasoning_failure",
        "permission_denied",
        "cancelled",
        "timeout",
    ):
        assert classify_failure(ClassifiedToolError(kind, "message")) == kind


# ---------------------------------------------------------------------------
# SafetyPlugin: failure DETECTION on the real MCP wire shape, and the retry
# cap actually tripping. This is Fix 8.
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name: str):
        self.name = name


class _FakeSession:
    id = "fake-session"


class _FakeContext:
    def __init__(self, task_id: str):
        self.state = {"orbit_task_id": task_id}
        self.session = _FakeSession()


def _mcp_failure(kind: str, message: str) -> dict:
    """The real wire shape confirmed live: BaseTool.execute's failure
    payload, JSON-serialized into the MCP text content block, with
    isError left False — the whole reason on_tool_error_callback doesn't
    see these."""
    return {
        "content": [{"type": "text", "text": json.dumps({"error": kind, "message": message})}],
        "isError": False,
    }


def _mcp_success(data) -> dict:
    """Confirmed live: a str return value goes into content[0]['text']
    verbatim (browser_snapshot); anything else is JSON-serialized first
    (browser_navigate's {"ok":...} dict)."""
    text = data if isinstance(data, str) else json.dumps(data)
    return {"content": [{"type": "text", "text": text}], "isError": False}


class TestExtractStructuredFailure:
    def test_detects_a_real_server_side_failure(self):
        result = _mcp_failure("permission_denied", "blocked")
        assert SafetyPlugin._extract_structured_failure(result) == ("permission_denied", "blocked")

    def test_success_dict_is_not_a_failure(self):
        result = _mcp_success({"ok": True, "final_url": "https://example.com/", "title": "Example Domain"})
        assert SafetyPlugin._extract_structured_failure(result) is None

    def test_string_snapshot_payload_is_not_a_failure(self):
        # The exact shape that must not false-positive: a raw string
        # success payload (browser_snapshot's wrapped page text) is not
        # valid JSON, so json.loads must fail cleanly, not crash, and this
        # must resolve to "not a failure".
        result = _mcp_success("<untrusted_web_content>...page text...</untrusted_web_content>")
        assert SafetyPlugin._extract_structured_failure(result) is None

    def test_unrecognized_error_kind_is_not_treated_as_a_failure(self):
        # Defensive: a dict that coincidentally has an "error" key but an
        # unrecognized value must not be misread as our failure shape.
        result = _mcp_success({"error": "not_a_real_kind", "message": "x"})
        assert SafetyPlugin._extract_structured_failure(result) is None

    def test_extra_keys_disqualify_the_match(self):
        # The match is deliberately narrow: exactly {"error","message"}.
        result = _mcp_success({"error": "tool_failure", "message": "x", "extra": "field"})
        assert SafetyPlugin._extract_structured_failure(result) is None

    def test_is_error_true_is_treated_as_generic_tool_failure(self):
        result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
        assert SafetyPlugin._extract_structured_failure(result) == ("tool_failure", "boom")

    def test_non_dict_result_is_not_a_failure(self):
        assert SafetyPlugin._extract_structured_failure("not a dict") is None
        assert SafetyPlugin._extract_structured_failure(None) is None


@pytest.mark.asyncio
async def test_permission_denied_trips_cap_on_the_first_failure():
    # cap=1 for permission_denied: a second identical attempt at a
    # deterministic policy block is guaranteed waste.
    task_id = db.create_task("t")
    plugin = SafetyPlugin()
    ctx = _FakeContext(task_id)
    tool = _FakeTool("browser_navigate")

    outcome = await plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=ctx,
        result=_mcp_failure("permission_denied", "blocklisted"),
    )
    assert outcome is not None
    assert outcome["error"] == "retry_cap_exceeded"
    assert outcome["classification"] == "permission_denied"


@pytest.mark.asyncio
async def test_tool_failure_does_not_trip_on_first_but_trips_on_second():
    # cap=2 (default) for tool_failure: transient, worth one retry.
    task_id = db.create_task("t")
    plugin = SafetyPlugin()
    ctx = _FakeContext(task_id)
    tool = _FakeTool("memory_write")
    fail = _mcp_failure("tool_failure", "boom")

    first = await plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=ctx, result=fail)
    assert first is None  # under the cap

    second = await plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=ctx, result=fail)
    assert second is not None
    assert second["classification"] == "tool_failure"


@pytest.mark.asyncio
async def test_a_genuine_success_between_failures_resets_the_streak():
    # This is the core of the bug: a genuine success (not a failure-shaped
    # result surviving as JSON) must still clear the counter — that part
    # was never wrong, only the "always clear, even on failure" part was.
    task_id = db.create_task("t")
    plugin = SafetyPlugin()
    ctx = _FakeContext(task_id)
    tool = _FakeTool("memory_write")
    fail = _mcp_failure("tool_failure", "boom")
    ok = _mcp_success({"memory_id": 1})

    await plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=ctx, result=fail)
    await plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=ctx, result=ok)
    third = await plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=ctx, result=fail)

    assert third is None  # streak reset by the success; this is only attempt 1 again


@pytest.mark.asyncio
async def test_transport_and_structured_failures_share_one_counter():
    # A tool failing once via each detection path (a real exception via
    # on_tool_error_callback, then a server-returned failure via
    # after_tool_callback) must count as two consecutive failures of that
    # tool, not one silently forgiven per path.
    task_id = db.create_task("t")
    plugin = SafetyPlugin()
    ctx = _FakeContext(task_id)
    tool = _FakeTool("browser_navigate")

    first = await plugin.on_tool_error_callback(
        tool=tool, tool_args={}, tool_context=ctx, error=Exception("connection reset")
    )
    assert first is None  # tool_failure via transport path, attempt 1 of cap=2

    second = await plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=ctx, result=_mcp_failure("tool_failure", "boom again"),
    )
    assert second is not None  # attempt 2 — cap hit, shared counter proven
    assert second["classification"] == "tool_failure"


@pytest.mark.asyncio
async def test_recorded_classification_matches_server_kind_not_a_reguessed_one():
    # The whole point of Fix 8's point 2: read the structured kind the
    # server already computed, don't re-run string matching on its
    # message. Use a message that would classify DIFFERENTLY under
    # classify_failure's keyword heuristic (contains "not found", which
    # its matcher calls state_failure) but the server explicitly tagged
    # permission_denied — the returned/recorded classification must be
    # permission_denied, proving it was read, not re-guessed.
    task_id = db.create_task("t")
    plugin = SafetyPlugin()
    ctx = _FakeContext(task_id)
    tool = _FakeTool("browser_navigate")
    deceptive_message = "resource not found on the blocklist server"

    outcome = await plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=ctx,
        result=_mcp_failure("permission_denied", deceptive_message),
    )
    assert outcome["classification"] == "permission_denied"

    with db.get_connection() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT result FROM events WHERE task_id = ? AND tool_call = ?",
                (task_id, "browser_navigate"),
            )
        ]
    assert any(r["result"] and "permission_denied" in r["result"] for r in rows)
    assert not any(r["result"] and "state_failure" in r["result"] for r in rows)


# ---------------------------------------------------------------------------
# Tool registry hard block — the "orphaned policy layer" fix. A tool name
# not on the approved registry (risk_tiers.yaml's low/medium/high/allowed)
# used to fall through to a soft default (tier='medium', still executed).
# That soft default is exactly what let research_product.py's raw-Playwright
# bug reach the model unnoticed: the raw server's tools shared this file's
# own names, so the tier lookup silently "matched" tools this policy layer
# had never actually reviewed. Registered/blocked is now a hard gate,
# checked before tier logic runs at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unregistered_tool_is_hard_blocked(caplog):
    task_id = db.create_task("t")
    plugin = SafetyPlugin(tool_registry={"browser_navigate"})  # deliberately small
    ctx = _FakeContext(task_id)
    tool = _FakeTool("totally_made_up_tool")

    with caplog.at_level(logging.WARNING, logger="orbit.policy"):
        outcome = await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)

    assert outcome is not None
    assert outcome["error"] == "tool_not_registered"
    assert "totally_made_up_tool" in outcome["message"]

    # "Log every block at WARNING with the tool name" — assert the log
    # record exists and names the tool, not just that *some* warning fired.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("totally_made_up_tool" in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_registered_tool_is_not_blocked_by_the_registry_check():
    task_id = db.create_task("t")
    plugin = SafetyPlugin(tool_registry={"browser_navigate"}, risk_tiers={"browser_navigate": "low"})
    ctx = _FakeContext(task_id)
    tool = _FakeTool("browser_navigate")

    outcome = await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)
    assert outcome is None  # allowed through


def test_every_tool_the_agent_exposes_at_build_time_is_registered():
    """Regression test for the exact class of bug this review found: a
    tool_filter change that adds a name to what the agent can call, without
    a matching update to risk_tiers.yaml, must fail this test rather than
    silently ship. Reads the REAL tool_filter each skill declares — not a
    hardcoded list here that could itself drift out of sync."""
    from orbit.skills import communication as communication_skill
    from orbit.skills import filesystem as filesystem_skill
    from orbit.skills import memory as memory_skill
    from orbit.skills import research_product
    from orbit.skills import screen_perception as screen_perception_skill
    from orbit.skills import windows_control as windows_control_skill

    registry = load_tool_registry()
    exposed: list[str] = []
    for toolset in (
        research_product.build_toolset(task_id="probe"),
        memory_skill.build_toolset(task_id="probe"),
        filesystem_skill.build_toolset(task_id="probe"),
        windows_control_skill.build_toolset(task_id="probe"),
        communication_skill.build_toolset(task_id="probe"),
        screen_perception_skill.build_toolset(task_id="probe"),
    ):
        exposed.extend(toolset.tool_filter)

    assert exposed, "expected at least one tool_filter entry to check — got none"
    missing = [name for name in exposed if name not in registry]
    assert not missing, (
        f"tool(s) exposed to the model but missing from risk_tiers.yaml: {missing} "
        "— add them with a real tier before this can ship"
    )
