"""Tool implementations for the `screen-perception` MCP server — Prompt 2
of "Claude Code Prompts - Building the MCP Tool Layer.md". Kept separate
from perception_server.py (the FastMCP process entry point) so these are
directly unit-testable without going through the MCP stdio transport, same
split as the other *_tools.py modules.

Read-only by design (Section 11): "structured observer free and always-on,
mid-tier perception cheap, visual observer on-demand only." Every tool
here observes; none of them simulate input — that's windows-control's job,
structurally separate (Section 11 / this catalog's own framing) even
though the two servers share UIA resolution code via uia_resolver.py.

Scope note (honest, not hidden — same pattern as browser_policy_tools.py's
and windows_control_tools.py's own scope notes): this module builds the
tools that don't need anything beyond what's already installed
(pywinauto/pywin32, already required by windows-control, plus `mss` for
screenshots — a small, pure-Python, no-system-binary addition). It does
NOT build:

  - perception_read_text_region (OCR). The catalog names PaddleOCR; that
    (or any OCR engine — Tesseract needs a separate system-level binary
    install, easyocr/PaddleOCR pull in a multi-hundred-MB ML stack) is not
    installed in this environment, and installing one unattended is a
    bigger, more consequential call than adding a small pure-Python
    library — the kind of thing to confirm rather than assume. Calling
    this tool today would mean faking OCR output, which is worse than not
    having it.
  - perception_vision_locate. The catalog is explicit about this one:
    "Do not implement this tool's exact signature ahead of that [grounding]
    spike; the representation decision belongs in the spike's output, not
    guessed here." No spike has run in this build. Guessing UI-TARS's or
    Nemotron's expected input/output shape now would produce exactly the
    kind of throwaway-and-redo work the catalog is warning against.

Neither is stubbed with fake data — both are simply absent from this
server's tool set, same as browser_click/browser_type were honestly left
out of the first browser-policy pass.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import mss
import win32gui

from orbit import db
from orbit.mcp_servers.uia_resolver import get_uia_tree, resolve_uia_element, window_snapshot
from orbit.task_manager import CancellationToken
from orbit.tools.foundation import BaseTool, ClassifiedToolError, Confidence, ToolMetadata

_LOW_HEADLESS = dict(
    risk_tier="low",
    lane="headless",
    requires_confirmation=False,
    is_destructive=False,
    returns_untrusted_content=False,
)

_FALLBACK_TASK_ID = "adhoc-perception-server"
_ORBIT_TASK_ID = os.environ.get("ORBIT_TASK_ID", "").strip()


def _resolve_task_id(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    if _ORBIT_TASK_ID:
        return _ORBIT_TASK_ID
    if db.get_task(_FALLBACK_TASK_ID) is None:
        db.create_task("perception server adhoc calls", task_id=_FALLBACK_TASK_ID)
    return _FALLBACK_TASK_ID


def _foreground_or_given(window_handle: Optional[int]) -> int:
    if window_handle is not None:
        return window_handle
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        raise ClassifiedToolError("state_failure", "no window currently has foreground focus")
    return hwnd


class GetStateTool(BaseTool):
    """perception_get_state — the structured observer: active window
    title, foreground process, and (if task_id is given) that task's
    status. No model call, no vision, effectively free — this is what
    answers "what's on my screen" without spending anything."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        hwnd = win32gui.GetForegroundWindow()
        foreground = window_snapshot(hwnd) if hwnd else None

        task_status = None
        query_task_id = args.get("task_id")
        if query_task_id:
            task = db.get_task(query_task_id)
            task_status = task["status"] if task else None

        return {"foreground_window": foreground, "task_status": task_status}, Confidence.API_SUCCESS


