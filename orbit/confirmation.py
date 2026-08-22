"""Human-in-the-loop confirmation for actions the confidence gate refuses.

WHY THIS LIVES IN THE PARENT PROCESS
------------------------------------
The obvious place to ask "are you sure?" would be inside `windows_click`.
It cannot go there: every tool runs inside an MCP server **subprocess**
whose stdin/stdout ARE the MCP protocol transport. A server that tried to
`input()` would read framed JSON-RPC, not a human, and would corrupt the
channel doing it.

So the interception happens in `SafetyPlugin.before_tool_callback`, which
already sees every tool call, already runs in the parent process, and
therefore still has a real console attached. The tool itself stays honest:
it re-validates the token independently (`_require_confidence`) rather than
trusting that anyone upstream did the asking.

WHAT A CONFIRMATION DOES AND DOES NOT DO
-----------------------------------------
Approving does **not** raise the element's confidence, and does not lower
`min_actuation_confidence`. A vision-sourced ElementRef stays scored at
`Confidence.VISION_INFERRED` (0.50) forever. What approval grants is a
one-shot, short-lived, single-use capability to perform *one specific
action* that was shown to a human. The floor is untouched; a second click
needs a second yes.

FAIL CLOSED
-----------
If there is no interactive console — a scheduled run, a piped stdin, the
GUI-only case — the answer is **no**, automatically, and the row is
recorded REJECTED. The alternative would be a build where an unattended
process silently self-approves guessed clicks, which is precisely the
outcome the confidence floor exists to prevent.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

from orbit import db
from orbit.policy import load_windows_control_policy

# Only these two actuate a *position*, which is the thing a visual guess can
# get wrong in a way a human looking at a box can catch. Deliberately not a
# blanket rule over every windows-control tool: `windows_type` sends
# keystrokes to whatever already has focus, so a box on a screenshot tells a
# human nothing useful about it.
CONFIRMABLE_TOOLS = {"windows_click", "windows_drag"}

# The dashboard repaints on a 2s timer, so polling faster than that only
# burns cycles waiting for a UI that has not redrawn yet.
_GUI_POLL_INTERVAL_S = 0.5


def _floor() -> float:
    return float(load_windows_control_policy().get("min_actuation_confidence", 0.70))


def _ttl_seconds() -> int:
    """Approval lifetime, from windows_control_policy.yaml.

    Short by design: an approval is consent about a *screenshot of a
    moment*. Windows move, dialogs open, focus changes. A token that
    outlived the screen it was granted against would aim an approved click
    at whatever happens to be there now.
    """
    return int(load_windows_control_policy().get("approval_token_ttl_seconds", 120))


def target_needs_confirmation(tool_name: str, tool_args: dict) -> Optional[dict]:
    """Return the target dict when this call would be refused by the gate.

    Reads the confidence the CALLER supplied rather than re-resolving the
    element: this runs in the parent process, which has no window handles
    and no business doing UIA work. The tool re-checks for real on the other
    side of the process boundary — this is a "should we ask?" test, not the
    security boundary.
    """
    if tool_name not in CONFIRMABLE_TOOLS:
        return None
    if tool_args.get("approval_token"):
        return None

    # windows_drag has two endpoints and either one being uncertain is
    # enough to ask about — the catalog's "higher risk if EITHER endpoint is
    # vision-tier" rule. One yes still covers the whole drag, because a drag
    # is one action; the token is consumed once on the far side.
    targets = [
        tool_args.get("target"),
        tool_args.get("from_target"),
        tool_args.get("to_target"),
    ]
    for target in targets:
        if not isinstance(target, dict):
            continue
        # A raw {x, y} is scored VISION_INFERRED by _resolve_click_target no
        # matter where the numbers came from, so it needs asking too.
        if "x" in target and "y" in target and "confidence" not in target:
            return target
        confidence = target.get("confidence")
        if confidence is not None and float(confidence) < _floor():
            return target
    return None


def describe_target(target: dict) -> str:
    """One line a human can actually judge. Names the source explicitly,
    because "the model guessed this" and "UIA resolved this" deserve
    different levels of trust from the person answering."""
    name = target.get("name") or target.get("element_id") or "unnamed element"
    source = target.get("source") or ("raw coordinates" if "x" in target else "unknown")
    bounds = target.get("bounds")
    where = (
        f"box {tuple(bounds)}"
        if bounds
        else f"point ({target.get('x')}, {target.get('y')})"
    )
    confidence = target.get("confidence")
    conf = f", confidence {float(confidence):.2f}" if confidence is not None else ""
    return f"{name!r} at {where} (source={source}{conf})"


def _gui_wait_seconds() -> float:
    """How long to wait for the GUI dashboard to answer, when there is no
    console to ask on.

    DEFAULT 0 — the GUI approval channel is opt-in, and that default is
    deliberate rather than timid. A non-zero wait makes every unattended run
    (eval, CI, a scheduled task) block for that long on each confirmation
    before failing closed, which turns a fast honest refusal into a hang.
    Set it to a real number when someone is actually watching the dashboard.
    """
    return float(load_windows_control_policy().get("approval_gui_wait_seconds", 0))


def _wait_for_gui_resolution(confirmation_id: str, timeout_s: float) -> Optional[str]:
    """Poll the confirmation row until a human resolves it in the GUI.

    Polling rather than any push mechanism because the dashboard runs in a
    DIFFERENT PROCESS and SQLite is the only thing the two share — exactly
    the reason the dashboard itself polls for tasks. The row is the channel.

    Returns the token on approval, None on rejection or timeout.
    """
    if timeout_s <= 0:
        return None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = db.get_pending_confirmation(confirmation_id)
        if row is None or row["status"] == "PENDING":
            time.sleep(_GUI_POLL_INTERVAL_S)
            continue
        return row["approval_token"] if row["status"] == "APPROVED" else None
    return None


def _console_is_interactive() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask_console(action: str, description: str) -> bool:
    """Block on an explicit y/n. Anything that is not an affirmative is a
    no — including EOF, an interrupt, and a bare Enter. There is no default
    yes."""
    print()
    print("  " + "=" * 68)
    print("  CONFIRMATION REQUIRED")
    print(f"  Orbit wants to run: {action}")
    print(f"  Target: {description}")
    print("  This target is below the automatic-actuation confidence floor.")
    print("  " + "=" * 68)
    try:
        answer = input("  Allow this one action? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  -> no answer, treating as NO")
        return False
    return answer in {"y", "yes"}


def request_confirmation(
    task_id: str,
    tool_name: str,
    tool_args: dict,
    target: dict,
    *,
    screenshot_path: Optional[str] = None,
    prompt: Optional[Any] = None,
) -> tuple[Optional[str], str]:
    """Record the request, ask a human, and return (token, confirmation_id).

    The row is written BEFORE anyone is asked, and is resolved either way.
    That ordering is what makes the audit trail complete: a refusal leaves a
    REJECTED row, an unattended run leaves a REJECTED row, and a crash
    mid-prompt leaves a PENDING row that a human can still see — rather than
    all three being indistinguishable from "never happened".

    `prompt` is injectable so tests can drive the decision without a TTY;
    it defaults to the real console asker.
    """
    description = describe_target(target)
    confirmation_id = db.create_pending_confirmation(
        tool_name,
        tool_args,
        task_id=task_id,
        screenshot_path=screenshot_path,
        candidate_box=tuple(target["bounds"]) if target.get("bounds") else None,
        candidate_label=description,
    )

    asker = prompt
    if asker is None:
        if not _console_is_interactive():
            # No console. The GUI dashboard may still be watching this table,
            # so wait for it if a wait is configured — that is the Phase 5
            # channel. When it is not configured, or nobody answers in time,
            # fail closed: an unattended process must never self-approve.
            token = _wait_for_gui_resolution(confirmation_id, _gui_wait_seconds())
            if token:
                db.log_event(
                    task_id,
                    tool_call=tool_name,
                    args={"confirmation_id": confirmation_id, "channel": "gui"},
                    result="confirmation_approved",
                )
                return token, confirmation_id

            # Only close the row ourselves if nobody else did — a GUI
            # rejection has already written REJECTED, and re-resolving would
            # raise.
            if db.get_pending_confirmation(confirmation_id)["status"] == "PENDING":
                db.resolve_pending_confirmation(confirmation_id, approved=False)
            db.log_event(
                task_id,
                tool_call=tool_name,
                args={"confirmation_id": confirmation_id},
                error="confirmation_denied: no interactive console, and no approval arrived",
            )
            return None, confirmation_id
        asker = _ask_console

    approved = bool(asker(tool_name, description))
    token = db.resolve_pending_confirmation(
        confirmation_id, approved=approved, ttl_seconds=_ttl_seconds()
    )

    db.log_event(
        task_id,
        tool_call=tool_name,
        args={"confirmation_id": confirmation_id, "target": description},
        result="confirmation_approved" if approved else None,
        error=None if approved else "confirmation_denied: user answered no",
    )
    return token, confirmation_id
