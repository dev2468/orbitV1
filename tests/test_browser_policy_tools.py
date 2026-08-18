"""Tests adapted from Prompt 4's required list (adjusted for this pass's
scope, which excludes browser_click/browser_type — see the module
docstring in browser_policy_tools.py):

- requesting a family member's profile context is refused
- a javascript: URL is refused
- a file:// URL is refused
- navigating to a blocklisted domain is refused (fail-closed: no confirm
  channel exists yet, matching the rest of this build's safety default)
- a real end-to-end open/navigate/close round trip against a live page
- session teardown is structural: reaped on task-terminal and on idle,
  with browser_close not offered to the model at all

The prompt-injection and mid-action-navigation tests from Prompt 4/8
belong at the whole-system level (a real agent driving these tools) and
are out of scope for unit tests against the proxy in isolation.

NOTE on the events table: assert on row CONTENT (which tool, which args,
which error), never on row counts. One logical tool call currently writes
three rows from three sites, and consolidating that is scheduled as Fix 7 —
count-based assertions would silently break when it lands.
"""

import asyncio

import pytest
import pytest_asyncio

import orbit.mcp_servers.browser_policy_tools as bp
from orbit import db
from orbit.mcp_servers.browser_policy_tools import (
    SESSIONS,
    aclose_all_sessions,
    close_session_tool,
    navigate_tool,
    open_session_tool,
)
from orbit.policy import load_chrome_profiles
from orbit.skills import research_product


@pytest_asyncio.fixture(autouse=True)
async def _close_leaked_sessions():
    # Guaranteed cleanup even when a test fails an assertion before
    # reaching its own close call — found the hard way: a failed
    # assertion left a session's subprocess/AsyncExitStack dangling, and
    # its later async-generator finalization (in a different task/loop
    # context) crashed with an anyio cancel-scope error during teardown.
    yield
    await aclose_all_sessions()


def test_non_owner_profiles_structurally_excluded():
    profiles = load_chrome_profiles()
    eligible = {n: c for n, c in profiles.items() if not c.get("owner_confirmation_required")}
    for name in ("mom", "dad", "sister"):
        assert name not in eligible


@pytest.mark.asyncio
async def test_requesting_family_context_never_resolves():
    caller = db.create_task("caller")
    result = await open_session_tool.execute({"context": "mom"}, task_id=caller)
    assert result.ok is False
    assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_javascript_url_refused():
    caller = db.create_task("caller")
    result = await navigate_tool.execute(
        {"session_id": "nonexistent", "url": "javascript:alert(1)"}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_file_url_refused():
    caller = db.create_task("caller")
    result = await navigate_tool.execute(
        {"session_id": "nonexistent", "url": "file:///etc/passwd"}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_blocklisted_domain_refused():
    caller = db.create_task("caller")
    result = await navigate_tool.execute(
        {"session_id": "nonexistent", "url": "https://mybank.com/login"}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "permission_denied"


def test_browser_close_is_not_offered_to_the_model():
    """Teardown must not depend on the model: in measured runs it skipped
    browser_close 2 times out of 4. It stays implemented and callable
    internally, but is not in the surface the model can see."""
    toolset = research_product.build_toolset(task_id="t-filter")
    assert "browser_close" not in toolset.tool_filter
    assert "browser_extract" not in toolset.tool_filter
    assert "browser_open" in toolset.tool_filter


@pytest.mark.asyncio
async def test_reaper_closes_session_when_task_reaches_terminal_state():
    """Covers the failure path specifically — a task that fails still lands
    in FAILED, so its sessions must still be reaped."""
    task_id = db.create_task("reaper terminal")
    result = await open_session_tool.execute(
        {"context": "research", "owner_task_id": task_id}, task_id=task_id
    )
    assert result.ok, result.error
    session_id = result.data["session_id"]

    # Still running -> must NOT be reaped (no premature teardown).
    assert await bp._reap_once() == []
    assert session_id in SESSIONS

    db.update_task_status(task_id, "FAILED", failure_reason="simulated")
    assert (session_id, "task_terminal") in await bp._reap_once()
    assert session_id not in SESSIONS


@pytest.mark.asyncio
async def test_reaper_closes_idle_session_mid_task(monkeypatch):
    """The idle path is the only thing that reaps a session abandoned while
    its task is still running — shutdown handlers cannot, the process is
    still alive."""
    monkeypatch.setattr(bp, "SESSION_IDLE_TIMEOUT_S", 0.5)
    task_id = db.create_task("reaper idle")
    result = await open_session_tool.execute(
        {"context": "research", "owner_task_id": task_id}, task_id=task_id
    )
    assert result.ok, result.error
    session_id = result.data["session_id"]

    assert db.get_task(task_id)["status"] not in db.TERMINAL_STATUSES
    await asyncio.sleep(0.7)
    assert (session_id, "idle") in await bp._reap_once()
    assert session_id not in SESSIONS


@pytest.mark.asyncio
async def test_blocked_navigation_is_recorded_by_content_not_count():
    """Asserts on row CONTENT (tool name + error text), never row counts —
    three write sites currently log each call and that is scheduled to
    change (Fix 7). A count-based assertion would break on that fix."""
    task_id = db.create_task("blocklist audit")
    await navigate_tool.execute(
        {"session_id": "nonexistent", "url": "https://mybank.com/login"}, task_id=task_id
    )
    with db.get_connection() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT tool_call, error FROM events WHERE task_id = ?", (task_id,)
            )
        ]
    assert any(
        r["tool_call"] == "browser_navigate"
        and r["error"]
        and "permission_denied" in r["error"]
        for r in rows
    ), rows


@pytest.mark.asyncio
async def test_open_navigate_close_round_trip():
    caller = db.create_task("caller")
    try:
        open_result = await open_session_tool.execute({"context": "personal"}, task_id=caller)
        assert open_result.ok, open_result.error
        session_id = open_result.data["session_id"]
        assert open_result.data["profile"] == "dev"

        nav_result = await navigate_tool.execute(
            {"session_id": session_id, "url": "https://example.com"}, task_id=caller
        )
        assert nav_result.ok, nav_result.error
        assert nav_result.data["title"] == "Example Domain"
        assert "example.com" in nav_result.data["final_url"]

        close_result = await close_session_tool.execute({"session_id": session_id}, task_id=caller)
        assert close_result.ok
        assert session_id not in SESSIONS
    finally:
        await aclose_all_sessions()
