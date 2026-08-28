"""SQLite task/event/memory store — Section 10 of the architecture spec.

Three tables (tasks, events, memory) per spec, extended with the fields
Section 4's task schema actually needs (lane, risk_tier, model, source_urls)
and the fields Section 14 requires (failure_reason for 14.3, tokens/cost on
events for 14.8 cost visibility). Retention/purge helper covers 14.9.
"""

from __future__ import annotations

import json
import secrets
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

# A confirmation's status records WHAT THE HUMAN DECIDED, and nothing else.
# Whether the resulting capability was later spent lives in
# token_consumed_at, deliberately separate — see the table comment.
CONFIRMATION_STATUSES = {"PENDING", "APPROVED", "REJECTED", "EXPIRED"}

# Task row materialized when a confirmation is raised outside any task, the
# same fallback `SafetyPlugin._task_id` and the MCP servers already use:
# pending_confirmations.task_id is a real foreign key with
# PRAGMA foreign_keys=ON, so an insert against a missing task raises
# IntegrityError rather than quietly orphaning the row.
ADHOC_CONFIRMATION_TASK = "adhoc-confirmation"

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

-- Human-in-the-loop approvals for actions that cannot be auto-approved.
--
-- This table exists because the vision tier can SEE a control it is not
-- allowed to CLICK: an ElementRef scored Confidence.VISION_INFERRED (0.50)
-- sits below windows_control_policy.yaml's min_actuation_confidence (0.70),
-- so windows_click/windows_drag refuse it. That refusal is correct and is
-- not what this table relaxes. What was missing was a CHANNEL — there was
-- no way for a human to look at a guess and say yes — so the only available
-- answer was "no", forever.
--
-- The approval_token is a CAPABILITY, not a record. Every column here that
-- constrains it is load-bearing:
--   * it is minted only on an APPROVED row, never on a REJECTED one;
--   * token_expires_at makes it short-lived, so an approval cannot be
--     replayed later against a screen that has since changed — the whole
--     basis for approving was a screenshot of a moment;
--   * token_consumed_at makes it single-use, kept separate from `status`
--     because "what the human decided" and "was the capability spent" are
--     different facts and conflating them loses one of them;
--   * it is bound to one confirmation row, and therefore to one proposed
--     action, so it can never authorise a second click.
--
-- screenshot_path holds a PATH, not the image. Base64 PNGs of every
-- confirmation would bloat this DB without limit, and the GUI (which renders
-- the candidate box over the shot) can read a file perfectly well.
CREATE TABLE IF NOT EXISTS pending_confirmations (
    confirmation_id  TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(task_id),
    -- The tool that would run, e.g. 'windows_click', and its proposed args
    -- as JSON. Stored verbatim so what the human approved is exactly what
    -- executes; a re-derived payload could differ from the one shown.
    action           TEXT NOT NULL,
    payload          TEXT NOT NULL,
    screenshot_path  TEXT,
    candidate_box    TEXT,
    candidate_label  TEXT,
    status           TEXT NOT NULL DEFAULT 'PENDING',
    approval_token   TEXT UNIQUE,
    token_expires_at TEXT,
    token_consumed_at TEXT,
    created_at       TEXT NOT NULL,
    resolved_at      TEXT
);

-- Lightweight cache of successful UI element click positions. Keyed by
-- (process_name, element_desc); hits incremented on re-use so frequently
-- clicked elements bubble up. Token cost at query time: one short row.
CREATE TABLE IF NOT EXISTS ui_memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    process_name TEXT NOT NULL COLLATE NOCASE,
    element_desc TEXT NOT NULL COLLATE NOCASE,
    x            INTEGER NOT NULL,
    y            INTEGER NOT NULL,
    automation_id TEXT,
    hits         INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(process_name, element_desc)
);

CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_memory_type ON memory(type);
CREATE INDEX IF NOT EXISTS idx_pending_conf_status ON pending_confirmations(status);
CREATE INDEX IF NOT EXISTS idx_pending_conf_task ON pending_confirmations(task_id);

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


