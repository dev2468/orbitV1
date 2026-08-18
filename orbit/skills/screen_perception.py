"""Screen-perception skill — read-only observation of the real screen/UI
state. Mirrors memory.py's/filesystem.py's/communication.py's connection
pattern exactly.

lane="headless" even though it inspects native windows: every tool here
only READS (a screenshot, a UIA tree, foreground-window info) — nothing
simulates input, so none of it needs orbit/task_manager.py's foreground
lock. That's the entire point of Section 11's "perception free and
always-on, actuation gated" split, and it's why this skill (unlike
windows_control.py) needs no lane gating in orbit/agent.py.
"""

from __future__ import annotations

import sys

from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

SKILL_META = {
    "skill": "ScreenPerception",
    "description": (
        "Read-only observation of the real screen: foreground window/process, "
        "a window's UI Automation tree, element resolution, screenshots, and "
        "waiting for visual change."
    ),
    "lane": "headless",
    "risk_tier": "low",
}


def build_toolset(task_id: str = "") -> MCPToolset:
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "orbit.mcp_servers.perception_server"],
                env={"ORBIT_TASK_ID": task_id} if task_id else None,
            ),
            timeout=30,
        ),
        tool_filter=[
            "perception_get_state",
            "perception_get_uia_tree",
            "perception_find_element",
            "perception_capture_screenshot",
            "perception_wait_for_visual_change",
        ],
    )
