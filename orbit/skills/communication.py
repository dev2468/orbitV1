"""Communication skill — email/calendar access against whatever backend
orbit/config/communication_policy.yaml resolves for an account (today:
only LocalMailBackend, an honestly-labeled local stand-in — see
orbit/mcp_servers/communication_backend.py's docstring).

Mirrors memory.py's/filesystem.py's connection pattern exactly: spawn the
communication MCP server via sys.executable, stamp the owning task_id into
the subprocess's environment, filter the exposed tool surface for the same
tool-calling-reliability reason established in Fix 1.

Unlike windows_control.py, this skill needs NO lane gating — it never
simulates OS input, so it's wired into orbit/agent.py unconditionally, the
same as memory/filesystem.
"""

from __future__ import annotations

import sys

from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

SKILL_META = {
    "skill": "Communication",
    "description": (
        "Draft, search, and read email; list and create calendar events, "
        "against a resolved account context."
    ),
    "lane": "headless",
    "risk_tier": "medium",  # email_send (high) is registered but excluded
    # from tool_filter below — see the note there.
}


def build_toolset(task_id: str = "") -> MCPToolset:
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "orbit.mcp_servers.communication_server"],
                env={"ORBIT_TASK_ID": task_id} if task_id else None,
            ),
            timeout=30,  # no subprocess-behind-a-subprocess here; same
            # timeout class as memory/filesystem, not browser-policy's 60s.
        ),
        # email_send is deliberately NOT exposed even though it's
        # implemented and registered (risk_tiers.yaml: high). Every call
        # would hit SafetyPlugin's unconditional high-tier block AND the
        # tool's own unconditional refusal (communication_tools.py's
        # EmailSendTool) — exposing it anyway would cost tool-selection
        # surface area for a call that can never succeed, the same
        # reasoning that holds fs_delete/windows_focus_window back from
        # their skills' tool_filters.
        tool_filter=[
            "email_draft",
            "email_search",
            "email_read",
            "email_list_threads",
            "calendar_list_events",
            "calendar_create_event",
        ],
    )
