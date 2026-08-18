"""SQLite task/event/memory store — Section 10 of the architecture spec.

Three tables (tasks, events, memory) per spec, extended with the fields
Section 4's task schema actually needs (lane, risk_tier, model, source_urls)
and the fields Section 14 requires (failure_reason for 14.3, tokens/cost on
events for 14.8 cost visibility). Retention/purge helper covers 14.9.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "orbit.db"

TASK_STATUSES = {
    "PENDING",
    "RUNNING",
    "WAITING_FOR_USER",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
}
LANES = {"headless", "foreground"}
RISK_TIERS = {"low", "medium", "high"}

# A task is finished, one way or another. Used both for event purge
# eligibility (Section 14.9 — events on a still-open task are never purged)
# and by the browser-policy server's session reaper, which closes any
# browser session whose owning task has reached one of these.
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
_TERMINAL_STATUSES = TERMINAL_STATUSES  # back-compat alias

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    lane          TEXT NOT NULL DEFAULT 'headless',
    risk_tier     TEXT NOT NULL DEFAULT 'low',
    goal          TEXT,
    parent_task   TEXT REFERENCES tasks(task_id),
    model         TEXT,
    result        TEXT,
    source_urls   TEXT NOT NULL DEFAULT '[]',
    failure_reason TEXT,
    created_at    TEXT NOT NULL,
    completed_at  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(task_id),
    tool_call  TEXT,
    args       TEXT,
    result     TEXT,
    error      TEXT,
    tokens_in  INTEGER,
    tokens_out INTEGER,
    cost_usd   REAL,
    timestamp  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory (
    memory_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    content    TEXT NOT NULL,
    task_id    TEXT REFERENCES tasks(task_id),
    project    TEXT,
    -- 'user' | 'system' | 'external'. Content that originated from a web
    -- page, email, or other outside source stays tagged 'external' forever,
    -- even once it's been sitting in our own DB for months — provenance
    -- must survive retrieval (MCP tool layer doc, Prompt 3).
    provenance TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_memory_type ON memory(type);

-- External-content FTS5 index over tasks, per the MCP tool layer doc's
-- Prompt 3: "start with FTS5, no embeddings until keyword search
-- demonstrably falls short." Kept in sync via triggers rather than
-- rebuilt per query.
CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title, goal, result, content='tasks', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS tasks_ai AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, goal, result)
    VALUES (new.rowid, new.title, new.goal, new.result);
END;

CREATE TRIGGER IF NOT EXISTS tasks_ad AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, goal, result)
    VALUES ('delete', old.rowid, old.title, old.goal, old.result);
END;

CREATE TRIGGER IF NOT EXISTS tasks_au AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, goal, result)
    VALUES ('delete', old.rowid, old.title, old.goal, old.result);
    INSERT INTO tasks_fts(rowid, title, goal, result)
    VALUES (new.rowid, new.title, new.goal, new.result);
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(_SCHEMA)


def create_task(
    title: str,
    *,
    goal: str = "",
    lane: str = "headless",
    risk_tier: str = "low",
    parent_task: Optional[str] = None,
    model: Optional[str] = None,
    task_id: Optional[str] = None,
) -> str:
    if lane not in LANES:
        raise ValueError(f"invalid lane: {lane!r}")
    if risk_tier not in RISK_TIERS:
        raise ValueError(f"invalid risk_tier: {risk_tier!r}")
    task_id = task_id or f"TASK-{uuid.uuid4().hex[:12]}"
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO tasks
               (task_id, title, status, lane, risk_tier, goal, parent_task,
                model, source_urls, created_at)
               VALUES (?, ?, 'PENDING', ?, ?, ?, ?, ?, '[]', ?)""",
            (task_id, title, lane, risk_tier, goal, parent_task, model, _now()),
        )
    return task_id


def update_task_status(
    task_id: str,
    status: str,
    *,
    result: Optional[str] = None,
    failure_reason: Optional[str] = None,
) -> None:
    if status not in TASK_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    completed_at = _now() if status in _TERMINAL_STATUSES else None
    with get_connection() as conn:
        conn.execute(
            """UPDATE tasks
               SET status = ?, result = COALESCE(?, result),
                   failure_reason = COALESCE(?, failure_reason),
                   completed_at = COALESCE(?, completed_at)
               WHERE task_id = ?""",
            (status, result, failure_reason, completed_at, task_id),
        )


