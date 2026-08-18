"""Tests required by Prompt 0 of the MCP tool layer doc: an exception inside
a tool becomes a ToolError, not a crash; a cancelled token stops execution
mid-call; a high-tier tool without requires_confirmation fails to construct;
the event row is written even when the tool errors."""

import asyncio

import pytest
from pydantic import ValidationError

from orbit import db
from orbit.task_manager import CancellationToken
from orbit.tools.foundation import BaseTool, Confidence, ToolMetadata


def make_metadata(**overrides):
    base = dict(
        name="dummy_tool",
        description="A dummy tool for tests.",
        risk_tier="low",
        lane="headless",
        requires_confirmation=False,
        is_destructive=False,
        returns_untrusted_content=False,
    )
    base.update(overrides)
    return ToolMetadata(**base)


class FailingTool(BaseTool):
    async def run(self, args, token):
        raise RuntimeError("boom")


class SucceedingTool(BaseTool):
    async def run(self, args, token):
        return {"result": "ok"}, Confidence.API_SUCCESS


class CancellableTool(BaseTool):
    async def run(self, args, token):
        progress = 0
        for _ in range(50):
            token.raise_if_cancelled()
            await asyncio.sleep(0.02)
            progress += 1
        return progress, None


class SlowTool(BaseTool):
    default_timeout_s = 0.1

    async def run(self, args, token):
        await asyncio.sleep(1)
        return None, None


@pytest.mark.asyncio
async def test_exception_becomes_tool_error_not_crash():
    tool = FailingTool(make_metadata(name="failing_tool"))
    result = await tool.execute({}, task_id=db.create_task("t1"))
    assert result.ok is False
    assert result.error.kind == "tool_failure"
    assert "boom" in result.error.message


@pytest.mark.asyncio
async def test_cancelled_token_stops_execution_mid_call():
    tool = CancellableTool(make_metadata(name="cancellable_tool"))
    token = CancellationToken()

    async def canceller():
        await asyncio.sleep(0.05)
        token.cancel()

    asyncio.create_task(canceller())
    result = await tool.execute({}, task_id=db.create_task("t2"), token=token)
    assert result.ok is False
    assert result.error.kind == "cancelled"


def test_high_tier_without_confirmation_fails_to_construct():
    with pytest.raises(ValidationError):
        make_metadata(name="dangerous_tool", risk_tier="high", requires_confirmation=False)
    make_metadata(name="dangerous_tool_ok", risk_tier="high", requires_confirmation=True)


@pytest.mark.asyncio
async def test_event_row_written_on_error():
    tool = FailingTool(make_metadata(name="failing_tool_logged"))
    result = await tool.execute({"x": 1}, task_id=db.create_task("t3"))
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (result.event_id,)
        ).fetchone()
    assert row is not None
    assert row["error"] is not None
    assert "tool_failure" in row["error"]


@pytest.mark.asyncio
async def test_event_row_written_on_success():
    tool = SucceedingTool(make_metadata(name="succeeding_tool"))
    result = await tool.execute({}, task_id=db.create_task("t4"))
    assert result.ok is True
    assert result.confidence == Confidence.API_SUCCESS
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (result.event_id,)
        ).fetchone()
    assert row is not None
    assert row["error"] is None


@pytest.mark.asyncio
async def test_secret_redaction_in_logged_args():
    tool = SucceedingTool(make_metadata(name="secret_tool"))
    result = await tool.execute(
        {"api_key": "sk-abcdefghijklmnop", "note": "hi"}, task_id=db.create_task("t5")
    )
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT args FROM events WHERE event_id = ?", (result.event_id,)
        ).fetchone()
    assert "sk-abcdefghijklmnop" not in row["args"]
    assert "REDACTED" in row["args"]


def test_confidence_gate_thresholds():
    assert Confidence.gate(0.95) == "execute"
    assert Confidence.gate(0.90) == "reverify"  # boundary: not > 0.90
    assert Confidence.gate(0.80) == "reverify"
    assert Confidence.gate(0.5) == "surface"


@pytest.mark.asyncio
async def test_timeout_becomes_tool_error_timeout_kind():
    tool = SlowTool(make_metadata(name="slow_tool"))
    result = await tool.execute({}, task_id=db.create_task("t6"))
    assert result.ok is False
    assert result.error.kind == "timeout"
