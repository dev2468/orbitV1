"""Dev-MCP skill — local machine access: list/read/write files anywhere the
policy allows, plus sandboxed PowerShell.

**This is now an in-repo server** (`orbit.mcp_servers.devmcp_server`), as of
2026-09-08. It used to spawn `C:\\Users\\HP\\Desktop\\MCP\\server.py` — a
server that was not in this repository, ran on its own Python 3.14 venv, and
existed only on the author's machine — while being loaded by *every* task in
*both* lanes. A fresh clone therefore got a toolset it could not start, and it
was the single largest obstacle to anyone else running Orbit.

Vendoring it also fixed a security hole: the external module's command
allowlist was disabled outright (`def is_command_allowed(...): return True, ""`),
so `run_command` ran arbitrary PowerShell despite the project's docs claiming
otherwise. See `orbit/mcp_servers/devmcp_tools.py` and
`orbit/config/devmcp_policy.yaml`.

`ORBIT_DEVMCP_EXTERNAL=1` restores the old behaviour, for comparing against
the original. It is deliberately opt-in and off by default — the external
server has no policy layer.
"""

from __future__ import annotations

import os
import sys

from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

SKILL_META = {
    "skill": "DevMCP",
    "description": (
        "Local machine access: list files in any folder, read text files, "
        "write to allowed paths, and run policy-checked PowerShell commands."
    ),
    "lane": "headless",
    "risk_tier": "medium",
}

# Only consulted when ORBIT_DEVMCP_EXTERNAL is set.
_EXTERNAL_PYTHON = r"C:\Users\HP\Desktop\MCP\venv\Scripts\python.exe"
_EXTERNAL_SCRIPT = r"C:\Users\HP\Desktop\MCP\server.py"


def _external_paths() -> tuple[str, str]:
    return (
        os.environ.get("DEVMCP_PYTHON", _EXTERNAL_PYTHON),
        os.environ.get("DEVMCP_SCRIPT", _EXTERNAL_SCRIPT),
    )


def build_toolset(task_id: str = "") -> MCPToolset:
    if os.environ.get("ORBIT_DEVMCP_EXTERNAL"):
        python, script = _external_paths()
        command, args = python, [script]
    else:
        command, args = sys.executable, ["-m", "orbit.mcp_servers.devmcp_server"]

    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=command,
                args=args,
                # MCP's stdio_client does not inherit the parent's full
                # os.environ — it applies a curated safelist — so task_id has
                # to be passed explicitly here. Same mechanism as every other
                # skill in this package.
                env={"ORBIT_TASK_ID": task_id} if task_id else None,
            ),
            # run_command's own ceiling is 120s (devmcp_policy.yaml). This
            # timeout governs BOTH connection and per-request read for stdio
            # connections, so it has to clear that with room to spare or a
            # long-but-legal command is killed by the transport instead of by
            # the tool, losing the tool's own error classification.
            timeout=180,
        ),
        tool_filter=[
            "list_files",
            "read_file",
            "write_file",
            "run_command",
        ],
    )
