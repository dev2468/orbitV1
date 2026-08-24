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
    browser_type_tool,
    click_tool,
    close_session_tool,
    drag_tool,
    extract_tool,
    go_back_tool,
    go_forward_tool,
    handle_dialog_tool,
    hover_tool,
    navigate_tool,
    open_session_tool,
    press_key_tool,
    resolve_task_id,
    select_option_tool,
    session_reaper_loop,
    snapshot_tool,
    tab_close_tool,
    tab_list_tool,
    tab_new_tool,
    tab_select_tool,
    take_screenshot_tool,
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


@mcp.tool()
async def browser_click(session_id: str, element: str, ref: str, task_id: str = "") -> Any:
    """Click an element on the page. 'element' is a human-readable description,
    'ref' is the exact element reference from browser_snapshot."""
    result = await click_tool.execute(
        {"session_id": session_id, "element": element, "ref": ref},
        task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_type(
    session_id: str, element: str, ref: str, text: str,
    submit: bool = False, task_id: str = "",
) -> Any:
    """Type text into an input field. Set submit=true to press Enter after."""
    result = await browser_type_tool.execute(
        {"session_id": session_id, "element": element, "ref": ref,
         "text": text, "submit": submit},
        task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_hover(session_id: str, element: str, ref: str, task_id: str = "") -> Any:
    """Hover over an element to reveal tooltips or dropdowns."""
    result = await hover_tool.execute(
        {"session_id": session_id, "element": element, "ref": ref},
        task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_select_option(
    session_id: str, element: str, ref: str, values: list[str], task_id: str = "",
) -> Any:
    """Select option(s) in a <select> dropdown."""
    result = await select_option_tool.execute(
        {"session_id": session_id, "element": element, "ref": ref, "values": values},
        task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_press_key(session_id: str, key: str, task_id: str = "") -> Any:
    """Press a keyboard key (Enter, Escape, ArrowDown, PageDown, etc.)."""
    result = await press_key_tool.execute(
        {"session_id": session_id, "key": key},
        task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_go_back(session_id: str, task_id: str = "") -> Any:
    """Go back in browser history."""
    result = await go_back_tool.execute(
        {"session_id": session_id}, task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_go_forward(session_id: str, task_id: str = "") -> Any:
    """Go forward in browser history."""
    result = await go_forward_tool.execute(
        {"session_id": session_id}, task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_tab_new(session_id: str, url: str = "", task_id: str = "") -> Any:
    """Open a new browser tab, optionally at a URL (subject to URL policy)."""
    result = await tab_new_tool.execute(
        {"session_id": session_id, "url": url} if url else {"session_id": session_id},
        task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_tab_list(session_id: str, task_id: str = "") -> Any:
    """List all open tabs with titles and URLs."""
    result = await tab_list_tool.execute(
        {"session_id": session_id}, task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_tab_select(session_id: str, index: int, task_id: str = "") -> Any:
    """Switch to a tab by its index (from browser_tab_list)."""
    result = await tab_select_tool.execute(
        {"session_id": session_id, "index": index},
        task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_tab_close(session_id: str, task_id: str = "") -> Any:
    """Close the current browser tab."""
    result = await tab_close_tool.execute(
        {"session_id": session_id}, task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_take_screenshot(session_id: str, task_id: str = "") -> Any:
    """Take a screenshot of the current page."""
    result = await take_screenshot_tool.execute(
        {"session_id": session_id}, task_id=resolve_task_id(task_id),
    )
    return _payload(result)


@mcp.tool()
async def browser_handle_dialog(
    session_id: str, accept: bool, promptText: str = "", task_id: str = "",
) -> Any:
    """Accept or dismiss a browser dialog (alert, confirm, prompt)."""
    args: dict[str, Any] = {"session_id": session_id, "accept": accept}
    if promptText:
        args["promptText"] = promptText
    result = await handle_dialog_tool.execute(args, task_id=resolve_task_id(task_id))
    return _payload(result)


@mcp.tool()
async def browser_drag(
    session_id: str, startElement: str, startRef: str,
    endElement: str, endRef: str, task_id: str = "",
) -> Any:
    """Drag one element to another."""
    result = await drag_tool.execute(
        {"session_id": session_id, "startElement": startElement, "startRef": startRef,
         "endElement": endElement, "endRef": endRef},
        task_id=resolve_task_id(task_id),
    )
    return _payload(result)


if __name__ == "__main__":
    # db.init_db() and session teardown both live in _lifespan so they run
    # inside the server's own event loop.
    mcp.run()
