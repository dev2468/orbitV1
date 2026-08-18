"""Provider degradation — Section 14.4 (P0).

Deliberately narrow scope: detect a primary-provider call failure and turn
it into a plain-language degraded-mode message instead of a raw crash.
Automatic multi-provider failover (silently retrying on a second provider)
is explicitly called out in Section 14.4 as a separate, larger decision —
not required for MVP, not built here. What ships here is the required
floor: never let a model-provider outage surface as an unhandled
traceback to the user.
"""

from __future__ import annotations

from orbit import db


def graceful_degradation_message(task_id: str, error: Exception) -> str:
    """The tool name in this message must actually be reachable by the
    agent, or a degraded-mode message just hands the user a dead end
    instead of a working next step.

    It previously said "query via search_task_history" — that's the name
    of the underlying orbit.db function, not a tool the model (or a human
    re-running it) can call; the agent had no memory tools wired in at all
    at the time this was written, so the claim was false regardless of
    wording. Now that orbit/skills/memory.py exists, memory_search_tasks
    is the actual reachable tool — but note this message is returned when
    the MODEL PROVIDER itself is down, so the model can't run that tool
    either. What's true is narrower: the data is safe and the user (or a
    fresh run once the provider recovers) can get at it.
    """
    still_available = db.list_tasks()
    completed = [t for t in still_available if t["status"] == "COMPLETED"]
    return (
        "I can't think right now — the model provider call failed "
        f"({type(error).__name__}: {error}). "
        "I can't run any tools until this clears, including looking up past "
        "results myself, but nothing local is lost: " + (
            f"{len(completed)} previously completed task(s) and their "
            "results are saved. Once the provider is back, ask me and I'll "
            "check memory_search_tasks before redoing any work."
            if completed
            else "your task history will be searchable via "
            "memory_search_tasks once you have some completed tasks and "
            "the provider is back."
        )
    )
