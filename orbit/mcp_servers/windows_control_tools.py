"""Tool implementations for the `windows-control` MCP server — Prompt 1 of
"Claude Code Prompts - Building the MCP Tool Layer.md". Kept separate from
windows_control_server.py (the FastMCP process entry point) so these are
directly unit-testable without going through the MCP stdio transport, same
split as the other *_tools.py modules.

Built on the Prompt 0 foundation (orbit.tools.foundation.BaseTool). This is
the actuation half of the perception/actuation pair Section 11 describes —
every tool here simulates real mouse/keyboard input on the user's actual
machine. There is no scoped-roots equivalent the way filesystem has one:
input lands wherever real OS focus is, and that can't be sandboxed the way
a file path can. Two structural mitigations stand in for that:

  1. Confidence gating (Contract 3 / Section 7): windows_click and
     windows_drag refuse to act below orbit/config/windows_control_policy.yaml's
     min_actuation_confidence, which raw {x, y} coordinates never clear
     (scored at Confidence.VISION_INFERRED) — there is no confirm channel
     to route a low-confidence click through, so this is a hard stop, not
     a softer retry path.
  2. A fail-closed key-combo denylist (same file) for windows_key, since a
     tier can't express "this one call is more destructive than the rest
     of this tool's calls" any more than fs_write_file's tier can.

Element resolution (_resolve_click_target below) is shared with
screen-perception via orbit/mcp_servers/uia_resolver.py — see that
module's docstring for why sharing the Python implementation is fine even
though the two MCP server PROCESSES stay structurally separate (Section
11: perception free/read-only, actuation gated). _resolve_click_target
also accepts an ALREADY-resolved ElementRef-shaped dict directly (one a
prior perception_find_element call returned) rather than only ever
re-resolving from a locator — this is Contract 3's intended shape:
"perception_find_element... windows_click and friends consume its output
directly." Win-key (Windows-logo-key) combinations are unsupported
entirely — no reliable send_keys mapping exists — rather than
half-implemented. Horizontal scroll is unsupported for the same reason.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import win32clipboard
import win32gui
from pywinauto import keyboard as pywinauto_keyboard
from pywinauto import mouse as pywinauto_mouse

from orbit import db
from orbit.mcp_servers.uia_resolver import (
    find_window_by_title_substring,
    is_process_running,
    resolve_uia_element,
    window_snapshot,
)
from orbit.policy import load_windows_control_policy
from orbit.task_manager import CancellationToken
from orbit.tools.element_ref import ElementRef
from orbit.tools.foundation import BaseTool, ClassifiedToolError, Confidence, ToolMetadata

_MEDIUM_FOREGROUND = dict(
    risk_tier="medium",
    lane="foreground",
    requires_confirmation=False,
    is_destructive=False,
    returns_untrusted_content=False,
)

_FALLBACK_TASK_ID = "adhoc-windows-control-server"
_ORBIT_TASK_ID = os.environ.get("ORBIT_TASK_ID", "").strip()


def _resolve_task_id(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    if _ORBIT_TASK_ID:
        return _ORBIT_TASK_ID
    if db.get_task(_FALLBACK_TASK_ID) is None:
        db.create_task("windows-control server adhoc calls", task_id=_FALLBACK_TASK_ID)
    return _FALLBACK_TASK_ID


# --- ElementRef resolution ---------------------------------------------------
# process_name/window_snapshot/find_window_by_title_substring/is_process_running
# and the core UIA lookup now live in uia_resolver.py, shared with
# perception_tools.py — see this module's docstring.


def _resolve_click_target(target: dict) -> ElementRef:
    """target is one of:
      - {"x": int, "y": int} — raw coordinates, always scored at
        Confidence.VISION_INFERRED per Contract 3's rule that a
        coordinate-based call is vision-tier regardless of how it was
        obtained.
      - {"window_handle": int, "automation_id"?: str, "name"?: str,
        "control_type"?: str} — a locator, resolved fresh via UIA.
      - An already-resolved ElementRef shape — has "bounds", "source" AND
        "confidence" keys, e.g. straight out of a prior
        perception_find_element/perception_get_uia_tree call. Used as-is
        rather than re-resolved: this is what lets perception's output
        feed directly into windows_click/windows_drag per Contract 3,
        without a second UIA round-trip re-deriving what perception
        already determined."""
    if "bounds" in target and "source" in target and "confidence" in target:
        return ElementRef(
            element_id=target.get("element_id", "resolved:external"),
            role=target.get("role"),
            name=target.get("name"),
            bounds=tuple(target["bounds"]) if target["bounds"] is not None else None,
            state=target.get("state"),
            source=target["source"],
            confidence=target["confidence"],
        )

    if "x" in target and "y" in target:
        x, y = int(target["x"]), int(target["y"])
        return ElementRef(
            element_id=f"raw:{x},{y}",
            bounds=(x, y, x, y),
            source="vision",
            confidence=Confidence.VISION_INFERRED,
        )

    window_handle = target.get("window_handle")
    if window_handle is None:
        raise ClassifiedToolError(
            "reasoning_failure", "target must include either {x, y} or {window_handle, automation_id/name}"
        )
    automation_id = target.get("automation_id")
    name = target.get("name")
    control_type = target.get("control_type")
    if not automation_id and not name:
        raise ClassifiedToolError(
            "reasoning_failure", "target needs automation_id or name to locate an element within window_handle"
        )
    return resolve_uia_element(window_handle, automation_id=automation_id, name=name, control_type=control_type)


def _require_confidence(element: ElementRef) -> None:
    policy = load_windows_control_policy()
    floor = policy.get("min_actuation_confidence", 0.70)
    if element.confidence < floor:
        raise ClassifiedToolError(
            "permission_denied",
            f"target resolved at confidence {element.confidence:.2f} (source={element.source}), "
            f"below this build's floor of {floor:.2f} — there is no confirmation channel to route a "
            "lower-confidence action through, so this is refused outright. Provide a more specific "
            "locator (automation_id) instead of raw coordinates.",
        )


# --- key combo parsing --------------------------------------------------------

_SPECIAL_KEYS = {
    "enter": "{ENTER}",
    "return": "{ENTER}",
    "esc": "{ESC}",
    "escape": "{ESC}",
    "tab": "{TAB}",
    "space": " ",
    "backspace": "{BACKSPACE}",
    "delete": "{DEL}",
    "del": "{DEL}",
    "home": "{HOME}",
    "end": "{END}",
    "up": "{UP}",
    "down": "{DOWN}",
    "left": "{LEFT}",
    "right": "{RIGHT}",
    "pageup": "{PGUP}",
    "pagedown": "{PGDN}",
    **{f"f{i}": f"{{F{i}}}" for i in range(1, 13)},
}
_MODIFIER_TOKENS = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+"}


def _canonical_combo(combo: str) -> str:
    parts = sorted(p.strip().lower() for p in combo.split("+") if p.strip())
    return "+".join(parts)


def _is_blocked_combo(combo: str) -> bool:
    policy = load_windows_control_policy()
    blocked = {_canonical_combo(c) for c in (policy.get("blocked_key_combos") or [])}
    return _canonical_combo(combo) in blocked


def _parse_key_combo(combo: str) -> str:
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ClassifiedToolError("reasoning_failure", f"empty key combo: {combo!r}")

    key = parts[-1]
    modifier_tokens = []
    for p in parts[:-1]:
        if p == "win":
            raise ClassifiedToolError(
                "reasoning_failure",
                "Win-key combinations are not supported by windows_key in this build "
                "— no reliable send_keys mapping exists yet",
            )
        if p not in _MODIFIER_TOKENS:
            raise ClassifiedToolError("reasoning_failure", f"unrecognized modifier {p!r} in {combo!r}")
        modifier_tokens.append(_MODIFIER_TOKENS[p])

    key_token = _SPECIAL_KEYS.get(key)
    if key_token is None:
        if len(key) == 1:
            key_token = key
        else:
            raise ClassifiedToolError(
                "reasoning_failure",
                f"unrecognized key {key!r} in {combo!r} — use a single character or a named key "
                "(enter, esc, tab, backspace, delete, home, end, arrows, f1-f12, ...)",
            )
    return "".join(modifier_tokens) + key_token


# --- clipboard helpers (typing via paste, not char-by-char keystrokes) ------
# automation_spikes/pywinauto_notepad_spike.py's finding: fast
# char-simulated typing (type_keys) gets corrupted by autocomplete/IME
# behavior on modern (MSIX-packaged) Windows apps. Clipboard paste doesn't
# have that problem. The clipboard's prior contents are restored
# afterward so this doesn't silently clobber whatever the user had copied.


def _get_clipboard_text_safe() -> Optional[str]:
    try:
        win32clipboard.OpenClipboard()
        try:
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def _set_clipboard_text(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


# --- tools --------------------------------------------------------------------


class GetForegroundWindowTool(BaseTool):
    """windows_get_foreground_window — returns the handle/title/process of
    whatever currently holds OS focus. The check every other tool in this
    server should be preceded by, so a task doesn't land input in a window
    it didn't intend to touch."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            raise ClassifiedToolError("state_failure", "no window currently has foreground focus")
        return window_snapshot(hwnd), Confidence.API_SUCCESS


