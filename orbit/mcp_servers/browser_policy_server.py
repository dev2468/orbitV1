"""`browser-policy` MCP server — Prompt 4 of the MCP tool layer doc.

A thin FastMCP wrapper: each tool call goes through a Prompt-0 BaseTool
(browser_policy_tools.py). This process itself spawns further Playwright
MCP subprocesses per session — it's a proxy, not a reimplementation.

Response shape: these tools return the MODEL-facing payload (the ToolResult
`data`, or a compact {error, message} on failure), not the full ToolResult
envelope. The envelope — ok/confidence/untrusted/duration_ms/event_id —
exists for the system, and handing it to an LLM forces it to dig for
`data.title` instead of `title` on every single call, which measurably
degrades tool-calling reliability on mid-tier models. Nothing is lost for
auditing: BaseTool.execute still writes the complete envelope to the event
log before these wrappers ever unwrap it.

Run standalone: python -m orbit.mcp_servers.browser_policy_server
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from orbit import db
from orbit.mcp_servers.browser_policy_tools import (
    aclose_all_sessions,
    close_session_tool,
    extract_tool,
    navigate_tool,
    open_session_tool,
    resolve_task_id,
    session_reaper_loop,
    snapshot_tool,
)

@asynccontextmanager
async def _lifespan(_server: FastMCP):
    """Guarantee spawned Playwright subprocesses are torn down inside the
    server's OWN event loop.

    The previous shutdown path called asyncio.run(aclose_all_sessions()) in
    a __main__ finally block, which spins up a *fresh* loop — the sessions
    belong to the server's loop, so that could never have unwound them and
    would have left orphaned Chrome processes holding profile locks.
    """
    db.init_db()
    reaper = asyncio.create_task(session_reaper_loop())
    try:
        yield {}
    finally:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass
        # Last resort only. The reaper is the designed teardown path; this
        # exists for the case where the process is torn down before a
        # reaper pass gets to run.
        await aclose_all_sessions()


mcp = FastMCP("browser-policy", lifespan=_lifespan)


def _payload(result) -> Any:
    """Unwrap a ToolResult into what the model should actually see.

    Success -> the bare data. Failure -> a compact, actionable error. The
    full envelope has already been persisted to the event log by
    BaseTool.execute, so auditing is unaffected.
    """
    if result.ok:
        return result.data
    return {"error": result.error.kind, "message": result.error.message}


@mcp.tool()
async def browser_open(context: str, task_id: str = "") -> Any:
    """Open a browser session for a named context (e.g. 'personal',
    'research'). Returns {session_id, profile}; pass that session_id to
    every later browser call. Non-owner profiles are never reachable."""
    result = await open_session_tool.execute({"context": context}, task_id=resolve_task_id(task_id))
    return _payload(result)


@mcp.tool()
async def browser_close(session_id: str, task_id: str = "") -> Any:
    """Close a browser session and free its resources. Always call this
    when finished with a session."""
    result = await close_session_tool.execute(
        {"session_id": session_id}, task_id=resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def browser_navigate(session_id: str, url: str, task_id: str = "") -> Any:
    """Navigate an open session to a URL, subject to scheme/blocklist policy.
    Returns {ok, final_url, title}."""
    result = await navigate_tool.execute(
        {"session_id": session_id, "url": url}, task_id=resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def browser_snapshot(session_id: str, task_id: str = "") -> Any:
    """Accessibility snapshot of the current page, containing its visible
    text. Returned wrapped in <untrusted_web_content> markers."""
    result = await snapshot_tool.execute({"session_id": session_id}, task_id=resolve_task_id(task_id))
    return _payload(result)


@mcp.tool()
async def browser_extract(session_id: str, js_expression: str, task_id: str = "") -> Any:
    """Extract structured data via a JS expression, wrapped as untrusted content."""
    result = await extract_tool.execute(
        {"session_id": session_id, "js_expression": js_expression},
        task_id=resolve_task_id(task_id),
    )
    return _payload(result)


if __name__ == "__main__":
    # db.init_db() and session teardown both live in _lifespan so they run
    # inside the server's own event loop.
    mcp.run()
