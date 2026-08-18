"""Safety & permission layer — Section 7 of the architecture spec.

Implemented as an ADK Plugin using before_tool_callback / on_tool_error_callback
/ after_tool_callback — the concrete enforcement point Section 2 names for
this. Every tool call in the system passes through here.

Fail-safe defaults — two of them, both "don't do anything stupid" encoded
as code rather than comments:
  1. There is no confirmation UI wired up yet, so a "high" risk-tier tool
     is always blocked rather than auto-approved. When the GUI (task #13 /
     Section 14.3) grows a real confirm channel, wire it in here — do not
     change the default to auto-approve in the meantime.
  2. A tool name not explicitly catalogued in orbit/config/risk_tiers.yaml
     is a hard block, not a soft "medium" default. It used to fall through
     to tier='medium' (logged, but still executed), which is exactly the
     failure this exists to catch: research_product.py once connected the
     agent straight to raw Playwright MCP instead of the browser-policy
     proxy, and the raw server's tools happened to share this file's own
     tool names, so the soft default silently approved a set of tools this
     policy layer had never actually reviewed. Do not reintroduce a
     fallback tier for unregistered names — add them to risk_tiers.yaml
     instead, deliberately.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from orbit import db
from orbit.task_manager import TaskManager

logger = logging.getLogger("orbit.policy")

_CONFIG_DIR = Path(__file__).resolve().parent / "config"

RiskTier = str  # "low" | "medium" | "high"


def load_risk_tiers(path: Optional[Path] = None) -> dict[str, RiskTier]:
    path = path or _CONFIG_DIR / "risk_tiers.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    mapping: dict[str, RiskTier] = {}
    for tier in ("low", "medium", "high"):
        for tool_name in raw.get(tier, []) or []:
            mapping[tool_name] = tier
    return mapping


def load_tool_registry(path: Optional[Path] = None) -> set[str]:
    """The full set of tool names this build is allowed to call at all:
    every name with an explicit low/medium/high tier, plus `allowed:` for
    tools that are registered but don't need a tier override.
    before_tool_callback hard-blocks anything not in this set, before tier
    logic is even consulted — an uncatalogued tool reaching the model is a
    stronger problem than a catalogued one at the wrong tier, and the two
    checks are deliberately separate for that reason."""
    path = path or _CONFIG_DIR / "risk_tiers.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    registry: set[str] = set()
    for tier in ("low", "medium", "high"):
        registry.update(raw.get(tier, []) or [])
    registry.update(raw.get("allowed", []) or [])
    return registry


def load_chrome_profiles(path: Optional[Path] = None) -> dict[str, dict]:
    path = path or _CONFIG_DIR / "chrome_profiles.yaml"
    return yaml.safe_load(path.read_text()) or {}


def load_filesystem_policy(path: Optional[Path] = None) -> dict:
    """Section 7 filesystem scoping policy (orbit/config/filesystem_policy.yaml):
    allowed_roots, denylist_keywords, and the quarantine location/TTL for
    fs_delete. Read at call time, same as load_chrome_profiles — an edit
    takes effect on the next tool call, no restart needed."""
    path = path or _CONFIG_DIR / "filesystem_policy.yaml"
    return yaml.safe_load(path.read_text()) or {}


def load_windows_control_policy(path: Optional[Path] = None) -> dict:
    """Section 7 windows-control policy (orbit/config/windows_control_policy.yaml):
    the destructive key-combo denylist and the minimum ElementRef
    confidence windows_click/windows_drag will act on. Read at call time,
    same convention as every other loader here."""
    path = path or _CONFIG_DIR / "windows_control_policy.yaml"
    return yaml.safe_load(path.read_text()) or {}


def load_communication_policy(path: Optional[Path] = None) -> dict:
    """Section 7 communication policy (orbit/config/communication_policy.yaml):
    account_context -> account config, mirroring load_chrome_profiles'
    shape exactly. Read at call time, same convention as every other
    loader here."""
    path = path or _CONFIG_DIR / "communication_policy.yaml"
    return yaml.safe_load(path.read_text()) or {}


def resolve_profile(name: str, *, confirmed: bool = False) -> dict:
    """Section 7 policy resolver. Non-owner profiles are never auto-selected
    by inference — callers must pass confirmed=True, obtained from an
    explicit, named user instruction, not inferred from context."""
    profiles = load_chrome_profiles()
    if name not in profiles:
        raise ValueError(f"unknown chrome profile: {name!r}")
    profile = profiles[name]
    if profile.get("owner_confirmation_required") and not confirmed:
        raise PermissionError(
            f"profile {name!r} requires explicit owner confirmation before "
            "any action — this must come from a direct, named user "
            "instruction, never inferred"
        )
    return profile


def confidence_gate(confidence: float) -> str:
    """Section 7 confidence-based execution gating. Delegates to
    orbit.tools.foundation.Confidence.gate — single source of truth now
    that the Prompt 0 foundation contract owns the scoring constants too."""
    from orbit.tools.foundation import Confidence

    return Confidence.gate(confidence)


def classify_failure(error: Exception) -> str:
    """Classifier for the TRANSPORT/PROTOCOL failure path only — a real
    Python exception that propagated up through ADK/MCPToolset itself:
    server subprocess crash, stdio timeout, malformed MCP response. This is
    on_tool_error_callback's territory.

    It is NOT used for an MCP tool that ran, caught its own error inside
    BaseTool.execute, and returned a normal (non-exception) JSON response
    shaped like {"error": kind, "message": ...}. That is the common case in
    this system — confirmed empirically that on_tool_error_callback never
    fires for it, since from ADK's point of view the call succeeded — and
    it is handled entirely by after_tool_callback's
    _extract_structured_failure, which reads the server's own
    already-computed ErrorKind directly off the wire. Re-running this
    function's string heuristic against that JSON's message text would be
    re-guessing information that already crossed the process boundary as
    structured data. Do not widen this function's use to cover that case.

    Precedence within this (transport-only) path: structured information
    first, string matching only as a last resort for exceptions that don't
    know what they are. ClassifiedToolError (orbit.tools.foundation) can in
    principle reach here too (e.g. a future in-process, non-MCP tool that
    raises one directly instead of going through BaseTool.execute's own
    catch), so it's checked first and its .kind is returned as-is, never
    re-guessed from its message.

    The keyword heuristic itself is deliberately ordered specific-before-
    generic: state_failure keywords are checked before the transport/
    timeout bucket. They used to be checked after, and the transport
    bucket used to contain a bare "5" (meant to catch 5xx status codes)
    that matched the digit anywhere in the message — "element 5 not found"
    classified as tool_failure (blind retry) instead of state_failure
    (re-observe), because a stray "5" beat "not found" to the first branch.
    Given HTTP codes, element indexes, and timestamps, that was a large
    fraction of real errors landing in the wrong bucket. Fixed by requiring
    a precise 5xx match and checking the more specific bucket first.
    """
    from orbit.tools.foundation import ClassifiedToolError

    if isinstance(error, ClassifiedToolError):
        return error.kind

    msg = str(error).lower()
    if any(k in msg for k in ("not found", "stale", "detached", "changed", "no longer")):
        return "state_failure"  # re-observe before acting again
    if re.search(r"\b5\d{2}\b", msg) or any(
        k in msg for k in ("timeout", "connection", "network", "rate limit", "429")
    ):
        return "tool_failure"  # retry within cap
    return "reasoning_failure"  # re-plan, don't re-execute the same steps


_KNOWN_ERROR_KINDS = {
    "tool_failure",
    "state_failure",
    "reasoning_failure",
    "permission_denied",
    "cancelled",
    "timeout",
}

# Per-classification override of the retry cap. Absent entries fall back to
# SafetyPlugin.retry_cap (2). The grouping principle: cap=1 for
# classifications where a second attempt at the EXACT SAME call is
# pointless by definition —
#   - permission_denied: a deterministic policy block (e.g. a blocklisted
#     URL). The environment did not change; retrying guarantees the same
#     refusal for zero chance of success.
#   - reasoning_failure: Section 7's own guidance for this classification
#     is "re-plan, don't re-execute the same steps" — a second identical
#     call means that guidance was ignored, not followed.
#   - cancelled: the task itself is being torn down; there is no
#     "next planning step" to retry into.
# tool_failure/state_failure/timeout keep the default cap of 2 because
# they're genuinely transient/environmental — a network blip or a stale
# element reference might legitimately succeed on a second attempt.
_CAP_OVERRIDE: dict[str, int] = {
    "permission_denied": 1,
    "reasoning_failure": 1,
    "cancelled": 1,
}


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
    ) -> None:
        super().__init__(name)
        self.task_manager = task_manager
        self.risk_tiers = risk_tiers if risk_tiers is not None else load_risk_tiers()
        self.tool_registry = tool_registry if tool_registry is not None else load_tool_registry()
        self.default_tier = default_tier
        self.retry_cap = retry_cap
        self._consecutive_failures: dict[tuple[str, str], int] = {}

    def _tier_for(self, tool_name: str) -> RiskTier:
        return self.risk_tiers.get(tool_name, self.default_tier)

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