class WaitTool(BaseTool):
    """windows_wait — polls a condition distinct from
    perception_wait_for_visual_change's pixel/UIA-diff wait: this one is
    about action COMPLETION (a window appearing, a process exiting), not
    passive observation. condition: {"type": "window_title"|
    "process_running"|"process_exited", "value": str}."""

    default_timeout_s = 90.0  # overridden per orbit/tools/CLAUDE.md: this
    # polls internally on its own deadline (capped well under this by the
    # caller-supplied `timeout` arg), so the outer wait_for needs headroom
    # beyond the 30s in-process default or a legitimate slow wait gets
    # reported as a generic timeout instead of this tool's own state_failure.

    _POLL_INTERVAL_S = 0.3
    _MAX_TIMEOUT_S = 60.0

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        condition = args["condition"]
        timeout = min(float(args.get("timeout", 10.0)), self._MAX_TIMEOUT_S)
        ctype = condition.get("type")
        value = condition.get("value")
        if ctype not in ("window_title", "process_running", "process_exited"):
            raise ClassifiedToolError(
                "reasoning_failure",
                f"unrecognized condition type {ctype!r} — must be window_title, "
                "process_running, or process_exited",
            )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            token.raise_if_cancelled()
            if ctype == "window_title" and find_window_by_title_substring(value) is not None:
                return {"satisfied": True, "condition": condition}, Confidence.API_SUCCESS
            if ctype == "process_running" and is_process_running(value):
                return {"satisfied": True, "condition": condition}, Confidence.API_SUCCESS
            if ctype == "process_exited" and not is_process_running(value):
                return {"satisfied": True, "condition": condition}, Confidence.API_SUCCESS
            await asyncio.sleep(self._POLL_INTERVAL_S)

        raise ClassifiedToolError(
            "state_failure", f"condition {condition!r} not satisfied within {timeout}s"
        )


