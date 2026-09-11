"""The ADK enforcement plugin — Section 7 of the architecture spec.

Split out of `orbit/policy.py` on 2026-09-08, for a reason that is about
layering first and startup cost second.

`orbit/policy.py` holds two unrelated things: pure YAML readers (`load_*`) and
this class, which is an ADK `BasePlugin`. Thirteen call sites import the
readers — every MCP server, the GUI, `confirmation.py` — and **not one of them
wants the plugin**. ADK runs in the parent process; a tool server has no
agent to police. They were importing a plugin they could never use.

The cost of that was not theoretical. `import google.adk` measured **1.96s of
a 2.04s** import for `orbit.mcp_servers.memory_tools`, and six MCP servers pay
it in parallel on every single task. The GUI paid it at startup too, for one
`load_windows_control_policy` call.

So: readers stay in `policy.py` (no import site changes), the plugin moved
here (four import sites changed — `run_task.py` and three test modules).

**Every tool call in the system still passes through this class.** Nothing
about invariant 2 changed; only which file the class lives in.

Fail-safe defaults, unchanged and load-bearing:
  1. There is no auto-approval for a "high" risk-tier tool. It is blocked.
  2. A tool name not catalogued in `orbit/config/risk_tiers.yaml` is a hard
     block, not a soft "medium" default — see `policy.py`'s docstring for the
     incident that rule exists because of.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from orbit import db
from orbit.policy import (
    _CAP_OVERRIDE,
    _KNOWN_ERROR_KINDS,
    RiskTier,
    classify_failure,
    load_risk_tiers,
    load_tool_registry,
)
from orbit.task_manager import TaskManager

logger = logging.getLogger("orbit.policy")


class SafetyPlugin(BasePlugin):
    def __init__(
        self,
        *,
        task_manager: Optional[TaskManager] = None,
        risk_tiers: Optional[dict[str, RiskTier]] = None,
        tool_registry: Optional[set[str]] = None,
        default_tier: RiskTier = "medium",
        retry_cap: int = 2,
        name: str = "orbit_safety",
        keep_full_results: int = 3,
        stale_result_char_limit: int = 2000,
    ) -> None:
        super().__init__(name)
        self.task_manager = task_manager
        self.risk_tiers = risk_tiers if risk_tiers is not None else load_risk_tiers()
        self.tool_registry = tool_registry if tool_registry is not None else load_tool_registry()
        self.default_tier = default_tier
        self.retry_cap = retry_cap
        self._consecutive_failures: dict[tuple[str, str], int] = {}
        self.keep_full_results = keep_full_results
        self.stale_result_char_limit = stale_result_char_limit

    def _tier_for(self, tool_name: str) -> RiskTier:
        return self.risk_tiers.get(tool_name, self.default_tier)

    async def before_model_callback(self, *, callback_context, llm_request):
        """Compact stale bulk out of the conversation before it is re-sent.

        An LLM API is stateless: every turn re-sends the whole conversation,
        so a large tool result is not billed once but on every remaining turn
        of the task. A 40-turn task that read three big pages early pays for
        those three pages roughly 37 more times.

        What this drops is narrow on purpose. Only `function_response`
        payloads are touched — never user text, never the model's own
        reasoning, never a tool's ARGUMENTS (which are small and are the
        record of what was already tried, so dropping them invites the model
        to repeat itself). The most recent `keep_full_results` results are
        always left intact, because those are what the model is actively
        reasoning about; only older ones over `stale_result_char_limit` are
        replaced.

        The replacement is an explicit note naming the tool and the size,
        not a silent deletion. That distinction is the whole design: a model
        that can see something was elided will re-call the tool when it
        genuinely needs the detail, whereas a model shown a gap it cannot
        account for tends to confabulate over it.

        INTERACTION WITH PROMPT CACHING (orbit/agent.py's cache breakpoint):
        these two do not stack cleanly, and it is worth knowing why rather
        than assuming they multiply. Caching pays off only when the prefix is
        byte-identical to the previous turn; eliding a result rewrites the
        middle of the history and invalidates the cache from that point. In
        practice a new elision happens on only some turns — most tool results
        never cross the size limit — so those turns take a cache miss and the
        rest still hit. The trade is worth it either way: removing a 155,000-
        token payload outright beats paying 0.25x to keep re-sending it.
        Compaction runs first (here, ADK-side) and caching marks the already-
        compacted history, which is the correct order.

        Returning None always — this mutates the request and never
        short-circuits the model call.
        """
        try:
            self._compact_history(llm_request)
        except Exception:  # noqa: BLE001
            # A context optimisation must never be able to fail a task.
            logger.debug("history compaction skipped", exc_info=True)
        return None

    def _compact_history(self, llm_request: Any) -> None:
        contents = getattr(llm_request, "contents", None) or []

        # Walk newest-first so "the last N results" is counted correctly,
        # then stub anything older and oversized.
        seen = 0
        for content in reversed(contents):
            for part in reversed(getattr(content, "parts", None) or []):
                fr = getattr(part, "function_response", None)
                if fr is None:
                    continue
                seen += 1
                if seen <= self.keep_full_results:
                    continue
                response = getattr(fr, "response", None)
                if response is None:
                    continue
                try:
                    size = len(json.dumps(response, default=str))
                except (TypeError, ValueError):
                    size = len(str(response))
                if size <= self.stale_result_char_limit:
                    continue
                fr.response = {
                    "elided": (
                        f"This {getattr(fr, 'name', 'tool')} result "
                        f"({size:,} characters) was removed from the "
                        "conversation to save context. It succeeded at the "
                        "time. Call the tool again if you still need it."
                    )
                }

    @staticmethod
    def _extract_structured_failure(result: Any) -> Optional[tuple[str, str]]:
        """Returns (kind, message) if `result` — the raw MCP CallToolResult
        envelope ADK hands to after_tool_callback, e.g.
        {'content': [{'type': 'text', 'text': '...'}], 'isError': False} —
        represents a failure the SERVER already classified. Returns None
        for a genuine success.

        This is where failure detection actually has to happen for MCP
        tools. Confirmed empirically (two live browser_navigate calls
        against a blocklisted URL): BaseTool.execute (orbit/tools/
        foundation.py) catches its own exceptions and returns a normal,
        successful JSON-RPC response — {"error": kind, "message": ...} as
        DATA, with isError left False (see browser_policy_server.py /
        memory_server.py's _payload()). From ADK's point of view the call
        succeeded, so on_tool_error_callback never fires for it. This
        method is the only place in the system that sees these failures.

        Deliberately conservative: only a dict with EXACTLY {"error",
        "message"} keys and a recognized ErrorKind counts as a failure.
        Anything else — a string (browser_snapshot's wrapped page text
        fails json.loads and is correctly treated as success), a dict
        shaped like a real success payload, unparsable content — is
        treated as success. A false negative here just means a real
        failure doesn't trip the cap (the old, already-known gap); a false
        positive would make the cap fire on legitimate results, which is
        worse, so the match is narrow on purpose.
        """
        if not isinstance(result, dict):
            return None

        if result.get("isError"):
            # A genuine MCP-protocol-level error — an exception that
            # escaped even the server's own tool wrapper, not one of our
            # tools' handled failures. No structured kind is available for
            # this case; classify it generically.
            content = result.get("content") or []
            text = content[0].get("text") if content and isinstance(content[0], dict) else None
            return ("tool_failure", text or "MCP protocol-level error (isError=true)")

        content = result.get("content")
        if not isinstance(content, list) or not content:
            return None
        first = content[0]
        text = first.get("text") if isinstance(first, dict) else None
        if not isinstance(text, str):
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if (
            isinstance(parsed, dict)
            and set(parsed.keys()) == {"error", "message"}
            and parsed["error"] in _KNOWN_ERROR_KINDS
        ):
            return (parsed["error"], parsed["message"])
        return None

    def _record_failure(
        self, task_id: str, tool_name: str, classification: str, message: str
    ) -> Optional[dict[str, Any]]:
        """Shared by after_tool_callback (structured, MCP-server failures)
        and on_tool_error_callback (transport-level exceptions) — both are
        consecutive failures of the SAME tool for retry-cap purposes, and
        must share one counter and one cap decision regardless of which
        path detected them."""
        key = (task_id, tool_name)
        count = self._consecutive_failures.get(key, 0) + 1
        self._consecutive_failures[key] = count
        cap = _CAP_OVERRIDE.get(classification, self.retry_cap)

        if count >= cap:
            self._consecutive_failures.pop(key, None)
            logger.error(
                "retry cap hit for %s on task %s after %d attempt(s) (%s, cap=%d): %s",
                tool_name, task_id, count, classification, cap, message,
            )
            return {
                "error": "retry_cap_exceeded",
                "classification": classification,
                "message": (
                    f"'{tool_name}' failed {count} time(s) in a row "
                    f"({classification}). Stopping retries per the retry cap "
                    f"(cap={cap} for this classification) — surface this "
                    "plainly to the user rather than continuing."
                ),
            }
        # Under the cap: let it propagate so the agent's normal retry logic
        # (or escalation, Section 5) handles it.
        return None

    def _task_id(self, tool_context: ToolContext) -> str:
        task_id = tool_context.state.get("orbit_task_id")
        if task_id:
            return task_id
        # No task was created upstream (e.g. a tool called outside
        # run_task.py's normal flow). events.task_id has a FK to tasks, so
        # log_event would raise IntegrityError against a bare session id —
        # materialize a real (if minimal) task row instead of assuming one
        # exists. Found via the Prompt 0 test suite, not by inspection.
        task_id = f"adhoc-{tool_context.session.id}"
        if db.get_task(task_id) is None:
            db.create_task("adhoc session task", task_id=task_id)
        return task_id

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> Optional[dict[str, Any]]:
        task_id = self._task_id(tool_context)

        # Cancellation: hard-checked before every tool call (Section 9 —
        # "build the token check into the tool layer itself").
        if self.task_manager is not None:
            token = self.task_manager.token_for(task_id)
            if token is not None and token.is_cancelled():
                return {"error": "task_cancelled", "message": "Task was cancelled."}

        # Registry check, ahead of and separate from tier logic: a tool
        # name not explicitly catalogued in risk_tiers.yaml is a hard
        # block, full stop — it does not fall through to a tier lookup at
        # all (there is no "default tier" for an unregistered tool; the
        # old soft default of tier='medium' for unlisted names is exactly
        # what let a set of tools reach the model without this policy
        # layer ever having reviewed them — see the module docstring for
        # the incident). Logged at WARNING, not ERROR: an unregistered
        # tool showing up is a configuration gap to notice loudly, not
        # (necessarily) an active attack.
        if tool.name not in self.tool_registry:
            logger.warning(
                "blocked unregistered tool %r (task %s) — not present in "
                "risk_tiers.yaml's low/medium/high/allowed lists",
                tool.name, task_id,
            )
            db.log_event(task_id, tool_call=tool.name, args=tool_args, error="blocked: tool not registered")
            return {
                "error": "tool_not_registered",
                "message": (
                    f"'{tool.name}' is not on the approved tool registry "
                    "(orbit/config/risk_tiers.yaml). This is a hard block, "
                    "not a retryable error — surface this to the user "
                    "rather than retrying or trying different arguments."
                ),
            }

        tier = self._tier_for(tool.name)
        if tier == "high":
            logger.warning("blocked high-risk tool %s (task %s) — no confirm channel wired", tool.name, task_id)
            db.log_event(task_id, tool_call=tool.name, args=tool_args, error="blocked: high-risk, needs confirmation")
            return {
                "error": "confirmation_required",
                "message": (
                    f"'{tool.name}' is a high-risk action and requires explicit "
                    "user confirmation. No confirmation channel is wired up in "
                    "this build yet, so it is blocked rather than auto-approved. "
                    "Surface this to the user instead of retrying."
                ),
            }

        # Human-in-the-loop confirmation for a position-actuating call whose
        # target sits below the actuation floor (a vision guess, or a raw
        # {x, y}). This is the ONLY path by which such a call can proceed,
        # and it proceeds by carrying a one-shot approval token — never by
        # the floor moving or the element's confidence changing.
        #
        # It happens here rather than inside windows_click because every
        # tool runs in an MCP server subprocess whose stdin IS the protocol
        # transport; a server cannot ask a human anything. See
        # orbit/confirmation.py. The tool still re-validates the token
        # itself, so this is the asking, not the enforcing.
        from orbit import confirmation

        pending_target = confirmation.target_needs_confirmation(tool.name, tool_args)
        if pending_target is not None:
            token, confirmation_id = confirmation.request_confirmation(
                task_id, tool.name, tool_args, pending_target
            )
            if not token:
                logger.info("confirmation denied for %s (task %s)", tool.name, task_id)
                return {
                    "error": "permission_denied",
                    "message": (
                        f"'{tool.name}' was not approved by the user "
                        f"(confirmation {confirmation_id}). Do not retry this action "
                        "and do not try to route around it — tell the user it was "
                        "declined and stop."
                    ),
                }
            # Injected into the args the tool actually receives. Mutating in
            # place is deliberate: ADK passes this dict through to the tool.
            tool_args["approval_token"] = token

        # Stamp the OWNING task onto every call, so a server's own event rows
        # are attributed correctly no matter how long that server process has
        # been alive.
        #
        # Each MCP server also reads ORBIT_TASK_ID from its environment, but
        # that is baked in at spawn and is therefore only correct while a
        # server lives exactly as long as one task. This is the mechanism that
        # does not depend on that — and it is what lets a server be reused
        # across the turns of a conversation (see run_task's runner cache).
        #
        # Every tool in orbit/mcp_servers/ already declares `task_id: str = ""`
        # for exactly this purpose. It was previously only fillable by the
        # model, which — as memory_tools.py notes — it reliably will not do.
        #
        # Mutating in place is deliberate and already the established pattern
        # here; see the approval_token injection above.
        if "task_id" not in tool_args:
            tool_args["task_id"] = task_id

        # medium/low: allowed, logged (Section 7: "Logged, no confirm").
        #
        # EVENT WRITE SITE 1 of 3. A single MCP tool call currently produces
        # three event rows: this one (client-side, args), the after_tool
        # write below (client-side, result), and BaseTool.execute's write
        # inside the MCP server process (full ToolResult envelope). That is
        # redundant and is scheduled as Fix 7 — deliberately not addressed
        # here. Until then: never assert on event row COUNTS, only on row
        # CONTENT (which tool, which args, which error), so tests stay
        # correct on both sides of that fix.
        db.log_event(task_id, tool_call=tool.name, args=tool_args)
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Fix 8: this is where MCP-server-side failures are actually
        detected. Confirmed empirically that on_tool_error_callback never
        fires for a tool that ran, caught its own error, and returned a
        normal (non-exception) JSON response — that response arrives HERE.
        Previously this method unconditionally cleared the failure
        counter, which combined with on_tool_error_callback almost never
        firing meant the retry cap could never trip on the live path — a
        model that looped on the same failing tool call was never stopped.
        """
        task_id = self._task_id(tool_context)
        # EVENT WRITE SITE 2 of 3 — see the note at write site 1 in
        # before_tool_callback. The other two are that one and
        # BaseTool.execute in orbit/tools/foundation.py. Scheduled as Fix 7;
        # assert on row content, never row counts.
        db.log_event(task_id, tool_call=tool.name, result=result)

        failure = self._extract_structured_failure(result)
        if failure is None:
            self._consecutive_failures.pop((task_id, tool.name), None)
            return None

        classification, message = failure
        return self._record_failure(task_id, tool.name, classification, message)

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> Optional[dict[str, Any]]:
        """Transport/protocol-level failures ONLY — a real Python exception
        that propagated up through ADK/MCPToolset itself (server subprocess
        crash, stdio timeout, malformed MCP response). An MCP tool that ran
        and returned a normal failure-shaped JSON response does NOT reach
        here; that is after_tool_callback's job (see
        _extract_structured_failure). Confirmed empirically: two live
        permission_denied browser_navigate calls produced zero invocations
        of this method.

        Shares _record_failure's counter/cap with after_tool_callback so a
        tool that fails once via each path still counts as two consecutive
        failures of that tool, not one of each forgiven independently.
        """
        task_id = self._task_id(tool_context)
        classification = classify_failure(error)
        db.log_event(task_id, tool_call=tool.name, error=f"{classification}: {error}")
        return self._record_failure(task_id, tool.name, classification, str(error))
