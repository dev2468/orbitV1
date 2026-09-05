"""Pure-logic helpers behind the History view's KPI tiles and every
duration/relative-time string the GUI renders.

No Qt imports here on purpose: this is the arithmetic half of the analytics
UI, so it can be unit-tested without a QApplication. The widgets in
``history_view.py`` and ``main.py`` only format what these return.

Timestamps in ``tasks.created_at`` / ``tasks.completed_at`` are ISO-8601 UTC
strings written by ``db._now()`` (``datetime.now(timezone.utc).isoformat()``).
Rows written by older code — or hand-edited — may be naive, so
:func:`parse_ts` normalises both shapes to aware UTC rather than trusting the
string. A naive timestamp compared against an aware ``now()`` raises
``TypeError``, which would take out the whole History tab for one bad row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

_TERMINAL_OK = "COMPLETED"
_TERMINAL_BAD = "FAILED"


def parse_ts(raw: Optional[str]) -> Optional[datetime]:
    """Parse a DB timestamp into an aware UTC datetime, or None.

    Accepts what ``db._now()`` writes plus the two shapes that show up in
    practice: a trailing ``Z`` (some tools emit it) and a naive string with
    no offset at all (assumed UTC — every writer in this codebase is UTC).
    Returns None rather than raising, because a single malformed row must
    not break the view that renders every other row alongside it.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_duration(seconds: Optional[float]) -> str:
    """Human duration in the design's shape: ``34s``, ``1m12s``, ``2h04m``.

    Returns "" for None/negative so callers can hide the label instead of
    printing a placeholder.
    """
    if seconds is None or seconds < 0:
        return ""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def relative_time(raw: Optional[str], *, now: Optional[datetime] = None) -> str:
    """"Now", "2m ago", "1h ago", "3d ago" — the History list's timestamp column.

    Anything under a minute reads "Now": at that resolution "12s ago" is
    noise, and a row that just landed is the one the user is looking at.
    """
    dt = parse_ts(raw)
    if dt is None:
        return ""
    ref = now or datetime.now(timezone.utc)
    delta = (ref - dt).total_seconds()
    if delta < 0:
        return "Now"  # clock skew — never render a future "in 3m"
    if delta < 60:
        return "Now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 604800:
        return f"{int(delta // 86400)}d ago"
    return dt.strftime("%b %d")


def task_duration_seconds(
    task: dict, *, now: Optional[datetime] = None
) -> Optional[float]:
    """Wall-clock seconds a task ran for.

    A finished task measures created→completed. A still-running one measures
    created→now, so the History row ticks up rather than showing nothing;
    callers that must distinguish the two check ``completed_at`` themselves.
    Returns None when ``created_at`` is missing or unparseable.
    """
    started = parse_ts(task.get("created_at"))
    if started is None:
        return None
    finished = parse_ts(task.get("completed_at"))
    if finished is None:
        finished = now or datetime.now(timezone.utc)
    return max(0.0, (finished - started).total_seconds())


def compute_kpis(tasks: Iterable[dict]) -> dict[str, Any]:
    """The three tiles at the top of the History panel.

    ``success_rate`` is computed over *decided* tasks only — COMPLETED plus
    FAILED — not over every row. Counting a currently-RUNNING task as a
    non-success would make the number sag while a task is in flight and jump
    when it lands, which reads as a bug rather than a metric. Same reasoning
    for ``avg_duration_seconds``: an unfinished task has no duration yet, so
    including its partial elapsed time would drag the average toward zero.

    Returns ``success_rate`` and ``avg_duration_seconds`` as None when there
    is nothing to divide by, so the tile can render "—" instead of a
    confident-looking 0%.
    """
    rows = list(tasks)
    total = len(rows)

    completed = sum(1 for t in rows if (t.get("status") or "").upper() == _TERMINAL_OK)
    failed = sum(1 for t in rows if (t.get("status") or "").upper() == _TERMINAL_BAD)
    decided = completed + failed

    success_rate = (completed / decided) if decided else None

    durations = [
        d
        for t in rows
        if t.get("completed_at")
        for d in (task_duration_seconds(t),)
        if d is not None
    ]
    avg_duration = (sum(durations) / len(durations)) if durations else None

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "running": total - decided,
        "success_rate": success_rate,
        "avg_duration_seconds": avg_duration,
    }


def format_percent(value: Optional[float]) -> str:
    """0.83 -> "83%"; None -> "—" (nothing decided yet)."""
    if value is None:
        return "—"
    return f"{int(round(value * 100))}%"


def count_tool_calls(events: Iterable[dict]) -> int:
    """Tool calls in a task, for the inspector's "N steps · M tool calls" line.

    Counts rows carrying a ``tool_call`` name. This is deliberately a count of
    *event rows*, not of distinct logical calls: one logical call currently
    writes up to three rows from three sites (see orbit/CLAUDE.md, Fix 7), so
    treat the number as a trace-volume indicator rather than an exact tally.
    """
    return sum(1 for ev in events if ev.get("tool_call"))
