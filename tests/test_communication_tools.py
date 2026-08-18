"""Tests for the communication MCP server (Prompt 6): draft/search/read
round trip against LocalMailBackend, untrusted-content wrapping on read,
email_send's unconditional refusal (checked at the TOOL level, independent
of SafetyPlugin's tier block — the second of the two layers
communication_tools.py's EmailSendTool docstring describes), non-owner
account_context refusal, and calendar create/list.

Mirrors test_memory_tools.py's/test_filesystem_tools.py's structure: call
the BaseTool instances directly, assert on ToolResult/backend state, never
on prose.
"""

from __future__ import annotations

import pytest

import orbit.db as db
import orbit.mcp_servers.communication_tools as comm_tools
from orbit.mcp_servers.communication_backend import LocalMailBackend


@pytest.fixture
def local_backend(tmp_path, monkeypatch):
    """Point the local backend at an isolated tmp_path db file instead of
    the real data/communication_local.db, so these tests never touch or
    leave residue in real project files."""
    db_path = tmp_path / "communication_local.db"
    monkeypatch.setattr(
        comm_tools, "get_backend", lambda name: LocalMailBackend(db_path=db_path)
    )
    return db_path


@pytest.mark.asyncio
async def test_draft_then_search_then_read_round_trip(local_backend):
    caller = db.create_task("caller")

    draft_result = await comm_tools.draft_tool.execute(
        {
            "account_context": "personal",
            "recipient": "someone@example.com",
            "subject": "quarterly numbers",
            "body": "here they are",
        },
        task_id=caller,
    )
    assert draft_result.ok
    draft_id = draft_result.data["draft_id"]

    # Send the draft directly against the BACKEND (not through the
    # blocked tool) so search/read have something real to find — this is
    # exactly the "fully implemented, unit-tested independently of the
    # blocked tool" pattern the module docstrings describe.
    backend = LocalMailBackend(db_path=local_backend)
    message_id = await backend.send(account="personal", draft_id=draft_id)

    search_result = await comm_tools.search_tool.execute(
        {"account_context": "personal", "query": "quarterly", "limit": 10}, task_id=caller
    )
    assert search_result.ok
    assert any(r["message_id"] == message_id for r in search_result.data["results"])

    read_result = await comm_tools.read_tool.execute(
        {"account_context": "personal", "message_id": message_id}, task_id=caller
    )
    assert read_result.ok
    assert read_result.data["body"].startswith("<untrusted_email_content")
    assert "here they are" in read_result.data["body"]
    assert read_result.data["body"].rstrip().endswith("</untrusted_email_content>")


@pytest.mark.asyncio
async def test_read_missing_message_is_state_failure(local_backend):
    caller = db.create_task("caller")
    result = await comm_tools.read_tool.execute(
        {"account_context": "personal", "message_id": "msg-does-not-exist"}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "state_failure"


@pytest.mark.asyncio
async def test_email_send_always_refuses_regardless_of_token(local_backend):
    """The load-bearing property: even called directly (bypassing
    SafetyPlugin's tier block entirely), email_send must never succeed —
    there is no confirm channel anywhere that could have minted a valid
    approval_token, so ANY token value must be refused."""
    caller = db.create_task("caller")
    for token in ("totally-fake", "", "Bearer real-looking-token-12345", None):
        result = await comm_tools.send_tool.execute(
            {"draft_id": "draft-whatever", "approval_token": token}, task_id=caller
        )
        assert result.ok is False
        assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_non_owner_account_context_is_refused(local_backend):
    caller = db.create_task("caller")
    result = await comm_tools.draft_tool.execute(
        {"account_context": "mom", "recipient": "x@example.com", "subject": "", "body": "x"},
        task_id=caller,
    )
    assert result.ok is False
    assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_unrecognized_account_context_is_reasoning_failure(local_backend):
    caller = db.create_task("caller")
    result = await comm_tools.draft_tool.execute(
        {"account_context": "totally_bogus_xyz", "recipient": "x@example.com", "subject": "", "body": "x"},
        task_id=caller,
    )
    assert result.ok is False
    assert result.error.kind == "reasoning_failure"


@pytest.mark.asyncio
async def test_calendar_create_then_list(local_backend):
    caller = db.create_task("caller")

    create_result = await comm_tools.calendar_create_event_tool.execute(
        {
            "account_context": "personal",
            "event": {
                "title": "team sync",
                "start": "2026-01-01T10:00:00+00:00",
                "end": "2026-01-01T10:30:00+00:00",
                "attendees": ["a@example.com"],
            },
        },
        task_id=caller,
    )
    assert create_result.ok
    event_id = create_result.data["event_id"]

    list_result = await comm_tools.calendar_list_events_tool.execute(
        {
            "account_context": "personal",
            "date_range": {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-02T00:00:00+00:00"},
        },
        task_id=caller,
    )
    assert list_result.ok
    assert any(e["event_id"] == event_id for e in list_result.data["events"])


def test_email_send_is_registered_high_tier():
    from orbit.policy import load_risk_tiers

    tiers = load_risk_tiers()
    assert tiers.get("email_send") == "high"


def test_email_send_is_not_in_the_exposed_tool_filter():
    from orbit.skills import communication as communication_skill

    toolset = communication_skill.build_toolset(task_id="probe")
    assert "email_send" not in toolset.tool_filter
    assert "email_draft" in toolset.tool_filter


def test_unknown_backend_name_is_a_hard_config_error():
    from orbit.mcp_servers.communication_backend import get_backend

    with pytest.raises(ValueError):
        get_backend("gmail")
