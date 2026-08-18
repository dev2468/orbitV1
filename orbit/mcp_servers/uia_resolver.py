"""Shared window/process/UIA inspection helpers — used by BOTH
`windows_control_tools.py` (actuation) and `perception_tools.py`
(read-only observation).

This module used to be inline in windows_control_tools.py, written before
screen-perception existed: "screen-perception (Prompt 2) is not built, so
there is no perception_find_element to hand this server a resolved
ElementRef... _resolve_click_target does that resolution itself." Now that
screen-perception exists, its perception_find_element and
perception_get_uia_tree need the exact same resolution logic — extracting
it here means both servers call one real implementation instead of two
that could drift apart, while the two MCP server PROCESSES stay
structurally separate (Section 11: "perception is read-only and free to
call while actuation is gated — structurally separate servers, not just a
convention"). Sharing a Python module is not the same as sharing a
process: each server still only exposes its own tool set, and
windows-control's actuation tools are still the only ones that ever
simulate input.

Nothing in here writes anything or simulates input — every function is a
read: a `win32gui`/`win32process` query or a pywinauto UIA tree walk.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

import win32api
import win32con
import win32gui
import win32process
from pywinauto import Application

from orbit.tools.element_ref import ElementRef
from orbit.tools.foundation import ClassifiedToolError, Confidence


def process_name(pid: int) -> str:
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
    except Exception:
        return ""
    try:
        return os.path.basename(win32process.GetModuleFileNameEx(handle, 0))
    except Exception:
        return ""
    finally:
        win32api.CloseHandle(handle)


def window_snapshot(hwnd: int) -> dict:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return {
        "window_handle": hwnd,
        "title": win32gui.GetWindowText(hwnd),
        "process_id": pid,
        "process_name": process_name(pid),
        "bounds": (left, top, right, bottom),
    }


def find_window_by_title_substring(substring: str) -> Optional[int]:
    substring_lower = substring.lower()
    found: list[int] = []

    def _cb(hwnd: int, _extra) -> bool:
        if win32gui.IsWindowVisible(hwnd) and substring_lower in win32gui.GetWindowText(hwnd).lower():
            found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def is_process_running(name_or_pid) -> bool:
    if isinstance(name_or_pid, int) or str(name_or_pid).isdigit():
        pid = int(name_or_pid)
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
        except Exception:
            return False
        win32api.CloseHandle(handle)
        return True
    target = str(name_or_pid).lower()
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5
    )
    return target in result.stdout.lower()


def _connect(window_handle: int):
    try:
        app = Application(backend="uia").connect(handle=window_handle)
        return app.window(handle=window_handle)
    except Exception as exc:
        raise ClassifiedToolError(
            "state_failure", f"window_handle {window_handle} is not a valid/open window: {exc}"
        ) from exc


def resolve_uia_element(
    window_handle: int, *, automation_id: Optional[str], name: Optional[str], control_type: Optional[str]
) -> ElementRef:
    """The UIA-tier resolution both windows_click/windows_drag (actuation)
    and perception_find_element (observation) use. Ambiguous locators
    raise reasoning_failure asking for a narrower one rather than guessing
    which match was meant — same philosophy as GetPolicyTool's refusal to
    guess a chrome profile."""
    win = _connect(window_handle)

    kwargs: dict[str, str] = {}
    if automation_id:
        kwargs["auto_id"] = automation_id
    if name:
        kwargs["title"] = name
    if control_type:
        kwargs["control_type"] = control_type

    try:
        candidates = win.descendants(**kwargs)
    except Exception as exc:
        raise ClassifiedToolError("state_failure", f"UIA lookup failed for {kwargs!r}: {exc}") from exc

    if not candidates:
        raise ClassifiedToolError(
            "state_failure", f"no element matched {kwargs!r} within window {window_handle}"
        )
    if len(candidates) > 1:
        raise ClassifiedToolError(
            "reasoning_failure",
            f"{len(candidates)} elements matched {kwargs!r} — add automation_id or control_type "
            "to narrow the locator rather than guessing which one was meant",
        )

    elem = candidates[0]
    rect = elem.rectangle()
    bounds = (rect.left, rect.top, rect.right, rect.bottom)
    confidence = Confidence.UIA_AUTOMATION_ID if automation_id else Confidence.UIA_NAME_MATCH
    try:
        role = elem.friendly_class_name()
    except Exception:
        role = None
    try:
        resolved_name = elem.window_text()
    except Exception:
        resolved_name = name

    return ElementRef(
        element_id=f"hwnd:{window_handle}/{kwargs}",
        role=role,
        name=resolved_name,
        bounds=bounds,
        source="uia",
        confidence=confidence,
    )


def get_uia_tree(window_handle: int, *, max_depth: int = 6, max_nodes: int = 200) -> list[dict]:
    """Walks the UIA descendant tree breadth-limited by max_nodes and
    depth-limited by max_depth — the tool catalog's own framing for this
    tier ("answers most questions for free") only holds if a single call
    can't flood the model's context with an entire native app's control
    tree. Order is depth-first, root first (depth=0 is the window itself),
    so truncation at max_nodes still returns a sensibly-rooted partial
    tree rather than an arbitrary slice."""
    win = _connect(window_handle)
    nodes: list[dict] = []

    def _describe(elem) -> dict:
        try:
            rect = elem.rectangle()
            bounds = (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            bounds = None
        try:
            role = elem.friendly_class_name()
        except Exception:
            role = None
        try:
            name = elem.window_text()
        except Exception:
            name = None
        try:
            automation_id = elem.automation_id()
        except Exception:
            automation_id = None
        try:
            visible = elem.is_visible()
        except Exception:
            visible = None
        return {
            "role": role,
            "name": name,
            "automation_id": automation_id or None,
            "bounds": bounds,
            "visible": visible,
        }

    def _walk(elem, depth: int) -> None:
        if len(nodes) >= max_nodes:
            return
        node = _describe(elem)
        node["depth"] = depth
        nodes.append(node)
        if depth >= max_depth:
            return
        try:
            children = elem.children()
        except Exception:
            children = []
        for child in children:
            if len(nodes) >= max_nodes:
                break
            _walk(child, depth + 1)

    _walk(win, 0)
    return nodes