def ui_memory_lookup(process_name: str, description: str) -> Optional[dict]:
    """Return cached {x, y, automation_id, hits} for a (process, desc) pair,
    or None if nothing is cached. COLLATE NOCASE — caller need not worry
    about case."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT x, y, automation_id, hits FROM ui_memory "
            "WHERE process_name = ? AND element_desc = ?",
            (process_name, description),
        ).fetchone()
    if row is None:
        return None
    return {"x": row[0], "y": row[1], "automation_id": row[2], "hits": row[3]}


def ui_memory_upsert(
    process_name: str,
    description: str,
    x: int,
    y: int,
    automation_id: Optional[str] = None,
) -> None:
    """Insert or update a cached click position; increment hit counter."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO ui_memory (process_name, element_desc, x, y, automation_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(process_name, element_desc) DO UPDATE SET
                   x = excluded.x,
                   y = excluded.y,
                   automation_id = COALESCE(excluded.automation_id, ui_memory.automation_id),
                   hits = hits + 1,
                   updated_at = datetime('now')""",
            (process_name, description, x, y, automation_id),
        )


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


# --- pending confirmations (human-in-the-loop approval) ---------------------


def _ensure_confirmation_task(task_id: Optional[str]) -> str:
    """Resolve the task a confirmation belongs to.

    A task_id that was explicitly supplied is returned UNCHANGED, even if no
    such task exists — the foreign key then fails loudly. That is deliberate:
    silently inventing a task row for a caller that passed a wrong id would
    hide the bug and file the approval under a task nobody is watching. The
    adhoc row is only for callers with genuinely no task, matching
    `SafetyPlugin._task_id` and the perception server's `_resolve_task_id`.
    """
    if task_id:
        return task_id
    if get_task(ADHOC_CONFIRMATION_TASK) is None:
        create_task("adhoc confirmation requests", task_id=ADHOC_CONFIRMATION_TASK)
    return ADHOC_CONFIRMATION_TASK


def create_pending_confirmation(
    action: str,
    payload: dict,
    *,
    task_id: Optional[str] = None,
    screenshot_path: Optional[str] = None,
    candidate_box: Optional[tuple] = None,
    candidate_label: Optional[str] = None,
) -> str:
    """Record a proposed action awaiting a human yes/no. Returns its id.

    Creates the row in PENDING with NO approval_token. A token exists only
    after a human approves, so a row that is merely *proposed* carries
    nothing that could authorise anything.
    """
    if not action:
        raise ValueError("action is required")
    confirmation_id = f"CONF-{uuid.uuid4().hex[:12]}"
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO pending_confirmations
               (confirmation_id, task_id, action, payload, screenshot_path,
                candidate_box, candidate_label, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
            (
                confirmation_id,
                _ensure_confirmation_task(task_id),
                action,
                json.dumps(payload),
                screenshot_path,
                json.dumps(list(candidate_box)) if candidate_box else None,
                candidate_label,
                _now(),
            ),
        )
    return confirmation_id


def get_pending_confirmation(confirmation_id: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM pending_confirmations WHERE confirmation_id = ?",
            (confirmation_id,),
        ).fetchone()
    return _confirmation_row(row)


def list_pending_confirmations(*, task_id: Optional[str] = None) -> list:
    """Every still-PENDING confirmation, oldest first.

    This is what a UI polls — the GUI dashboard already does exactly this for
    tasks. Ordered oldest-first so the queue is answered in the order a human
    was asked, rather than newest-shouts-loudest.
    """
    sql = "SELECT * FROM pending_confirmations WHERE status = 'PENDING'"
    params: tuple = ()
    if task_id:
        sql += " AND task_id = ?"
        params = (task_id,)
    sql += " ORDER BY created_at ASC"
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_confirmation_row(r) for r in rows]


def resolve_pending_confirmation(
    confirmation_id: str,
    *,
    approved: bool,
    ttl_seconds: int = 120,
) -> Optional[str]:
    """Record the human's decision. Returns the approval token, or None.

    A token is returned ONLY when approved is True. On rejection the row is
    closed carrying no token at all, so there is nothing to leak, replay, or
    pass along by mistake.

    The UPDATE is conditional on the row still being PENDING and its rowcount
    is checked, so a second call cannot re-decide a closed row. That is the
    property stopping a REJECTED confirmation from later becoming APPROVED —
    a plain UPDATE would do exactly that, and "nothing currently calls it
    twice" is not a reason to leave the door open.

    Raises KeyError when the row is missing or already resolved. Both need to
    stay distinguishable from "approved, here is your token", and neither is
    a normal outcome worth swallowing.
    """
    token = secrets.token_urlsafe(32) if approved else None
    expires = (
        (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        if approved
        else None
    )
    status = "APPROVED" if approved else "REJECTED"

    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE pending_confirmations
                  SET status = ?, approval_token = ?, token_expires_at = ?,
                      resolved_at = ?
                WHERE confirmation_id = ? AND status = 'PENDING'""",
            (status, token, expires, _now(), confirmation_id),
        )
        if cursor.rowcount == 0:
            existing = conn.execute(
                "SELECT status FROM pending_confirmations WHERE confirmation_id = ?",
                (confirmation_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"no such confirmation: {confirmation_id!r}")
            raise KeyError(
                f"confirmation {confirmation_id!r} is already resolved "
                f"({existing['status']}) and cannot be decided again"
            )
    return token


def consume_approval_token(token: str) -> Optional[dict]:
    """Spend an approval token, exactly once. Returns its row, or None.

    None means the token is not usable, for ANY reason — unknown, already
    spent, expired, or attached to a row that was not approved. The caller is
    deliberately not told which: the answer to "may I act" is identical in
    every case, and distinguishing them turns this into an oracle for probing
    which tokens exist.

    Expiry is enforced HERE rather than trusted from a caller-supplied clock,
    and an expired row is flipped to EXPIRED as it is refused, so what is on
    disk matches the decision just made.
    """
    if not token:
        return None
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM pending_confirmations WHERE approval_token = ?",
            (token,),
        ).fetchone()
        if row is None or row["status"] != "APPROVED":
            return None
        if row["token_consumed_at"] is not None:
            return None
        if row["token_expires_at"] and datetime.fromisoformat(
            row["token_expires_at"]
        ) < now:
            conn.execute(
                "UPDATE pending_confirmations SET status = 'EXPIRED' "
                "WHERE confirmation_id = ?",
                (row["confirmation_id"],),
            )
            return None
        cursor = conn.execute(
            """UPDATE pending_confirmations SET token_consumed_at = ?
                WHERE confirmation_id = ? AND token_consumed_at IS NULL""",
            (_now(), row["confirmation_id"]),
        )
        if cursor.rowcount == 0:
            # Lost a race with another consumer. Refuse rather than act: two
            # callers must never both come away believing they hold the
            # single-use capability.
            return None
        fresh = conn.execute(
            "SELECT * FROM pending_confirmations WHERE confirmation_id = ?",
            (row["confirmation_id"],),
        ).fetchone()
    return _confirmation_row(fresh)


def _confirmation_row(row) -> Optional[dict]:
    """Decode a row, parsing the JSON columns back into real objects."""
    if row is None:
        return None
    out = dict(row)
    out["payload"] = json.loads(out["payload"]) if out.get("payload") else {}
    out["candidate_box"] = (
        tuple(json.loads(out["candidate_box"])) if out.get("candidate_box") else None
    )
    return out
