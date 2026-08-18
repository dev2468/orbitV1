"""ResearchProduct skill — Section 6 example, promoted from the spike.

Skills compose atomic tools; the orchestrator matches intent to a skill, not
to a pile of raw tools. The tool_filter here is the enforced allowlist the
skill's `tools:` line in Section 6 describes — confirmed necessary by the
Section 2 spike (Playwright MCP's full ~24-tool surface degrades tool-calling
reliability on mid-tier models).
"""

from __future__ import annotations

import sys

from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

SKILL_META = {
    "skill": "ResearchProduct",
    "description": (
        "Search retailers for a product, compare prices and offers, "
        "return a ranked summary."
    ),
    "inputs": ["category", "budget", "preferences"],
    "lane": "headless",
    "risk_tier": "low",
}


def build_toolset(task_id: str = "") -> MCPToolset:
    """Connect to the browser-policy server, NOT raw Playwright MCP.

    This previously pointed straight at `npx @playwright/mcp@latest`, which
    silently bypassed the entire policy layer — URL scheme allowlist,
    sensitive-category blocklist, non-owner Chrome profile exclusion, and
    <untrusted_web_content> wrapping were all dead code at runtime. The bug
    was well disguised: raw Playwright exposes tools with the SAME NAMES the
    proxy uses (browser_navigate, browser_snapshot), which are also the
    names in config/risk_tiers.yaml, so SafetyPlugin found tier 'low',
    approved, and wrote a perfectly normal-looking event row. A clean event
    log is NOT evidence the policy ran — the load-bearing proof is the
    <untrusted_web_content marker in snapshot output.

    sys.executable, not a bare "python": the venv is not on PATH here, so a
    bare "python" resolves to the wrong interpreter or nothing at all.
    """
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "orbit.mcp_servers.browser_policy_server"],
                # Stamp the owning task into the server's environment so it
                # can bind each browser session to that task and reap the
                # session when the task reaches a terminal state. Passing
                # this as a tool ARGUMENT instead would make teardown
                # depend on the model remembering to thread it through —
                # exactly the model-dependence this is removing. `env` is
                # merged with the default environment by the MCP SDK, so
                # PATH (and therefore npx) still resolves.
                env={"ORBIT_TASK_ID": task_id} if task_id else None,
            ),
            # Longer than the old 30s: this server spawns its own Playwright
            # subprocess per browser_open, so startup is a process launch
            # behind a process launch.
            timeout=60,
        ),
        # Surface area still matters — the reason the old filter existed
        # (a large raw tool list measurably degrades tool-calling on
        # mid-tier models) applies to the proxy too. browser_extract is held
        # back for the same reason browser_evaluate was: it takes a raw JS
        # expression and models reach for it reflexively when the snapshot
        # already has the answer.
        #
        # browser_close is deliberately NOT offered either. It stays
        # implemented and callable internally, but measured runs had the
        # model skipping it 2 times out of 4 — so it was never a teardown
        # guarantee, only an occasional shortcut, while still costing tool
        # surface area. Teardown is now structural: the server's reaper
        # closes sessions on task-terminal and on idle.
        tool_filter=[
            "browser_open",
            "browser_navigate",
            "browser_snapshot",
        ],
    )
