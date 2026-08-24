"""Windows-control skill — real mouse/keyboard actuation on the user's
machine. Mirrors the other skills' MCPToolset wiring exactly, but with one
load-bearing difference: `orbit/agent.py` only adds this toolset to the
agent's tools when the owning task was submitted under lane="foreground".

That gating is NOT optional and is not enforced here — a skill module has
no way to refuse being added to an agent's tool list. It lives in
build_agent() because orbit/task_manager.py's foreground lock is what
actually serializes input-simulating tasks against each other (Section 9:
"one mouse and one keyboard"), and a task running in the headless lane
(TaskManager's Semaphore(5)) provides no such serialization. Handing a
headless-lane agent these tools would let up to 5 concurrent tasks each
try to drive the mouse at once — see orbit/CLAUDE.md's lane-gating note
for the full reasoning.
"""

from __future__ import annotations

import sys

from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

SKILL_META = {
    "skill": "WindowsControl",
    "description": (
        "Click, type, scroll, and drag inside native Windows applications "
        "via UI Automation; launch apps; wait on windows/processes."
    ),
    "lane": "foreground",
    "risk_tier": "medium",  # windows_focus_window (high) is registered but
    # excluded from tool_filter below — see the note there.
}


def build_toolset(task_id: str = "") -> MCPToolset:
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "orbit.mcp_servers.windows_control_server"],
                env={"ORBIT_TASK_ID": task_id} if task_id else None,
            ),
            timeout=30,  # no subprocess-behind-a-subprocess here; same
            # timeout class as memory/filesystem, not browser-policy's 60s.
        ),
        # windows_focus_window is deliberately NOT exposed even though it's
        # implemented and registered (risk_tiers.yaml: high). Every call
        # would hit SafetyPlugin's unconditional high-tier block and return
        # confirmation_required — same reasoning that holds fs_delete back
        # from the filesystem skill's tool_filter.
        tool_filter=[
            "windows_get_foreground_window",
            "windows_wait",
            "windows_scroll",
            "windows_click",
            "windows_drag",
            "windows_type",
            "windows_key",
            "windows_open_app",
            "windows_clipboard_copy_image",
        ],
    )