class ScrollTool(BaseTool):
    """windows_scroll — vertical scroll only (see module docstring's scope
    note; horizontal scroll is unimplemented, not silently approximated).
    Scrolls at `target`'s resolved center if given, else at the foreground
    window's center."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        direction = args["direction"]
        if direction not in ("up", "down"):
            raise ClassifiedToolError(
                "reasoning_failure",
                f"direction {direction!r} is not supported — only 'up'/'down' "
                "(horizontal scroll is not implemented in this build)",
            )
        amount = int(args.get("amount", 3))
        target = args.get("target")

        if target:
            element = _resolve_click_target(target)
            _require_confidence(element)
            x, y = element.center()
        else:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                raise ClassifiedToolError("state_failure", "no foreground window to scroll")
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            x, y = (left + right) // 2, (top + bottom) // 2

        wheel_dist = amount if direction == "up" else -amount
        pywinauto_mouse.scroll(coords=(x, y), wheel_dist=wheel_dist)
        return {"direction": direction, "amount": amount}, Confidence.API_SUCCESS


class ClickTool(BaseTool):
    """windows_click — target is an ElementRef-shaped locator (preferred)
    or raw {x, y} (vision-tier only). Confidence below
    windows_control_policy.yaml's min_actuation_confidence is refused
    outright — see _require_confidence and the module docstring."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        element = _resolve_click_target(args["target"])
        _require_confidence(element)
        x, y = element.center()
        pywinauto_mouse.click(button="left", coords=(x, y))
        return {"clicked": element.model_dump()}, element.confidence


