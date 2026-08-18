"""The swappable mail/calendar backend behind the `communication` MCP
server — Prompt 6 of the MCP tool layer doc.

Honest, not hidden: connecting a real mailbox needs an account and
credentials only a human can provide — an OAuth consent grant (Gmail/
Google Calendar API) or an app-specific password (IMAP/SMTP) — neither of
which this build has. `LocalMailBackend` is what stands in until someone
supplies those: a genuinely-working local store (its own SQLite file, not
orbit/db.py's task/event/memory schema) rather than a pile of stub methods
that just return empty lists. It exercises the full contract for real —
drafts persist, sent messages actually land in the local "sent" folder and
become searchable/readable — it just never talks to any real mail server.

`MailBackend` is the Protocol every backend implements. A future
`GmailBackend` (google-api-python-client + OAuth) or `ImapSmtpBackend`
(imaplib/smtplib + an app password) drops in by implementing this same
Protocol; `communication_tools.py` never needs to change shape, only which
backend `_get_backend` constructs for a given account's `backend:` config
key. `_get_backend` raises a hard, explicit error for any name it doesn't
recognize (e.g. "gmail") rather than silently falling back to "local" —
same "no silent fallback" rule `orbit/policy.py`'s profile resolution
already established.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "communication_local.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_drafts (
    draft_id    TEXT PRIMARY KEY,
    account     TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 0,
    message_id  TEXT
);

CREATE TABLE IF NOT EXISTS mail_messages (
    message_id  TEXT PRIMARY KEY,
    account     TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    folder      TEXT NOT NULL DEFAULT 'sent',
    sender      TEXT,
    recipient   TEXT,
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_events (
    event_id    TEXT PRIMARY KEY,
    account     TEXT NOT NULL,
    title       TEXT NOT NULL,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    attendees   TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mail_messages_account ON mail_messages(account);
CREATE INDEX IF NOT EXISTS idx_calendar_events_account ON calendar_events(account);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _get_connection(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_local_db(db_path: Path = DB_PATH) -> None:
    with _get_connection(db_path) as conn:
        conn.executescript(_SCHEMA)


class MailBackend(Protocol):
    async def draft(self, *, account: str, recipient: str, subject: str, body: str) -> str: ...

    async def send(self, *, account: str, draft_id: str) -> str: ...

    async def search(self, *, account: str, query: str, limit: int) -> list[dict]: ...

    async def read(self, *, account: str, message_id: str) -> dict: ...

    async def list_threads(self, *, account: str, folder: str, limit: int) -> list[dict]: ...

    async def list_events(self, *, account: str, start: str, end: str) -> list[dict]: ...

    async def create_event(
        self, *, account: str, title: str, start: str, end: str, attendees: list[str]
    ) -> str: ...


class LocalMailBackend:
    """Local SQLite stand-in — see module docstring. Every method here does
    real, verifiable work against `DB_PATH`; nothing is a stub that just
    returns an empty list to satisfy the Protocol's shape."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        init_local_db(db_path)

    async def draft(self, *, account: str, recipient: str, subject: str, body: str) -> str:
        draft_id = f"draft-{uuid.uuid4().hex[:12]}"
        with _get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO mail_drafts (draft_id, account, recipient, subject, body, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (draft_id, account, recipient, subject, body, _now()),
            )
        return draft_id

    async def send(self, *, account: str, draft_id: str) -> str:
        """Fully implemented and directly unit-tested against this backend
        (bypassing the tool layer), even though communication_tools.py's
        EmailSendTool refuses to ever call this — see that module's
        docstring for why send is structurally blocked at TWO layers, not
        just the tier gate."""
        with _get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM mail_drafts WHERE draft_id = ? AND account = ?", (draft_id, account)
            ).fetchone()
            if row is None:
                raise ValueError(f"no such draft: {draft_id!r} for account {account!r}")
            if row["sent"]:
                raise ValueError(f"draft {draft_id!r} was already sent as {row['message_id']!r}")

            message_id = f"msg-{uuid.uuid4().hex[:12]}"
            thread_id = f"thread-{uuid.uuid4().hex[:12]}"
            conn.execute(
                """INSERT INTO mail_messages
                   (message_id, account, thread_id, folder, sender, recipient, subject, body, created_at)
                   VALUES (?, ?, ?, 'sent', ?, ?, ?, ?, ?)""",
                (message_id, account, thread_id, account, row["recipient"], row["subject"], row["body"], _now()),
            )
            conn.execute(
                "UPDATE mail_drafts SET sent = 1, message_id = ? WHERE draft_id = ?",
                (message_id, draft_id),
            )
        return message_id

    async def search(self, *, account: str, query: str, limit: int) -> list[dict]:
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM mail_messages
                   WHERE account = ? AND (subject LIKE ? OR body LIKE ?)
                   ORDER BY created_at DESC LIMIT ?""",
                (account, f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    async def read(self, *, account: str, message_id: str) -> dict:
        with _get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM mail_messages WHERE message_id = ? AND account = ?",
                (message_id, account),
            ).fetchone()
        if row is None:
            raise ValueError(f"no such message: {message_id!r} for account {account!r}")
        return dict(row)

    async def list_threads(self, *, account: str, folder: str, limit: int) -> list[dict]:
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(
                """SELECT thread_id, MAX(created_at) AS last_activity, COUNT(*) AS message_count
                   FROM mail_messages WHERE account = ? AND folder = ?
                   GROUP BY thread_id ORDER BY last_activity DESC LIMIT ?""",
                (account, folder, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    async def list_events(self, *, account: str, start: str, end: str) -> list[dict]:
        with _get_connection(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM calendar_events
                   WHERE account = ? AND start_time < ? AND end_time > ?
                   ORDER BY start_time""",
                (account, end, start),
            ).fetchall()
        return [dict(r) for r in rows]

    async def create_event(
        self, *, account: str, title: str, start: str, end: str, attendees: list[str]
    ) -> str:
        event_id = f"event-{uuid.uuid4().hex[:12]}"
        with _get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO calendar_events
                   (event_id, account, title, start_time, end_time, attendees, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_id, account, title, start, end, json.dumps(attendees), _now()),
            )
        return event_id


_BACKENDS: dict[str, type] = {"local": LocalMailBackend}


def get_backend(backend_name: str) -> MailBackend:
    backend_cls = _BACKENDS.get(backend_name)
    if backend_cls is None:
        raise ValueError(
            f"unknown communication backend {backend_name!r} — no implementation registered "
            f"in orbit.mcp_servers.communication_backend._BACKENDS (have: {sorted(_BACKENDS)}). "
            "This is a hard config error, not a fallback to 'local'."
        )
    return backend_cls()
