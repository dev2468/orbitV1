"""`windows-control` MCP server — Prompt 1 of the MCP tool layer doc.

Thin FastMCP wrapper: each tool call goes through a Prompt-0 BaseTool
(windows_control_tools.py). Never an unhandled exception reaches the model.

Response shape follows the other servers' precedent (see filesystem_server.py /
memory_server.py's docstrings): bare ToolResult.data on success, a compact
{error, message} on failure — not the full envelope.

This server is `lane: foreground` end to end — see orbit/skills/windows_control.py
and orbit/agent.py's lane-gated tool list for why it must never be reachable
from a task submitted under lane="headless".

Run standalone: python -m orbit.mcp_servers.windows_control_server
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from orbit import db
from orbit.mcp_servers.windows_control_tools import (
    _resolve_task_id,
    click_tool,
    clipboard_copy_image_tool,
    drag_tool,
    focus_window_tool,
    get_foreground_window_tool,
    key_tool,
    open_app_tool,
    scroll_tool,
    type_tool,
    wait_tool,
)

mcp = FastMCP("windows-control")


def _payload(result) -> Any:
    """Unwrap a ToolResult into what the model should actually see. Success
    -> bare data. Failure -> compact {error, message}. The full envelope is
    already in the event log via BaseTool.execute; nothing is lost."""
    if result.ok:
        return result.data
    return {"error": result.error.kind, "message": result.error.message}


@mcp.tool()
async def windows_get_foreground_window(task_id: str = "") -> Any:
    """Return the handle/title/process of whatever currently holds OS
    focus. Call this before other windows_* calls."""
    result = await get_foreground_window_tool.execute({}, task_id=_resolve_task_id(task_id))
    return _payload(result)


@mcp.tool()
async def windows_wait(condition: dict, timeout: float = 10.0, task_id: str = "") -> Any:
    """Wait for a window (by title substring) or process to appear/exit.
    condition: {type: window_title|process_running|process_exited, value: str}."""
    result = await wait_tool.execute(
        {"condition": condition, "timeout": timeout}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def windows_scroll(direction: str, amount: int = 3, target: Optional[dict] = None, task_id: str = "") -> Any:
    """Scroll vertically ('up' or 'down') at target's location, or the
    foreground window's center if no target is given."""
    result = await scroll_tool.execute(
        {"direction": direction, "amount": amount, "target": target}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def windows_click(target: dict, task_id: str = "", approval_token: str = "") -> Any:
    """Click a UI element. target: {window_handle, automation_id} or
    {window_handle, name, control_type?}. A target below the actuation
    confidence floor (raw {x, y}, or a vision-tier ElementRef) is refused
    unless it carries a one-shot approval_token, which SafetyPlugin obtains
    from a human before the call reaches here — do not invent one."""
    result = await click_tool.execute(
        {"target": target, "approval_token": approval_token or None},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def windows_drag(
    from_target: dict, to_target: dict, task_id: str = "", approval_token: str = ""
) -> Any:
    """Click-drag-release from one resolved target to another. Both endpoints
    are confidence-checked independently; one approval_token covers the whole
    drag, since a drag is one action."""
    result = await drag_tool.execute(
        {
            "from_target": from_target,
            "to_target": to_target,
            "approval_token": approval_token or None,
        },
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def windows_type(text: str, target: Optional[dict] = None, task_id: str = "") -> Any:
    """Type text via clipboard paste into target (focused first) or
    whatever already has focus."""
    result = await type_tool.execute({"text": text, "target": target}, task_id=_resolve_task_id(task_id))
    return _payload(result)


@mcp.tool()
async def windows_key(key_combo: str, task_id: str = "") -> Any:
    """Send a key/key-combo to the foreground window, e.g. 'Enter',
    'Ctrl+S', 'F5'. Destructive/system combos are refused outright."""
    result = await key_tool.execute({"key_combo": key_combo}, task_id=_resolve_task_id(task_id))
    return _payload(result)


@mcp.tool()
async def windows_open_app(app_name_or_path: str, task_id: str = "") -> Any:
    """Launch a native application by path or registered name."""
    result = await open_app_tool.execute(
        {"app_name_or_path": app_name_or_path}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def windows_focus_window(window_handle: int, task_id: str = "") -> Any:
    """Bring a window to the foreground. Blocked pending a confirmation
    channel — expect confirmation_required, not an actual focus change."""
    result = await focus_window_tool.execute(
        {"window_handle": window_handle}, task_id=_resolve_task_id(task_id)
    )
    return _payload(result)


@mcp.tool()
async def windows_clipboard_copy_image(
    image_path: str = "", image_base64: str = "", task_id: str = ""
) -> Any:
    """Place an image on the clipboard so Ctrl+V pastes it in Word, Paint,
    etc. Provide image_path OR image_base64 (from perception_capture_screenshot)."""
    result = await clipboard_copy_image_tool.execute(
        {"image_path": image_path or None, "image_base64": image_base64 or None},
        task_id=_resolve_task_id(task_id),
    )
    return _payload(result)


if __name__ == "__main__":
    # A fresh subprocess launch has no guarantee the schema already exists
    # (same fix as memory_server.py / filesystem_server.py).
    db.init_db()
    mcp.run()