class DragTool(BaseTool):
    """windows_drag — click-drag-release between two resolved targets.
    Both endpoints are confidence-checked independently — dragging FROM a
    confidently-resolved element TO a guessed coordinate is exactly the
    "higher risk if EITHER endpoint is vision-tier" case the tool catalog
    calls out."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        from_element = _resolve_click_target(args["from_target"])
        to_element = _resolve_click_target(args["to_target"])
        _require_confidence(from_element)
        _require_confidence(to_element)

        fx, fy = from_element.center()
        tx, ty = to_element.center()
        pywinauto_mouse.press(button="left", coords=(fx, fy))
        pywinauto_mouse.move(coords=(tx, ty))
        pywinauto_mouse.release(button="left", coords=(tx, ty))
        return (
            {"from": from_element.model_dump(), "to": to_element.model_dump()},
            min(from_element.confidence, to_element.confidence),
        )


class TypeTool(BaseTool):
    """windows_type — types via clipboard paste (see module docstring),
    into `target` if given (focused first via UIA — same confidence
    gate as click, since focusing the wrong element is just as real a
    mistake as clicking the wrong one) or into whatever already has
    focus."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        text = args["text"]
        target = args.get("target")
        confidence = Confidence.API_SUCCESS

        if target:
            element = _resolve_click_target(target)
            _require_confidence(element)
            x, y = element.center()
            pywinauto_mouse.click(button="left", coords=(x, y))  # focus by clicking
            confidence = element.confidence

        previous = _get_clipboard_text_safe()
        try:
            _set_clipboard_text(text)
            pywinauto_keyboard.send_keys("^v", pause=0.05)
        finally:
            if previous is not None:
                _set_clipboard_text(previous)

        return {"typed_chars": len(text)}, confidence


class KeyTool(BaseTool):
    """windows_key — sends a key/key-combo to the foreground window.
    windows_control_policy.yaml's blocked_key_combos is checked first and
    wins outright (permission_denied) — fail-closed, no confirm channel to
    route a destructive combo through instead."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        combo = args["key_combo"]
        if _is_blocked_combo(combo):
            raise ClassifiedToolError(
                "permission_denied",
                f"{combo!r} is a blocked destructive/system key combination — refused "
                "outright, there is no confirmation channel to route it through instead",
            )
        send_keys_str = _parse_key_combo(combo)
        pywinauto_keyboard.send_keys(send_keys_str, pause=0.02)
        return {"sent": combo}, Confidence.API_SUCCESS


class OpenAppTool(BaseTool):
    """windows_open_app — launches a native application by path or
    registered name via os.startfile, the same shell resolution mechanism
    (App Paths registry, PATH, file associations) as double-clicking a
    shortcut. No allowlist, per design decision: any path/app name the
    model provides is launched — this is real code execution capability,
    kept at the tool catalog's plain medium tier without extra
    restriction. Fire-and-forget (ShellExecute) — no handle/pid is
    returned; pair with windows_wait(condition={"type": "process_running",
    ...}) to confirm the launch actually landed."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        target = args["app_name_or_path"]
        try:
            os.startfile(target)  # noqa: S606 — intentional: this IS the feature
        except OSError as exc:
            raise ClassifiedToolError("state_failure", f"could not launch {target!r}: {exc}") from exc
        return {"launched": target}, Confidence.API_SUCCESS