class GetUiaTreeTool(BaseTool):
    """perception_get_uia_tree — the tier that "answers most questions for
    free" (per the build plan): most native apps expose full structure
    through UIA without needing OCR or vision. Depth/node-capped (see
    uia_resolver.get_uia_tree) so one call can't flood the model's context
    with an entire app's control tree."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        window_handle = _foreground_or_given(args.get("window_handle"))
        max_depth = args.get("max_depth", 6)
        max_nodes = args.get("max_nodes", 200)
        nodes = get_uia_tree(window_handle, max_depth=max_depth, max_nodes=max_nodes)
        return (
            {"window_handle": window_handle, "nodes": nodes, "truncated": len(nodes) >= max_nodes},
            Confidence.API_SUCCESS,
        )


class FindElementTool(BaseTool):
    """perception_find_element — the unified resolution entry point named
    explicitly in the build plan: tries UIA first, then OCR, then vision,
    stopping at the first tier that produces a confident match. Only the
    UIA tier is implemented in this build (see module docstring for why
    OCR/vision aren't) — tier_order is still accepted for forward
    compatibility with the eventual 3-tier version, but any tier other
    than "uia" in it is reported back as unavailable rather than silently
    skipped without explanation.

    Returns an ElementRef (orbit.tools.element_ref) — the exact shape
    windows_click/windows_drag's target argument already accepts directly
    (Contract 3), so the natural sequence is:
      1. perception_find_element(query={"window_handle":..., "name":...})
      2. windows_click(target=<the ElementRef this returned>)
    with no second UIA lookup in between."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        query = args["query"]
        tier_order = args.get("tier_order") or ["uia"]
        unavailable = [t for t in tier_order if t != "uia"]

        window_handle = _foreground_or_given(query.get("window_handle"))
        automation_id = query.get("automation_id")
        name = query.get("name")
        control_type = query.get("control_type")
        if not automation_id and not name:
            raise ClassifiedToolError(
                "reasoning_failure",
                "query needs automation_id or name to locate an element — this build only "
                "resolves the uia tier, which requires a locator, not a free-text description",
            )

        element = resolve_uia_element(
            window_handle, automation_id=automation_id, name=name, control_type=control_type
        )
        return (
            {"element": element.model_dump(), "tiers_tried": ["uia"], "tiers_unavailable": unavailable},
            element.confidence,
        )


class CaptureScreenshotTool(BaseTool):
    """perception_capture_screenshot — captures the screen or a region via
    `mss` (pure-Python, no system binary — see module docstring for why
    this isn't DXCam/Windows.Graphics.Capture as the catalog names). Never
    fired by continuous polling — on-demand only, same "hotkey or a cheap
    change-detection trigger" framing the catalog uses. Returns PNG bytes
    base64-encoded plus the captured region, not a file path — nothing is
    written to disk by this tool."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        import base64

        region = args.get("region")
        with mss.MSS() as sct:
            monitor = _region_to_monitor(region, sct)
            shot = sct.grab(monitor)
            png_bytes = mss.tools.to_png(shot.rgb, shot.size)

        return (
            {
                "region": {"left": monitor["left"], "top": monitor["top"], "width": monitor["width"], "height": monitor["height"]},
                "image_base64": base64.b64encode(png_bytes).decode("ascii"),
                "format": "png",
            },
            Confidence.API_SUCCESS,
        )


def _region_to_monitor(region: Optional[dict], sct: "mss.base.MSSBase") -> dict:
    if region:
        return {
            "left": int(region["left"]),
            "top": int(region["top"]),
            "width": int(region["width"]),
            "height": int(region["height"]),
        }
    primary = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
    return primary


class WaitForVisualChangeTool(BaseTool):
    """perception_wait_for_visual_change — blocks until a pixel-diff is
    detected in `region`, or times out. The perception-side equivalent of
    browser_wait_for — needed because native UI has no DOM-mutation event
    to hook the way a browser does. Compares raw RGB bytes of successive
    `mss` grabs; any byte difference counts as a change (no fuzz
    threshold) — cheap and correct for "did anything change", not
    sensitive to *how much*."""

    default_timeout_s = 90.0  # same reasoning as WaitTool in
    # windows_control_tools.py: this polls on its own deadline, so needs
    # headroom beyond the 30s in-process default.

    _POLL_INTERVAL_S = 0.3
    _MAX_TIMEOUT_S = 60.0

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        import asyncio

        region = args.get("region")
        timeout = min(float(args.get("timeout", 10.0)), self._MAX_TIMEOUT_S)

        with mss.MSS() as sct:
            monitor = _region_to_monitor(region, sct)
            baseline = bytes(sct.grab(monitor).rgb)

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                token.raise_if_cancelled()
                current = bytes(sct.grab(monitor).rgb)
                if current != baseline:
                    return {"changed": True, "region": monitor}, Confidence.API_SUCCESS
                await asyncio.sleep(self._POLL_INTERVAL_S)

        return {"changed": False, "region": monitor}, Confidence.API_SUCCESS


def _metadata(name: str, description: str, **overrides) -> ToolMetadata:
    fields = {**_LOW_HEADLESS, **overrides}
    return ToolMetadata(name=name, description=description, **fields)


get_state_tool = GetStateTool(
    _metadata(
        "perception_get_state",
        "Return the active window's title/process and (if task_id given) "
        "that task's status. No model call, effectively free — call this "
        "before anything else when you need to know what's on screen.",
    )
)
get_uia_tree_tool = GetUiaTreeTool(
    _metadata(
        "perception_get_uia_tree",
        "Return the UI Automation tree for a window (default: the "
        "foreground window) as a flat, depth-labeled node list — role, "
        "name, automation_id, bounds. Capped at max_nodes (default 200); "
        "check `truncated` before assuming you saw the whole tree.",
    )
)
find_element_tool = FindElementTool(
    _metadata(
        "perception_find_element",
        "Resolve a UI element by locator (query: {window_handle?, "
        "automation_id?, name?, control_type?}) to an ElementRef. Only "
        "the uia tier is implemented — query needs automation_id or name, "
        "not a free-text visual description. Feed the returned element "
        "straight into windows_click/windows_drag's target argument.",
    )
)
capture_screenshot_tool = CaptureScreenshotTool(
    _metadata(
        "perception_capture_screenshot",
        "Capture the screen (or `region`: {left, top, width, height}) as "
        "a base64-encoded PNG. On-demand only — never call this in a "
        "polling loop; use perception_wait_for_visual_change instead.",
    )
)
wait_for_visual_change_tool = WaitForVisualChangeTool(
    _metadata(
        "perception_wait_for_visual_change",
        "Block until any pixel in `region` (default: primary monitor) "
        "changes, or `timeout` seconds elapse (capped at 60). Returns "
        "{changed: bool}.",
    )
)
