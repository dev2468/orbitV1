"""`screen-perception` MCP server — Prompt 2 of the MCP tool layer doc.

Thin FastMCP wrapper: each tool call goes through a Prompt-0 BaseTool
(perception_tools.py). Never an unhandled exception reaches the model.

Response shape follows the other servers' precedent: bare ToolResult.data
on success, a compact {error, message} on failure — not the full envelope.

Every tool here is read-only (Section 11: "perception is read-only and
free to call while actuation is gated") — including perception_vision_locate,
which only looks at the screen and reports coordinates; it cannot click
anything, and its output is refused by windows-control's confidence gate by
design. See perception_tools.py's module docstring for the one catalog tool
still NOT built here (perception_read_text_region) and why.

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
    ui_memory_lookup_tool,
    ui_memory_upsert_tool,
    vision_locate_tool,
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


def _prune_uia_nodes(payload: Any) -> Any:
    """Drop UIA nodes the model provably cannot act on, and empty fields.

    Applied at the MCP edge rather than in `uia_resolver.get_uia_tree`
    because that walk is shared with `candidate_source.py`, whose geometry
    filter genuinely wants the unnamed and the invisible. Only the model's
    copy is pruned.

    Two classes go: nodes with `visible` explicitly False (offscreen or
    collapsed — not clickable, not readable), and nodes carrying neither a
    name nor an automation_id (nothing to locate them by and no text to
    read, so they are pure structural noise to a caller that addresses
    elements by locator). Null-valued keys are stripped from survivors,
    since a flat node list repeats every key on every node.

    This matters because perception_get_uia_tree is the main tool in
    foreground mode and averaged 12,955 characters per call in this build's
    event log — roughly 3,200 tokens, re-sent on every subsequent turn of
    the task once it enters the conversation.
    """
    if not isinstance(payload, dict) or "error" in payload:
        return payload
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return payload

    kept = []
    for node in nodes:
        if not isinstance(node, dict):
            kept.append(node)
            continue
        if node.get("visible") is False:
            continue
        if not node.get("name") and not node.get("automation_id"):
            continue
        kept.append({k: v for k, v in node.items() if v is not None and v != ""})

    payload["nodes"] = kept
    payload["nodes_pruned"] = len(nodes) - len(kept)
    return payload


@mcp.tool()
async def perception_get_uia_tree(
    window_handle: Optional[int] = None, max_depth: int = 6, max_nodes: int = 200, task_id: str = ""
) -> Any:
    """Return the UI Automation tree for a window (default: foreground) as
    a flat, depth-labeled node list. Capped at max_nodes. Invisible nodes
    and nodes with neither a name nor an automation_id are omitted."""
    result = await get_uia_tree_tool.execute(
        {"window_handle": window_handle, "max_depth": max_depth, "max_nodes": max_nodes},
        task_id=_resolve_task_id(task_id),
    )
    return _prune_uia_nodes(_payload(result))


@mcp.tool()
async def perception_find_element(query: dict, tier_order: Optional[list] = None, task_id: str = "") -> Any:
    """Resolve a UI element to an ElementRef. query: {window_handle?,
    automation_id?, name?, control_type?, description?}. Default tier_order
    is ['uia'] (free, needs a locator). Pass ['uia','vision'] with
    query.description to fall back to the vision tier on a UIA miss."""
    resolved = _resolve_task_id(task_id)
    # task_id rides along in args so the tool can attribute the nested
    # vision-tier execute() to the same task rather than the adhoc row.
    result = await find_element_tool.execute(
        {"query": query, "tier_order": tier_order, "task_id": resolved}, task_id=resolved
    )
    return _payload(result)


@mcp.tool()
async def perception_vision_locate(
    target_description: str, window_handle: Optional[int] = None, task_id: str = ""
) -> Any:
    """Locate a UI element from a plain-language description by sending a
    screenshot of the window to a vision model. The only tier that works on
    controls with no UI Automation representation. Slow and costs a model
    call. The returned ElementRef is read-only intelligence: windows_click
    and windows_drag will refuse it."""
    result = await vision_locate_tool.execute(
        {"target_description": target_description, "window_handle": window_handle},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def perception_capture_screenshot(region: Optional[dict] = None, task_id: str = "") -> Any:
    """Capture the screen (or region: {left, top, width, height}) to a PNG
    file and return its path plus dimensions. On-demand only, not for
    polling. Pass the returned image_path to windows_clipboard_copy_image
    to paste the capture into Word/Paint/etc."""
    result = await capture_screenshot_tool.execute({"region": region}, task_id=_resolve_task_id(task_id))
    payload = _payload(result)

    # The full-resolution base64 PNG is dropped HERE, at the MCP edge —
    # it measured 327,509 chars (~82,000 tokens) in this build's event log
    # and was re-billed on every subsequent turn for no benefit.
    # The full PNG still goes to disk for debugging/clipboard use.
    # image_small_b64 (a 4x box-filtered downscale, ~400px wide) IS kept:
    # agent.py's _inject_screenshot_images turns it into an inline image
    # block the multimodal model actually sees. candidate_source.py and
    # in-process tests read image_base64 before this stripping runs, so
    # neither loses bytes.
    if isinstance(payload, dict) and "image_base64" in payload:
        import base64 as _b64
        import time as _time
        from pathlib import Path as _Path

        raw = _b64.b64decode(payload.pop("image_base64"))
        shots = _Path(__file__).resolve().parents[2] / "data" / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"shot-{int(_time.time() * 1000)}.png"
        path.write_bytes(raw)
        payload["image_path"] = str(path)
        payload["bytes"] = len(raw)

    return payload


@mcp.tool()
async def ui_memory_lookup(process_name: str, description: str, task_id: str = "") -> Any:
    """Return cached {x, y} coordinates for a UI element. Check this before
    taking a screenshot — if it has a cached hit, use those coords directly
    with windows_click(target={x, y}). Raises state_failure on a cache miss."""
    result = await ui_memory_lookup_tool.execute(
        {"process_name": process_name, "description": description},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def ui_memory_upsert(
    process_name: str,
    description: str,
    x: int,
    y: int,
    automation_id: Optional[str] = None,
    task_id: str = "",
) -> Any:
    """Cache a successful click location. Call after every successful
    windows_click({x, y}) so future tasks skip the screenshot step.
    process_name: e.g. 'WINWORD.EXE'. description: short unique label."""
    result = await ui_memory_upsert_tool.execute(
        {
            "process_name": process_name,
            "description": description,
            "x": x,
            "y": y,
            "automation_id": automation_id,
        },
        task_id=_resolve_task_id(task_id),
    )
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
