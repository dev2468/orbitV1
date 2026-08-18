"""Integration entry point: ties db + TaskManager + SafetyPlugin + agent
together into one real run. This is the thing that proves the pieces built
in this session actually work as a system, not just in isolation.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from google.adk.runners import InMemoryRunner

from orbit import db
from orbit.agent import build_agent
from orbit.degradation import graceful_degradation_message
from orbit.policy import SafetyPlugin
from orbit.task_manager import TaskManager


def _extract_final_text(events: list) -> str:
    texts: list[str] = []
    for ev in events:
        if ev.is_final_response() and ev.content and ev.content.parts:
            for part in ev.content.parts:
                if getattr(part, "text", None):
                    texts.append(part.text)
    return "\n".join(texts) if texts else "(no final text response)"


async def run_task(
    title: str, goal: str, *, lane: str = "headless", risk_tier: str = "low"
) -> dict[str, Any]:
    db.init_db()
    tm = TaskManager()
    task_id = db.create_task(title, goal=goal, lane=lane, risk_tier=risk_tier)

    # Belt-and-suspenders, not the load-bearing mechanism: verified by
    # reading mcp.client.stdio.get_default_environment() that stdio_client
    # subprocesses do NOT inherit the parent's full os.environ — it applies
    # a curated safelist (PATH, APPDATA, etc.) that excludes arbitrary
    # custom vars. So setting this alone would silently fail to reach the
    # memory/browser-policy server subprocesses. What actually works is the
    # explicit env={"ORBIT_TASK_ID": task_id} each skill's build_toolset()
    # passes into its own StdioServerParameters (orbit/skills/memory.py,
    # orbit/skills/research_product.py) — set here too for consistency and
    # for any future code path that does a plain subprocess inherit.
    os.environ["ORBIT_TASK_ID"] = task_id

    plugin = SafetyPlugin(task_manager=tm)
    # lane is threaded into build_agent too, not just TaskManager.submit
    # below — it's what decides whether the agent even has the
    # windows-control toolset. See build_agent's docstring (orbit/agent.py)
    # for why that gate has to live there rather than being trusted to the
    # model.
    agent = build_agent(task_id=task_id, lane=lane)

    async def work(token) -> str:
        runner = InMemoryRunner(agent=agent, plugins=[plugin])
        try:
            await runner.session_service.create_session(
                app_name=runner.app_name,
                user_id="orbit_user",
                session_id=task_id,
                state={"orbit_task_id": task_id},
            )
            events = await runner.run_debug(
                goal, user_id="orbit_user", session_id=task_id, quiet=True
            )
            return _extract_final_text(events)
        finally:
            # Each run spawns its own Playwright MCP subprocess (its own
            # Chrome profile lock). Without an explicit close, a leaked
            # subprocess from one task collides with the next one's browser
            # ("browser is already in use") — found by the eval harness.
            await runner.close()

    aio_task = await tm.submit(task_id, lane, work)
    try:
        result = await aio_task
        return {"task_id": task_id, "status": "COMPLETED", "result": result}
    except Exception as exc:  # noqa: BLE001 — this is the top-level degradation catch
        friendly = graceful_degradation_message(task_id, exc)
        return {
            "task_id": task_id,
            "status": "FAILED",
            "result": friendly,
            "raw_error": str(exc),
        }


DEMO_GOAL = (
    "Go to https://example.com and tell me the exact page title and "
    "the first sentence of body text you see."
)


def _main() -> int:
    # Model output routinely contains non-ASCII (em-dashes, curly quotes,
    # accented names). The Windows console's default codepage renders those
    # as mojibake, so force UTF-8 on the output streams.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # non-reconfigurable stream (piped/redirected) — harmless

    if "--list-models" in sys.argv:
        from orbit.agent import DEFAULT_MODEL, KNOWN_MODELS

        current = os.environ.get("ORBIT_MODEL") or DEFAULT_MODEL
        print("\nKnown-good models (all verified to support tool calling):\n")
        for name, note in KNOWN_MODELS.items():
            marker = "*" if name == current else " "
            print(f" {marker} {name}\n     {note}\n")
        print(f"Active: {current}")
        print("Change it by setting ORBIT_MODEL in .env\n")
        return 0

    # --foreground opts into lane="foreground", the ONLY way the agent gets
    # the windows-control toolset at all (orbit/agent.py's build_agent).
    # Without it every task runs headless, same as before this flag
    # existed — this is additive, not a behavior change for existing
    # callers. Stripped out of argv before the rest is joined into the
    # goal string so it doesn't leak into the task title/goal text.
    args = [a for a in sys.argv[1:] if a != "--foreground"]
    foreground = "--foreground" in sys.argv[1:]
    lane = "foreground" if foreground else "headless"

    # Everything after the module name (minus --foreground) is the goal,
    # so quoting is optional:
    #   python -m orbit.run_task find the cheapest 65 inch tv
    #   python -m orbit.run_task --foreground open notepad and type hello
    goal = " ".join(args).strip() or DEMO_GOAL
    title = (goal[:60] + "...") if len(goal) > 60 else goal

    print(f"\n> {goal}\n  (working — this takes ~15-45s{', foreground lane' if foreground else ''})\n")
    outcome = asyncio.run(run_task(title, goal, lane=lane))

    print("-" * 60)
    if outcome["status"] == "COMPLETED":
        print(outcome["result"])
    else:
        print(f"FAILED: {outcome['result']}")
    print("-" * 60)
    print(f"task_id: {outcome['task_id']}  (visible in the GUI dashboard)\n")
    return 0 if outcome["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