def log_event(
    task_id: str,
    *,
    tool_call: Optional[str] = None,
    args: Any = None,
    result: Any = None,
    error: Optional[str] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cost_usd: Optional[float] = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO events
               (task_id, tool_call, args, result, error, tokens_in,
                tokens_out, cost_usd, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                tool_call,
                json.dumps(args) if args is not None else None,
                json.dumps(result) if result is not None else None,
                error,
                tokens_in,
                tokens_out,
                cost_usd,
                _now(),
            ),
        )
        return cur.lastrowid


MEMORY_TYPES = {"episodic", "semantic", "procedural", "project"}
PROVENANCE_VALUES = {"user", "system", "external"}


def add_memory(
    content: str,
    *,
    type: str = "episodic",
    task_id: Optional[str] = None,
    project: Optional[str] = None,
    provenance: str = "system",
) -> int:
    if type not in MEMORY_TYPES:
        raise ValueError(f"invalid memory type: {type!r}, must be one of {MEMORY_TYPES}")
    if provenance not in PROVENANCE_VALUES:
        raise ValueError(f"invalid provenance: {provenance!r}, must be one of {PROVENANCE_VALUES}")
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO memory (type, content, task_id, project, provenance, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (type, content, task_id, project, provenance, _now()),
        )
        return cur.lastrowid


def search_task_history(
    query: str,
    *,
    type: Optional[str] = None,
    project: Optional[str] = None,
    date_range: Optional[tuple[str, str]] = None,
) -> list[dict]:
    """Keyword/tag lookup over memory — Section 10: no vector DB until this
    demonstrably falls short."""
    sql = "SELECT * FROM memory WHERE content LIKE ?"
    params: list[Any] = [f"%{query}%"]
    if type:
        sql += " AND type = ?"
        params.append(type)
    if project:
        sql += " AND project = ?"
        params.append(project)
    if date_range:
        sql += " AND created_at BETWEEN ? AND ?"
        params.extend(date_range)
    sql += " ORDER BY created_at DESC"
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_daily_cost(tool_call: str, *, day: Optional[str] = None) -> float:
    """Sum of events.cost_usd for a given tool_call within one UTC calendar
    day (default: today) — generic per-tool daily spend cap infrastructure
    (Section 14.8 cost visibility), meant to be checked BEFORE starting a
    costed call, not after, so a cap actually stops spend rather than just
    reporting it after the fact. No caller currently exists: its one
    caller was the voice runtime's daily transcription-cost cap, removed
    along with all voice code — see orbit/CLAUDE.md."""
    day = day or datetime.now(timezone.utc).date().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(cost_usd), 0) AS total FROM events
               WHERE tool_call = ? AND cost_usd IS NOT NULL
                 AND timestamp >= ? AND timestamp < date(?, '+1 day')""",
            (tool_call, day, day),
        ).fetchone()
        return float(row["total"])


def purge_old_events(retention_days: int = 30) -> int:
    """Section 14.9: delete events belonging to terminal (COMPLETED/FAILED/
    CANCELLED) tasks older than retention_days. Events on open tasks are
    kept regardless of age. Returns number of rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    placeholders = ",".join("?" * len(_TERMINAL_STATUSES))
    with get_connection() as conn:
        cur = conn.execute(
            f"""DELETE FROM events
                WHERE timestamp < ?
                  AND task_id IN (
                      SELECT task_id FROM tasks
                      WHERE status IN ({placeholders})
                  )""",
            (cutoff, *sorted(_TERMINAL_STATUSES)),
        )
        return cur.rowcount


def _fts_query(raw_query: str) -> Optional[str]:
    """Tokenize a free-text query into a lenient FTS5 MATCH expression.
    Quoting each token avoids FTS5 syntax errors from punctuation in
    natural-language input (parens, colons, etc. are FTS5 operators)."""
    import re

    tokens = re.findall(r"[A-Za-z0-9_]+", raw_query)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def search_tasks_fts(query: str, *, limit: int = 10) -> list[dict]:
    """FTS5 search over tasks(title, goal, result) — Prompt 3 of the MCP
    tool layer doc: start with keyword search, no embeddings yet."""
    fts_query = _fts_query(query)
    with get_connection() as conn:
        if not fts_query:
            rows = conn.execute(
                "SELECT *, NULL AS relevance FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT tasks.*, bm25(tasks_fts) AS relevance
                   FROM tasks_fts
                   JOIN tasks ON tasks.rowid = tasks_fts.rowid
                   WHERE tasks_fts MATCH ?
                   ORDER BY relevance
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def get_task(task_id: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None


def list_tasks(*, status: Optional[str] = None) -> list[dict]:
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
