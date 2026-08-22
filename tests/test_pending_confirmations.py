"""Tests for the pending_confirmations data model (Phase 3).

This table exists to open a door that is deliberately shut: a vision-tier
ElementRef cannot be actuated, and until now there was no way for a human to
look at one and say yes. Everything here is therefore about the door staying
narrow. The happy path is one test; the rest are the ways an approval could
turn into more authority than the human granted.

Per tests/CLAUDE.md, every assertion is on DB state.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from orbit import db


@pytest.fixture
def task_id():
    return db.create_task("confirmation test task")


# --- creation --------------------------------------------------------------


def test_a_new_confirmation_is_pending_and_carries_no_token(task_id):
    """A merely PROPOSED action must hold nothing that could authorise
    anything. The token comes into existence only when a human approves."""
    cid = db.create_pending_confirmation(
        "windows_click", {"target": {"bounds": [10, 20, 30, 40]}}, task_id=task_id
    )
    row = db.get_pending_confirmation(cid)
    assert row["status"] == "PENDING"
    assert row["approval_token"] is None
    assert row["token_expires_at"] is None
    assert row["resolved_at"] is None
    assert row["created_at"]


def test_payload_and_candidate_box_round_trip(task_id):
    """What the human approved has to be exactly what executes, so the stored
    payload must come back unchanged rather than re-derived."""
    payload = {"target": {"element_id": "vision:1/knob"}, "button": "left"}
    cid = db.create_pending_confirmation(
        "windows_click", payload, task_id=task_id,
        candidate_box=(100, 200, 180, 240), candidate_label="the red knob",
        screenshot_path="data/confirmations/shot.png",
    )
    row = db.get_pending_confirmation(cid)
    assert row["payload"] == payload
    assert row["candidate_box"] == (100, 200, 180, 240)
    assert row["candidate_label"] == "the red knob"
    assert row["screenshot_path"] == "data/confirmations/shot.png"


def test_confirmation_against_an_unknown_task_is_refused(task_id):
    """The foreign key must bite. Filing an approval under a task nobody is
    watching is worse than failing loudly."""
    with pytest.raises(sqlite3.IntegrityError):
        db.create_pending_confirmation("windows_click", {}, task_id="TASK-does-not-exist")


def test_no_task_id_materializes_the_adhoc_task(task_id):
    """Same fallback SafetyPlugin._task_id and the MCP servers use — but only
    when there is genuinely no task, never to paper over a wrong id."""
    cid = db.create_pending_confirmation("windows_click", {})
    row = db.get_pending_confirmation(cid)
    assert row["task_id"] == db.ADHOC_CONFIRMATION_TASK
    assert db.get_task(db.ADHOC_CONFIRMATION_TASK) is not None


def test_action_is_required(task_id):
    with pytest.raises(ValueError):
        db.create_pending_confirmation("", {}, task_id=task_id)


# --- resolution ------------------------------------------------------------


def test_approval_mints_a_token_and_records_the_decision(task_id):
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    token = db.resolve_pending_confirmation(cid, approved=True)
    assert token

    row = db.get_pending_confirmation(cid)
    assert row["status"] == "APPROVED"
    assert row["approval_token"] == token
    assert row["resolved_at"] is not None
    assert row["token_expires_at"] is not None


def test_rejection_records_the_decision_and_mints_nothing(task_id):
    """A refusal must leave no capability behind — nothing to leak, replay, or
    hand on by mistake."""
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    token = db.resolve_pending_confirmation(cid, approved=False)
    assert token is None

    row = db.get_pending_confirmation(cid)
    assert row["status"] == "REJECTED"
    assert row["approval_token"] is None
    assert row["token_expires_at"] is None
    assert row["resolved_at"] is not None


def test_a_rejected_confirmation_can_never_later_be_approved(task_id):
    """THE test on this table. A plain UPDATE would happily flip REJECTED to
    APPROVED and mint a token for an action a human refused. The conditional
    update is what stops it, and 'nothing calls it twice today' is not a
    reason to leave that open."""
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    db.resolve_pending_confirmation(cid, approved=False)

    with pytest.raises(KeyError):
        db.resolve_pending_confirmation(cid, approved=True)

    row = db.get_pending_confirmation(cid)
    assert row["status"] == "REJECTED"
    assert row["approval_token"] is None


def test_an_approved_confirmation_cannot_be_approved_twice(task_id):
    """Two tokens for one human decision would be two clicks' worth of
    authority granted for one yes."""
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    first = db.resolve_pending_confirmation(cid, approved=True)

    with pytest.raises(KeyError):
        db.resolve_pending_confirmation(cid, approved=True)

    assert db.get_pending_confirmation(cid)["approval_token"] == first


def test_resolving_an_unknown_confirmation_raises(task_id):
    with pytest.raises(KeyError):
        db.resolve_pending_confirmation("CONF-nope", approved=True)


def test_tokens_are_unguessable_and_unique(task_id):
    """The token is a capability. A predictable one is not a gate.
    secrets.token_urlsafe(32) gives ~256 bits; anything short or sequential
    would be enumerable."""
    tokens = set()
    for _ in range(15):
        cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
        tokens.add(db.resolve_pending_confirmation(cid, approved=True))
    assert len(tokens) == 15
    assert all(len(t) >= 32 for t in tokens)


# --- listing ---------------------------------------------------------------


def test_listing_returns_only_pending_rows_oldest_first(task_id):
    """What a UI polls. Oldest-first so the queue is answered in the order the
    human was asked."""
    first = db.create_pending_confirmation("windows_click", {"n": 1}, task_id=task_id)
    second = db.create_pending_confirmation("windows_click", {"n": 2}, task_id=task_id)
    third = db.create_pending_confirmation("windows_click", {"n": 3}, task_id=task_id)
    db.resolve_pending_confirmation(second, approved=True)

    ids = [r["confirmation_id"] for r in db.list_pending_confirmations()]
    assert ids == [first, third]


def test_listing_can_be_scoped_to_one_task(task_id):
    other = db.create_task("other task")
    mine = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    db.create_pending_confirmation("windows_click", {}, task_id=other)

    ids = [r["confirmation_id"] for r in db.list_pending_confirmations(task_id=task_id)]
    assert ids == [mine]


# --- token consumption -----------------------------------------------------


def test_a_valid_token_can_be_spent_once(task_id):
    cid = db.create_pending_confirmation(
        "windows_click", {"button": "left"}, task_id=task_id
    )
    token = db.resolve_pending_confirmation(cid, approved=True)

    spent = db.consume_approval_token(token)
    assert spent is not None
    assert spent["confirmation_id"] == cid
    assert spent["payload"] == {"button": "left"}
    assert spent["token_consumed_at"] is not None


def test_a_token_cannot_be_spent_twice(task_id):
    """Single use is the difference between "approve this click" and "approve
    clicking whenever you like"."""
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    token = db.resolve_pending_confirmation(cid, approved=True)

    assert db.consume_approval_token(token) is not None
    assert db.consume_approval_token(token) is None


