"""Safety & permission policy — Section 7 of the architecture spec.

**This module is the policy DATA: the YAML readers, the risk-tier vocabulary,
and failure classification. The enforcement point that uses them is
`SafetyPlugin`, which lives in `orbit/safety_plugin.py`.**

They were one file until 2026-09-08. Splitting them was a layering fix with a
large incidental payoff: thirteen call sites import the readers — every MCP
server, the GUI, `confirmation.py` — and none of them wants an ADK plugin,
because ADK runs in the parent process and a tool server has no agent to
police. Yet importing this module dragged in `google.adk`, measured at **1.96s
of a 2.04s** import, paid by six MCP servers in parallel on every task and by
the GUI at startup.

Nothing here imports ADK now. **Keep it that way** — if something in this file
starts needing `google.adk`, it belongs in `safety_plugin.py` instead.

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

from orbit import db

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


def load_perception_policy(path: Optional[Path] = None) -> dict:
    """screen-perception policy (orbit/config/perception_policy.yaml):
    candidate-generation geometry filters and the OmniParser fallback's
    hosting mode. Read at call time, same convention as every other loader
    here.

    Note this is NOT a permission boundary — screen-perception's tools are
    all pure reads and there is nothing here to scope or deny. It governs
    the shape of candidate generation only; see the file's own header."""
    path = path or _CONFIG_DIR / "perception_policy.yaml"
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
