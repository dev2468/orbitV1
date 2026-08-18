"""Memory skill — gives the agent access to its own task history.

Mirrors research_product.py's connection pattern exactly: spawn the memory
MCP server via sys.executable (not a bare "python" — the venv is not on
PATH here), stamp the owning task_id into the subprocess's environment so
memory reads/writes are attributable to the real task rather than landing
on the shared adhoc row, and filter the exposed tool surface for the same
tool-calling-reliability reason established in Fix 1.
"""

from __future__ import annotations

import sys

from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters


def build_toolset(task_id: str = "") -> MCPToolset:
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "orbit.mcp_servers.memory_server"],
                # Verified this is the mechanism that actually works: MCP's
                # stdio_client does NOT inherit the parent process's full
                # os.environ — get_default_environment() applies a curated
                # safelist (PATH, APPDATA, etc.) that excludes arbitrary
                # custom vars. Setting os.environ["ORBIT_TASK_ID"] in the
                # parent alone would silently do nothing; it has to be
                # passed explicitly here, merged on top of that safelist.
                env={"ORBIT_TASK_ID": task_id} if task_id else None,
            ),
            timeout=30,
        ),
        # memory_get_task is deliberately left out: it's mainly useful once
        # the model already has a task_id in hand (e.g. drilling into one
        # search result), and every additional tool costs selection
        # reliability on mid-tier models — the same reasoning that trimmed
        # Playwright's ~24 tools down to 2 in Fix 1.
        tool_filter=[
            "memory_search_tasks",
            "memory_get_context",
            "memory_get_policy",
            "memory_write",
        ],
    )