def test_an_expired_token_is_refused_and_the_row_is_marked_expired(task_id):
    """An approval is consent about a screenshot of a moment. Replaying it
    later aims a click at a screen that has since changed."""
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    token = db.resolve_pending_confirmation(cid, approved=True, ttl_seconds=-1)

    assert db.consume_approval_token(token) is None
    assert db.get_pending_confirmation(cid)["status"] == "EXPIRED"


def test_a_token_within_its_ttl_is_accepted(task_id):
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    token = db.resolve_pending_confirmation(cid, approved=True, ttl_seconds=300)
    row = db.get_pending_confirmation(cid)

    expiry = datetime.fromisoformat(row["token_expires_at"])
    assert expiry > datetime.now(timezone.utc) + timedelta(seconds=120)
    assert db.consume_approval_token(token) is not None


def test_unknown_empty_and_none_tokens_are_refused(task_id):
    assert db.consume_approval_token("not-a-real-token") is None
    assert db.consume_approval_token("") is None
    assert db.consume_approval_token(None) is None


def test_consumption_does_not_reveal_why_it_failed(task_id):
    """Unknown, spent, expired and never-approved all return exactly None.
    Distinguishing them would turn this into an oracle for probing which
    tokens exist."""
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    spent_token = db.resolve_pending_confirmation(cid, approved=True)
    db.consume_approval_token(spent_token)

    expired_cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    expired_token = db.resolve_pending_confirmation(
        expired_cid, approved=True, ttl_seconds=-1
    )

    assert db.consume_approval_token(spent_token) is None
    assert db.consume_approval_token(expired_token) is None
    assert db.consume_approval_token("fabricated") is None


def test_a_token_is_bound_to_its_own_confirmation_only(task_id):
    """One yes authorises one action. A token must not resolve to a different
    proposed click than the one the human looked at."""
    a = db.create_pending_confirmation("windows_click", {"which": "a"}, task_id=task_id)
    b = db.create_pending_confirmation("windows_click", {"which": "b"}, task_id=task_id)
    token_a = db.resolve_pending_confirmation(a, approved=True)
    db.resolve_pending_confirmation(b, approved=True)

    spent = db.consume_approval_token(token_a)
    assert spent["confirmation_id"] == a
    assert spent["payload"] == {"which": "a"}
    assert db.get_pending_confirmation(b)["token_consumed_at"] is None


# --- schema ----------------------------------------------------------------


def test_the_table_is_created_by_init_db():
    """init_db() runs at every entry point (run_task, every MCP server, the
    GUI, conftest), so CREATE TABLE IF NOT EXISTS is the whole migration
    story for an existing orbit.db. If that ever stops being true, this table
    silently will not exist in the field."""
    db.init_db()
    with db.get_connection() as conn:
        names = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "pending_confirmations" in names
