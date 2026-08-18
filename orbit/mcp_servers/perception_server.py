"""`screen-perception` MCP server — Prompt 2 of the MCP tool layer doc.

Thin FastMCP wrapper: each tool call goes through a Prompt-0 BaseTool
(perception_tools.py). Never an unhandled exception reaches the model.

Response shape follows the other servers' precedent: bare ToolResult.data
on success, a compact {error, message} on failure — not the full envelope.

Every tool here is read-only (Section 11: "perception is read-only and
free to call while actuation is gated"). See perception_tools.py's module
docstring for the two tools NOT built here (perception_read_text_region,
perception_vision_locate) and why.

Run standalone: python -m orbit.mcp_servers.perception_server
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from orbit import db
from orbit.mcp_servers.perception_tools import (
    _resolve_task_id,
    capture_screenshot_tool,
    find_element_tool,
    get_state_tool,
    get_uia_tree_tool,
    wait_for_visual_change_tool,
)

mcp = FastMCP("screen-perception")


def _payload(result) -> Any:
    """Unwrap a ToolResult into what the model should actually see. Success
    -> bare data. Failure -> compact {error, message}. The full envelope is
    already in the event log via BaseTool.execute; nothing is lost."""
    if result.ok:
        return result.data
    return {"error": result.error.kind, "message": result.error.message}


@mcp.tool()
async def perception_get_state(query_task_id: Optional[str] = None, task_id: str = "") -> Any:
    """Return the active window's title/process, and query_task_id's
    status if given. Effectively free — no model call, no vision."""
    result = await get_state_tool.execute(
        {"task_id": query_task_id}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def perception_get_uia_tree(
    window_handle: Optional[int] = None, max_depth: int = 6, max_nodes: int = 200, task_id: str = ""
) -> Any:
    """Return the UI Automation tree for a window (default: foreground) as
    a flat, depth-labeled node list. Capped at max_nodes."""
    result = await get_uia_tree_tool.execute(
        {"window_handle": window_handle, "max_depth": max_depth, "max_nodes": max_nodes},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def perception_find_element(query: dict, tier_order: Optional[list] = None, task_id: str = "") -> Any:
    """Resolve a UI element by locator to an ElementRef. query:
    {window_handle?, automation_id?, name?, control_type?}. Feed the
    result straight into windows_click/windows_drag's target."""
    result = await find_element_tool.execute(
        {"query": query, "tier_order": tier_order}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def perception_capture_screenshot(region: Optional[dict] = None, task_id: str = "") -> Any:
    """Capture the screen (or region: {left, top, width, height}) as a
    base64-encoded PNG. On-demand only, not for polling."""
    result = await capture_screenshot_tool.execute({"region": region}, task_id=_resolve_task_id(task_id))
    return _payload(result)


@mcp.tool()
async def perception_wait_for_visual_change(
    region: Optional[dict] = None, timeout: float = 10.0, task_id: str = ""
) -> Any:
    """Block until any pixel in region (default: primary monitor) changes,
    or timeout seconds elapse (capped at 60)."""
    result = await wait_for_visual_change_tool.execute(
        {"region": region, "timeout": timeout}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


if __name__ == "__main__":
    # A fresh subprocess launch has no guarantee the schema already exists
    # (same fix as every other server here).
    db.init_db()
    mcp.run()
