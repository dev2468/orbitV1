"""`communication` MCP server — Prompt 6 of the MCP tool layer doc.

Thin FastMCP wrapper: each tool call goes through a Prompt-0 BaseTool
(communication_tools.py). Never an unhandled exception reaches the model.

Response shape follows the other servers' precedent: bare ToolResult.data
on success, a compact {error, message} on failure — not the full envelope.

Run standalone: python -m orbit.mcp_servers.communication_server
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from orbit import db
from orbit.mcp_servers.communication_tools import (
    _resolve_task_id,
    calendar_create_event_tool,
    calendar_list_events_tool,
    draft_tool,
    list_threads_tool,
    read_tool,
    search_tool,
    send_tool,
)

mcp = FastMCP("communication")


def _payload(result) -> Any:
    """Unwrap a ToolResult into what the model should actually see. Success
    -> bare data. Failure -> compact {error, message}. The full envelope is
    already in the event log via BaseTool.execute; nothing is lost."""
    if result.ok:
        return result.data
    return {"error": result.error.kind, "message": result.error.message}


@mcp.tool()
async def email_draft(account_context: str, recipient: str, body: str, subject: str = "", task_id: str = "") -> Any:
    """Create an email draft bound to account_context. Returns {draft_id}."""
    result = await draft_tool.execute(
        {"account_context": account_context, "recipient": recipient, "subject": subject, "body": body},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def email_send(draft_id: str, approval_token: str, task_id: str = "") -> Any:
    """Send a previously created draft. BLOCKED in this build regardless
    of approval_token — no confirmation channel exists to mint a valid one."""
    result = await send_tool.execute(
        {"draft_id": draft_id, "approval_token": approval_token}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def email_search(account_context: str, query: str, limit: int = 20, task_id: str = "") -> Any:
    """Search a mailbox by keyword against subject and body."""
    result = await search_tool.execute(
        {"account_context": account_context, "query": query, "limit": limit},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def email_read(account_context: str, message_id: str, task_id: str = "") -> Any:
    """Read one email by message_id. Body arrives wrapped in
    <untrusted_email_content> markers."""
    result = await read_tool.execute(
        {"account_context": account_context, "message_id": message_id},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def email_list_threads(account_context: str, folder: str = "sent", limit: int = 20, task_id: str = "") -> Any:
    """List threads in a folder without reading full bodies."""
    result = await list_threads_tool.execute(
        {"account_context": account_context, "folder": folder, "limit": limit},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def calendar_list_events(account_context: str, date_range: dict, task_id: str = "") -> Any:
    """Read calendar events overlapping date_range: {start, end} (ISO-8601)."""
    result = await calendar_list_events_tool.execute(
        {"account_context": account_context, "date_range": date_range},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def calendar_create_event(account_context: str, event: dict, task_id: str = "") -> Any:
    """Create a calendar event: event = {title, start, end, attendees?}."""
    result = await calendar_create_event_tool.execute(
        {"account_context": account_context, "event": event}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


if __name__ == "__main__":
    # A fresh subprocess launch has no guarantee the schema already exists
    # (same fix as memory_server.py / filesystem_server.py). Only orbit's
    # own task/event DB needs this — communication_backend.LocalMailBackend
    # initializes its own schema lazily on first construction.
    db.init_db()
    mcp.run()