class FocusWindowTool(BaseTool):
    """windows_focus_window — brings a window to the foreground. Tiered
    'high' in risk_tiers.yaml and therefore unconditionally blocked by
    SafetyPlugin with confirmation_required: the tool catalog flags
    stealing OS focus mid-task as a real, unmitigated UX problem ("only
    run when idle / always ask first / show a countdown"), and nothing in
    this codebase builds any of those mitigations yet. Fully implemented,
    not a stub — ready once a real UX decision lands, same posture as
    fs_delete/email_send."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        window_handle = args["window_handle"]
        try:
            win32gui.ShowWindow(window_handle, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(window_handle)
        except Exception as exc:
            raise ClassifiedToolError(
                "state_failure", f"could not focus window {window_handle}: {exc}"
            ) from exc
        return window_snapshot(window_handle), Confidence.API_SUCCESS


def _metadata(name: str, description: str, **overrides) -> ToolMetadata:
    fields = {**_MEDIUM_FOREGROUND, **overrides}
    return ToolMetadata(name=name, description=description, **fields)


get_foreground_window_tool = GetForegroundWindowTool(
    _metadata(
        "windows_get_foreground_window",
        "Return the handle/title/process of whatever currently holds OS "
        "focus. Call this before other windows_* calls so input doesn't "
        "land in the wrong window.",
        risk_tier="low",
    )
)
wait_tool = WaitTool(
    _metadata(
        "windows_wait",
        "Wait for a window to appear (by title substring) or a process to "
        "start/exit, up to `timeout` seconds (capped at 60). "
        "condition: {type: window_title|process_running|process_exited, value: str}.",
        risk_tier="low",
    )
)
scroll_tool = ScrollTool(
    _metadata(
        "windows_scroll",
        "Scroll vertically ('up' or 'down' only) by `amount` notches, at "
        "`target`'s location if given, else at the foreground window's center.",
        risk_tier="low",
    )
)
click_tool = ClickTool(
    _metadata(
        "windows_click",
        "Click a UI element. target: {window_handle, automation_id} or "
        "{window_handle, name, control_type?} — resolved via the window's "
        "UI Automation tree. Raw {x, y} coordinates are accepted but "
        "always refused (no confirmation channel exists for vision-tier "
        "clicks) — always prefer a window_handle + locator.",
    )
)
drag_tool = DragTool(
    _metadata(
        "windows_drag",
        "Click-drag-release from one resolved target to another. Both "
        "ends need automation_id/name locators, same as windows_click — "
        "raw coordinates on either end will be refused.",
    )
)
type_tool = TypeTool(
    _metadata(
        "windows_type",
        "Type text via clipboard paste into `target` if given (focused "
        "first), else into whatever control already has focus. Restores "
        "the clipboard's prior contents afterward.",
    )
)
key_tool = KeyTool(
    _metadata(
        "windows_key",
        "Send a key or key combination to the foreground window, e.g. "
        "'Enter', 'Ctrl+S', 'F5'. A small set of destructive/system combos "
        "(Alt+F4, Ctrl+Alt+Delete, Win+L) is refused outright. Win-key "
        "combinations are not supported at all in this build.",
        is_destructive=True,
    )
)
open_app_tool = OpenAppTool(
    _metadata(
        "windows_open_app",
        "Launch a native application by path or registered name (same "
        "resolution as double-clicking a shortcut). No allowlist — any "
        "app the model names will be launched. Fire-and-forget; use "
        "windows_wait to confirm it actually started.",
        is_destructive=True,
    )
)
focus_window_tool = FocusWindowTool(
    _metadata(
        "windows_focus_window",
        "Bring a window to the foreground and give it input focus. "
        "Blocked pending a confirmation channel — stealing OS focus "
        "mid-task is a real UX problem this build has no mitigation for yet.",
        risk_tier="high",
        requires_confirmation=True,
        is_destructive=True,
    )
)
