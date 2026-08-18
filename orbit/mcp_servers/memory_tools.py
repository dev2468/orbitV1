"""Tool implementations for the `memory` MCP server — Prompt 3 of the MCP
tool layer doc. Kept separate from memory_server.py (the FastMCP process
entry point) so these are directly unit-testable without going through the
MCP stdio transport.

Built on the Prompt 0 foundation (orbit.tools.foundation.BaseTool):
every call here is wrapped, timed, cancellation-checked, and logged to the
same events table as the rest of the system.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from orbit import db
from orbit.policy import load_chrome_profiles
from orbit.task_manager import CancellationToken
from orbit.tools.foundation import BaseTool, ClassifiedToolError, Confidence, ToolMetadata

_LOW_HEADLESS = dict(
    risk_tier="low",
    lane="headless",
    requires_confirmation=False,
    is_destructive=False,
    # Retrieved memories are OUR data structurally (Prompt 3) — but any row
    # tagged provenance="external" still gets wrapped in an inline
    # <untrusted_external_content> marker by _tag_if_external, so taint
    # survives even though this static per-tool flag is False.
    returns_untrusted_content=False,
)

# Deliberately named "adhoc" and materialized lazily, same pattern as
# policy.py's SafetyPlugin fallback: an MCP server subprocess has no direct
# view of the orchestrator's ADK session/task_id, so a task_id is accepted
# as an explicit tool argument. When the caller doesn't have one yet (e.g.
# a memory lookup that isn't itself part of a tracked task), fall back to
# one stable adhoc row rather than leaving events orphaned or crashing on
# the tasks FK.
_FALLBACK_TASK_ID = "adhoc-memory-server"

# The orchestrator stamps the real task_id into this server's environment
# when it spawns it (via StdioServerParameters(env=...) in orbit/agent.py) —
# same mechanism as browser_policy_tools.py's _ORBIT_TASK_ID. This is what
# makes memory writes/reads attributable to the real task without depending
# on the model remembering to pass task_id as a tool argument, which it
# reliably will not.
_ORBIT_TASK_ID = os.environ.get("ORBIT_TASK_ID", "").strip()


def _resolve_task_id(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    if _ORBIT_TASK_ID:
        return _ORBIT_TASK_ID
    if db.get_task(_FALLBACK_TASK_ID) is None:
        db.create_task("memory server adhoc calls", task_id=_FALLBACK_TASK_ID)
    return _FALLBACK_TASK_ID


def _tag_if_external(row: dict) -> str:
    """Provenance must survive retrieval (Prompt 3 requirement): a memory
    row written with provenance='external' stays wrapped as untrusted
    content no matter how long it's been in our own DB."""
    content = row.get("content", "")
    if row.get("provenance") == "external":
        return f'<untrusted_external_content task_id="{row.get("task_id")}">{content}</untrusted_external_content>'
    return content


class SearchTasksTool(BaseTool):
    """memory_search_tasks — FTS5 keyword search over tasks (title/goal/
    result), not the memory table. memory_type/project are accepted for
    forward-compat with the prompt's declared signature but are currently
    no-ops: the tasks table has no type/project dimension in this schema
    version. Documented here rather than silently implying they filter."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        query = args["query"]
        limit = args.get("limit", 10)
        rows = db.search_tasks_fts(query, limit=limit)
        results = [
            {
                "task_id": r["task_id"],
                "title": r["title"],
                "goal": r["goal"],
                "result_summary": (r["result"] or "")[:280],
                "completed_at": r["completed_at"],
                "source_urls": r["source_urls"],
                "relevance": r.get("relevance"),
            }
            for r in rows
        ]
        return results, Confidence.API_SUCCESS


class GetTaskTool(BaseTool):
    """memory_get_task — full task record, events off by default (verbose)."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        task_id = args["task_id"]
        include_events = args.get("include_events", False)
        task = db.get_task(task_id)
        if task is None:
            raise ValueError(f"no such task: {task_id!r}")
        if include_events:
            with db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM events WHERE task_id = ? ORDER BY timestamp",
                    (task_id,),
                ).fetchall()
            events = []
            for r in rows:
                e = dict(r)
                for key in ("args", "result"):
                    if e.get(key) and len(e[key]) > 500:
                        e[key] = e[key][:500] + "...(truncated)"
                events.append(e)
            task = {**task, "events": events}
        return task, Confidence.API_SUCCESS


