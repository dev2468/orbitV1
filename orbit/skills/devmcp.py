"""Dev-MCP skill — wraps the user's external MCP server at
C:\\Users\\HP\\Desktop\\MCP\\server.py, which provides local filesystem
access (list/read/write) and sandboxed PowerShell command execution.

That server has its own venv (Python 3.14) and its own security layer
(command allowlist, write-path restrictions, dangerous-pattern blocking),
so we proxy it as-is rather than reimplementing its tools.
"""

from __future__ import annotations

from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

SKILL_META = {
    "skill": "DevMCP",
    "description": (
        "Local machine access: list files in any folder, read any file "
        "(txt/py/pdf/docx/xlsx/pptx/images), write to allowed paths, "
        "and run sandboxed PowerShell commands."
    ),
    "lane": "headless",
    "risk_tier": "medium",
}

_SERVER_PYTHON = r"C:\Users\HP\Desktop\MCP\venv\Scripts\python.exe"
_SERVER_SCRIPT = r"C:\Users\HP\Desktop\MCP\venv\..\server.py"


def build_toolset(task_id: str = "") -> MCPToolset:
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=_SERVER_PYTHON,
                args=[_SERVER_SCRIPT],
                env={"ORBIT_TASK_ID": task_id} if task_id else None,
            ),
            timeout=120,  # run_command can take >30s (pip, python scripts);
            # timeout controls BOTH connection and per-request read timeout
            # for stdio connections (session_context.py:318), so 30s is too
            # short for any non-trivial command.
        ),
        tool_filter=[
            "list_files",
            "read_file",
            "write_file",
            "run_command",
        ],
    )
