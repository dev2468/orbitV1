"""Foundation contract — Prompt 0 of "Claude Code Prompts - Building the
MCP Tool Layer.md". Not a tool itself: the base class, error envelope, and
safety metadata every real tool (Prompts 1-6) inherits.

Reuses orbit.db's events table (Section 10) as the event store rather than
building a second one, and orbit.task_manager.CancellationToken as the
cancellation primitive rather than a second cancellation type — one
cancellation story for the whole system, not two that can drift apart.
"""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from orbit import db
from orbit.task_manager import CancellationToken

RiskTier = Literal["low", "medium", "high"]
Lane = Literal["headless", "foreground"]
ErrorKind = Literal[
    "tool_failure",
    "state_failure",
    "reasoning_failure",
    "permission_denied",
    "cancelled",
    "timeout",
]


class ToolMetadata(BaseModel):
    name: str
    description: str  # written for an LLM: what it does, when to use it, when NOT to
    risk_tier: RiskTier
    lane: Lane
    requires_confirmation: bool
    is_destructive: bool
    returns_untrusted_content: bool

    @model_validator(mode="after")
    def _high_tier_must_confirm(self) -> "ToolMetadata":
        if self.risk_tier == "high" and not self.requires_confirmation:
            raise ValueError(
                f"tool {self.name!r} is risk_tier='high' but "
                "requires_confirmation=False — every high-tier tool must "
                "require confirmation. This is enforced here so it can't be "
                "an author oversight."
            )
        return self


class ToolError(BaseModel):
    kind: ErrorKind  # required — no default. See module docstring: this
    # field is what the retry/recovery system routes on (Section 7), so an
    # author cannot construct a ToolError without deciding it.
    message: str
    retryable: bool
    details: dict = Field(default_factory=dict)


class ClassifiedToolError(Exception):
    """Raise this from within a tool's `run` when the author already knows
    the correct ErrorKind (e.g. permission_denied for a policy block,
    state_failure for a page that navigated underneath us) — without it,
    BaseTool.execute's catch-all can only ever produce kind='tool_failure',
    which collapses genuinely different failure modes into one bucket and
    defeats Section 7's failure-routing (tool_failure retries, state_failure
    re-observes, reasoning_failure re-plans)."""

    def __init__(
        self, kind: ErrorKind, message: str, *, retryable: bool = False, details: Optional[dict] = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.details = details or {}


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: Optional[ToolError] = None
    confidence: Optional[float] = None
    untrusted: bool = False
    duration_ms: int = 0
    event_id: str = ""


class Confidence:
    """Section 7's confidence-based execution gating, scored by how an
    action was grounded."""

    API_SUCCESS = 1.0
    UIA_AUTOMATION_ID = 0.95
    UIA_NAME_MATCH = 0.80
    OCR_MATCH = 0.60
    VISION_INFERRED = 0.50

    @staticmethod
    def gate(confidence: float) -> Literal["execute", "reverify", "surface"]:
        if confidence > 0.90:
            return "execute"
        if confidence >= 0.70:
            return "reverify"
        return "surface"


# --- secret redaction -------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|auth)", re.IGNORECASE
)
_SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"gsk_[A-Za-z0-9]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    # Deepgram keys have no fixed prefix — lowercase hex, no dashes. The
    # docs' own example ("1234567890abcdef1234567890abcdef") is 32 chars,
    # but a real key obtained from the user during this build was 40 chars
    # — doc examples aren't guaranteed representative, so this matches
    # either length rather than trusting the shorter one. This shape also
    # matches e.g. a bare MD5/SHA-1 hash; over-redacting a stray hash is an
    # acceptable cost for not missing a real key that leaked outside a
    # suspiciously-named field (the _SECRET_KEY_RE check above only catches
    # the latter).
    re.compile(r"\b[0-9a-f]{32,40}\b"),
]
_REDACTED = "***REDACTED***"


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _SECRET_KEY_RE.search(str(k)) else redact_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if isinstance(value, str):
        redacted = value
        for pat in _SECRET_VALUE_PATTERNS:
            redacted = pat.sub(_REDACTED, redacted)
        return redacted
    return value


