"""`memory` MCP server — Prompt 3 of the MCP tool layer doc.

Thin FastMCP wrapper: each tool call goes through a Prompt-0 BaseTool
(memory_tools.py). Never an unhandled exception reaches the model.

Response shape follows browser_policy_server.py's precedent: these tools
return the MODEL-facing payload (ToolResult.data, or a compact
{error, message} on failure), not the full ToolResult envelope. This
server originally returned result.model_dump() — the same defect Fix 1
found and fixed on the browser-policy server — which forces the model to
dig for e.g. data.result_summary instead of result_summary on every call.
Left as-is here, newly-exposed memory_search_tasks would carry the same
tool-calling reliability cost the browser tools had, which directly
undermines the search-first behavior this change is adding. The full
envelope is still persisted to the event log by BaseTool.execute.

Run standalone: python -m orbit.mcp_servers.memory_server
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from orbit import db
from orbit.mcp_servers.memory_tools import (
    _resolve_task_id,
    get_context_tool,
    get_policy_tool,
    get_task_tool,
    search_tasks_tool,
    write_memory_tool,
)

mcp = FastMCP("memory")


def _payload(result) -> Any:
    """Unwrap a ToolResult into what the model should actually see. Success
    -> bare data. Failure -> compact {error, message}. The full envelope is
    already in the event log via BaseTool.execute; nothing is lost."""
    if result.ok:
        return result.data
    return {"error": result.error.kind, "message": result.error.message}


@mcp.tool()
async def memory_search_tasks(query: str, limit: int = 10, task_id: str = "") -> Any:
    """Search past completed tasks by keyword (FTS5 over title/goal/result).
    Call this BEFORE starting research that involves browsing — if a past
    task already answered the question, its result is in here."""
    result = await search_tasks_tool.execute(
        {"query": query, "limit": limit}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def memory_get_task(
    task_id: str, include_events: bool = False, caller_task_id: str = ""
) -> Any:
    """Fetch one task's full record. include_events=True adds the (verbose)
    tool-call trail."""
    result = await get_task_tool.execute(
        {"task_id": task_id, "include_events": include_events},
        task_id=_resolve_task_id(caller_task_id),
    )
    return _payload(result)


@mcp.tool()
async def memory_write(
    memory_type: str,
    content: str,
    task_id: Optional[str] = None,
    project: Optional[str] = None,
    provenance: str = "system",
    caller_task_id: str = "",
) -> Any:
    """Write a memory row. memory_type: episodic | semantic | procedural |
    project. provenance: user | system | external — tag 'external' for
    anything that came from a web page, email, or other outside source."""
    result = await write_memory_tool.execute(
        {
            "memory_type": memory_type,
            "content": content,
            "task_id": task_id,
            "project": project,
            "provenance": provenance,
        },
        task_id=_resolve_task_id(caller_task_id),
    )
    return _payload(result)


@mcp.tool()
async def memory_get_context(query: str, budget_tokens: int = 2000, task_id: str = "") -> Any:
    """Assemble a token-budgeted context block of relevant past memories.
    Prefer this over memory_search_tasks for durable facts/procedures."""
    result = await get_context_tool.execute(
        {"query": query, "budget_tokens": budget_tokens},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def memory_get_policy(context: str, task_id: str = "") -> Any:
    """Resolve which Chrome profile/policy applies to a named context.
    Always call this rather than guessing a profile."""
    result = await get_policy_tool.execute(
        {"context": context}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


if __name__ == "__main__":
    # A fresh subprocess launch has no guarantee the schema already exists
    # (found by actually running this as a subprocess, not just importing
    # it in-process under the test fixture, which pre-creates it).
    db.init_db()
    mcp.run()