class WriteMemoryTool(BaseTool):
    """memory_write — validated write, no arbitrary schema. Description
    (surfaced to the model via metadata) must state what belongs in each
    memory_type per the prompt: episodic = something that happened at a
    point in time; semantic = a durable fact about the user/setup;
    procedural = how a task is performed; project = knowledge scoped to a
    named project."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        memory_id = db.add_memory(
            args["content"],
            type=args["memory_type"],  # db.add_memory raises ValueError on
            # an invalid type/provenance -> becomes ToolError(kind="tool_failure")
            # via BaseTool's generic except clause. The model cannot
            # succeed in writing an out-of-schema row.
            task_id=args.get("task_id"),
            project=args.get("project"),
            provenance=args.get("provenance", "system"),
        )
        return {"memory_id": memory_id}, Confidence.API_SUCCESS


class GetContextTool(BaseTool):
    """memory_get_context — the Context Resolver. Assembles only what's
    relevant, inside a token budget (approximated at ~4 chars/token — no
    tokenizer dependency for this), most-recent-first, always carrying
    source task_ids so claims stay traceable."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        query = args["query"]
        budget_tokens = args.get("budget_tokens", 2000)
        budget_chars = budget_tokens * 4

        rows = db.search_task_history(query)
        assembled: list[str] = []
        source_task_ids: list[str] = []
        used = 0
        for row in rows:
            body = _tag_if_external(row)
            snippet = f"[{row['type']}] {body}"
            if used + len(snippet) > budget_chars:
                break
            assembled.append(snippet)
            used += len(snippet)
            if row.get("task_id") and row["task_id"] not in source_task_ids:
                source_task_ids.append(row["task_id"])

        return (
            {
                "context": "\n".join(assembled),
                "source_task_ids": source_task_ids,
                "budget_tokens": budget_tokens,
                "chars_used": used,
            },
            Confidence.API_SUCCESS,
        )


class GetPolicyTool(BaseTool):
    """memory_get_policy — a READ of deterministic config (Section 7
    chrome_profiles.yaml). The model asks which profile applies to a
    context; it does not get to invent the answer.

    Found during the Prompt 8 adversarial review carrying the exact same
    bug already found and fixed once in browser_open (OpenSessionTool,
    browser_policy_tools.py) — the fix was never mirrored to this sibling
    tool. Confirmed live: memory_get_policy(context="mom") returned the
    "dev" profile's config with no refusal at all (this tool had NO
    owner-check whatsoever, not even the gap version — a plain silent
    pass-through), and memory_get_policy(context="totally_bogus_xyz") also
    silently resolved to "dev". Both are now closed the same way as
    browser_open: an explicit refusal for a family-member match, and an
    explicit error (never a silent default) for a context matching
    nothing at all."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        context = args["context"]
        profiles = load_chrome_profiles()

        matched_name = next(
            (n for n, c in profiles.items() if context in (c.get("contexts") or [])), None
        ) or (context if context in profiles else None)

        if matched_name is not None and profiles[matched_name].get("owner_confirmation_required"):
            raise ClassifiedToolError(
                "permission_denied",
                f"context {context!r} resolves to a non-owner profile ({matched_name!r}) "
                "— never reachable through this tool, under any context",
            )

        if matched_name is None:
            raise ClassifiedToolError(
                "reasoning_failure",
                f"context {context!r} is not a recognized context — check "
                "chrome_profiles.yaml for valid context names rather than guessing",
            )

        resolved = {"profile": matched_name, **profiles[matched_name]}
        return resolved, Confidence.API_SUCCESS


def _metadata(name: str, description: str, **overrides) -> ToolMetadata:
    fields = {**_LOW_HEADLESS, **overrides}
    return ToolMetadata(name=name, description=description, **fields)


search_tasks_tool = SearchTasksTool(
    _metadata(
        "memory_search_tasks",
        "Search past completed tasks by keyword (FTS5 over title/goal/result). "
        "Use this BEFORE redoing research — it's how 'what did we already find "
        "out' gets answered without re-browsing. Do NOT use for looking up "
        "durable facts about the user (use memory_get_context instead).",
    )
)
get_task_tool = GetTaskTool(
    _metadata(
        "memory_get_task",
        "Fetch one task's full record by task_id. Set include_events=True only "
        "when you need the detailed tool-call trail, not just the result — "
        "events are verbose.",
    )
)
write_memory_tool = WriteMemoryTool(
    _metadata(
        "memory_write",
        "Write a memory row. memory_type must be exactly one of: episodic "
        "(something that happened at a point in time), semantic (a durable "
        "fact about the user/setup), procedural (how a task is performed), "
        "or project (knowledge scoped to a named project). Choosing the "
        "wrong type silently degrades future retrieval — think before writing.",
    )
)
get_context_tool = GetContextTool(
    _metadata(
        "memory_get_context",
        "Assemble a token-budgeted context block of relevant past memories "
        "for a query. Use this instead of memory_search_tasks when you want "
        "durable facts/procedures, not a list of past tasks. Never dumps full "
        "history — respects budget_tokens.",
    )
)
get_policy_tool = GetPolicyTool(
    _metadata(
        "memory_get_policy",
        "Resolve which Chrome profile/policy applies to a named context "
        "(e.g. 'personal', 'college'). This is a config lookup, not something "
        "to guess — always call this rather than assuming a profile.",
    )
)
