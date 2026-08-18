"""Filesystem skill — gives the agent scoped read/write access to its own
sandbox directory (orbit/config/filesystem_policy.yaml's allowed_roots).

Mirrors memory.py's connection pattern exactly: spawn the filesystem MCP
server via sys.executable (not a bare "python" — the venv is not on PATH
here), stamp the owning task_id into the subprocess's environment, and
filter the exposed tool surface for the same tool-calling-reliability
reason established in Fix 1 / research_product.py.
"""

from __future__ import annotations

import sys

from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

SKILL_META = {
    "skill": "Filesystem",
    "description": (
        "Read, write, search, and organize files inside Orbit's own "
        "sandboxed workspace directory."
    ),
    "lane": "headless",
    "risk_tier": "medium",  # highest tier actually reachable through the
    # tool_filter below; fs_delete (tier high) is registered but excluded,
    # see the note on tool_filter.
}


def build_toolset(task_id: str = "") -> MCPToolset:
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "orbit.mcp_servers.filesystem_server"],
                # Same mechanism as research_product.py/memory.py: MCP's
                # stdio_client does not inherit the parent's os.environ, so
                # task_id has to be passed explicitly here rather than set
                # on os.environ in the parent process.
                env={"ORBIT_TASK_ID": task_id} if task_id else None,
            ),
            timeout=30,  # this server only touches the local filesystem —
            # no subprocess-behind-a-subprocess like browser-policy, so the
            # shorter memory-server-style timeout applies.
        ),
        # fs_delete is deliberately NOT exposed here even though it is
        # implemented and registered (risk_tiers.yaml: high). Every call
        # would hit SafetyPlugin's unconditional high-tier block and return
        # confirmation_required — exposing it anyway would just cost tool-
        # selection surface area for a call that can never succeed today,
        # the same reasoning that held browser_close and browser_extract
        # back from research_product's tool_filter. Add it here once a real
        # confirm channel exists to make it reachable.
        tool_filter=[
            "fs_list_dir",
            "fs_read_file",
            "fs_write_file",
            "fs_move",
            "fs_copy",
            "fs_search",
            "fs_create_dir",
            "fs_get_metadata",
        ],
    )