class BaseTool(ABC):
    """Every real tool subclasses this and implements `run`. `execute` is
    the public entry point and is NOT overridden — it's what guarantees the
    try/except, timeout, cancellation check, and event logging happen
    uniformly, regardless of what an individual tool author remembers."""

    default_timeout_s: float = 30.0

    def __init__(self, metadata: ToolMetadata, *, timeout_s: Optional[float] = None):
        self.metadata = metadata
        self.timeout_s = timeout_s if timeout_s is not None else self.default_timeout_s

    @abstractmethod
    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        """Subclasses implement tool logic here. Return (data, confidence).
        confidence may be None for tools where grounding doesn't apply
        (e.g. a pure API call already covered by ok=True/False).
        Long-running implementations must check `token.raise_if_cancelled()`
        between internal await points, not just rely on the outer timeout."""
        raise NotImplementedError

    async def execute(
        self, args: dict, *, task_id: str, token: Optional[CancellationToken] = None
    ) -> ToolResult:
        token = token or CancellationToken()
        start = time.monotonic()

        if token.is_cancelled():
            return await self._finish(
                task_id, args, start,
                error=ToolError(kind="cancelled", message="Cancelled before start.", retryable=False),
            )

        try:
            data, confidence = await asyncio.wait_for(
                self.run(args, token), timeout=self.timeout_s
            )
            return await self._finish(task_id, args, start, data=data, confidence=confidence)
        except asyncio.TimeoutError:
            return await self._finish(
                task_id, args, start,
                error=ToolError(
                    kind="timeout",
                    message=f"'{self.metadata.name}' exceeded its {self.timeout_s}s timeout.",
                    retryable=True,
                ),
            )
        except asyncio.CancelledError:
            return await self._finish(
                task_id, args, start,
                error=ToolError(kind="cancelled", message="Cancelled mid-execution.", retryable=False),
            )
        except ClassifiedToolError as exc:
            return await self._finish(
                task_id, args, start,
                error=ToolError(kind=exc.kind, message=str(exc), retryable=exc.retryable, details=exc.details),
            )
        except Exception as exc:  # noqa: BLE001 — the whole point: never let this crash the caller
            return await self._finish(
                task_id, args, start,
                error=ToolError(
                    kind="tool_failure",
                    message=str(exc),
                    retryable=True,
                    details={"exception_type": type(exc).__name__},
                ),
            )

    async def _finish(
        self,
        task_id: str,
        args: dict,
        start: float,
        *,
        data: Any = None,
        confidence: Optional[float] = None,
        error: Optional[ToolError] = None,
    ) -> ToolResult:
        duration_ms = int((time.monotonic() - start) * 1000)
        result = ToolResult(
            ok=error is None,
            data=data,
            error=error,
            confidence=confidence,
            untrusted=self.metadata.returns_untrusted_content,
            duration_ms=duration_ms,
        )
        safe_args = redact_secrets(args)
        safe_data = redact_secrets(data) if data is not None else None
        # EVENT WRITE SITE 3 of 3. When this tool runs inside an MCP server
        # that the orchestrator calls, the same logical tool call is also
        # logged twice more by SafetyPlugin on the client side
        # (before_tool_callback and after_tool_callback in orbit/policy.py).
        # The duplication is known and scheduled as Fix 7 — not addressed
        # here. Until then: assert on event row CONTENT, never on counts.
        event_id = db.log_event(
            task_id,
            tool_call=self.metadata.name,
            args=safe_args,
            result=safe_data if result.ok else None,
            error=None if result.ok else f"{error.kind}: {error.message}",
        )
        result.event_id = str(event_id)
        return result
