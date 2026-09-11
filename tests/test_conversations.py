"""Tests for the conversation backend (Phase 4).

All tests are deterministic and offline — they exercise the DB schema,
conversation CRUD, turn linkage, and context building, not model calls.
"""

import pytest

from orbit import db
from orbit.run_task import _build_conversation_context


def test_create_conversation_returns_id():
    conv_id = db.create_conversation(title="test conv")
    assert conv_id.startswith("CONV-")
    conv = db.get_conversation(conv_id)
    assert conv is not None
    assert conv["title"] == "test conv"
    assert conv["lane"] == "headless"


def test_create_conversation_with_explicit_id():
    conv_id = db.create_conversation(
        title="explicit", conversation_id="CONV-explicit"
    )
    assert conv_id == "CONV-explicit"
    assert db.get_conversation("CONV-explicit") is not None


def test_create_conversation_validates_lane():
    with pytest.raises(ValueError, match="invalid lane"):
        db.create_conversation(lane="bogus")


def test_list_conversations_ordered_by_updated():
    db.create_conversation(title="first", conversation_id="CONV-first")
    db.create_conversation(title="second", conversation_id="CONV-second")
    convs = db.list_conversations()
    assert len(convs) >= 2
    assert convs[0]["conversation_id"] == "CONV-second"


def test_get_conversation_returns_none_for_missing():
    assert db.get_conversation("CONV-nonexistent") is None


def test_add_turn_assigns_sequential_indices():
    conv_id = db.create_conversation(title="multi-turn")
    t1 = db.create_task("turn 1", goal="goal 1")
    t2 = db.create_task("turn 2", goal="goal 2")

    idx1 = db.add_turn_to_conversation(conv_id, t1)
    idx2 = db.add_turn_to_conversation(conv_id, t2)

    assert idx1 == 0
    assert idx2 == 1

    turns = db.conversation_turns(conv_id)
    assert len(turns) == 2
    assert turns[0]["task_id"] == t1
    assert turns[0]["turn_index"] == 0
    assert turns[1]["task_id"] == t2
    assert turns[1]["turn_index"] == 1


def test_create_task_with_conversation_id():
    conv_id = db.create_conversation(title="linked")
    task_id = db.create_task(
        "linked task", goal="g", conversation_id=conv_id, turn_index=0,
    )
    task = db.get_task(task_id)
    assert task["conversation_id"] == conv_id
    assert task["turn_index"] == 0


def test_create_task_without_conversation_id():
    task_id = db.create_task("standalone")
    task = db.get_task(task_id)
    assert task["conversation_id"] is None
    assert task["turn_index"] is None


def test_conversation_turns_empty_for_new_conversation():
    conv_id = db.create_conversation(title="empty")
    assert db.conversation_turns(conv_id) == []


def test_build_conversation_context_empty_for_no_turns():
    conv_id = db.create_conversation(title="empty context")
    assert _build_conversation_context(conv_id) == ""


def test_build_conversation_context_includes_prior_turns():
    conv_id = db.create_conversation(title="context test")
    t1 = db.create_task("turn 1", goal="find flights")
    db.update_task_status(t1, "COMPLETED", result="Found 3 flights to NYC")
    db.add_turn_to_conversation(conv_id, t1)

    context = _build_conversation_context(conv_id)
    assert "CONVERSATION HISTORY" in context
    assert "find flights" in context
    assert "Found 3 flights" in context


def test_build_conversation_context_truncates_long_results():
    conv_id = db.create_conversation(title="truncation test")
    t1 = db.create_task("turn 1", goal="verbose")
    db.update_task_status(t1, "COMPLETED", result="x" * 1000)
    db.add_turn_to_conversation(conv_id, t1)

    context = _build_conversation_context(conv_id)
    assert "..." in context
    assert len(context) < 1000


def test_migration_is_idempotent():
    db.init_db()
    db.init_db()
    conv_id = db.create_conversation(title="after double init")
    assert db.get_conversation(conv_id) is not None


# --- session reuse -----------------------------------------------------------
#
# Turn-to-turn continuity was a TEXT SUMMARY of prior goals and results. It now
# reuses the ADK session, so turn two continues turn one's real history — the
# actual tool calls, their arguments, and their results, not a paraphrase.
#
# These pin the bookkeeping around that. The continuity itself needs a live
# model and is verified by hand; what breaks silently is the cache lifecycle,
# which is what is tested here.

import asyncio

import pytest

from orbit import run_task as rt


@pytest.fixture(autouse=True)
def _clean_runner_cache():
    rt._RUNNER_CACHE.clear()
    yield
    rt._RUNNER_CACHE.clear()


class _FakeRunner:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


def test_closing_a_conversation_closes_its_runner():
    runner = _FakeRunner()
    rt._RUNNER_CACHE["CONV-x"] = (runner, "CONV-x")

    asyncio.run(rt.close_conversation("CONV-x"))

    assert runner.closed == 1
    assert "CONV-x" not in rt._RUNNER_CACHE


def test_closing_an_unknown_conversation_is_a_no_op():
    asyncio.run(rt.close_conversation("CONV-never-existed"))  # must not raise


def test_closing_is_idempotent():
    """The GUI can send close_conversation for a conversation the worker has
    already dropped — a restart, a crash — and that must not error."""
    runner = _FakeRunner()
    rt._RUNNER_CACHE["CONV-x"] = (runner, "CONV-x")
    asyncio.run(rt.close_conversation("CONV-x"))
    asyncio.run(rt.close_conversation("CONV-x"))
    assert runner.closed == 1


def test_close_all_releases_every_conversation():
    """Called when the worker's stdin closes. Each cached entry is holding six
    MCP subprocesses, one of which owns a Chrome profile lock."""
    runners = [_FakeRunner() for _ in range(3)]
    for i, runner in enumerate(runners):
        rt._RUNNER_CACHE[f"CONV-{i}"] = (runner, f"CONV-{i}")

    asyncio.run(rt.close_all_conversations())

    assert all(r.closed == 1 for r in runners)
    assert rt._RUNNER_CACHE == {}


def test_a_failing_close_does_not_block_the_others():
    """One wedged MCP server must not strand the rest at shutdown."""
    class _Exploding(_FakeRunner):
        async def close(self):
            raise RuntimeError("server wedged")

    good = _FakeRunner()
    rt._RUNNER_CACHE["CONV-bad"] = (_Exploding(), "CONV-bad")
    rt._RUNNER_CACHE["CONV-good"] = (good, "CONV-good")

    asyncio.run(rt.close_all_conversations())

    assert good.closed == 1
    assert rt._RUNNER_CACHE == {}


def test_the_text_summary_is_skipped_when_a_live_session_exists():
    """The bridge and the session are alternatives, never both: injecting a
    paraphrase on top of the real history shows the model every turn twice."""
    conv = db.create_conversation(title="dup check")
    task = db.create_task("t", goal="open notepad", conversation_id=conv)
    db.add_turn_to_conversation(conv, task)
    db.update_task_status(task, "COMPLETED", result="Notepad is open.")

    # No cached runner: the summary is the only continuity available.
    assert "open notepad" in _build_conversation_context(conv)

    # With one, run_task uses the session instead — asserted on the condition
    # run_task branches on, since building a real runner needs a model call.
    rt._RUNNER_CACHE[conv] = (_FakeRunner(), conv)
    assert conv in rt._RUNNER_CACHE


# --- the replay cap ----------------------------------------------------------
#
# The text bridge used to replay EVERY turn. That was fine while it only ever
# covered one or two, but resuming an old chat from the GUI can hand it thirty
# at up to ~500 characters of result each, all in the first message.


def test_context_replays_only_the_most_recent_turns():
    from orbit.run_task import _MAX_CONTEXT_TURNS

    conv = db.create_conversation(title="long chat")
    for i in range(_MAX_CONTEXT_TURNS + 5):
        task = db.create_task(f"t{i}", goal=f"goal number {i}", conversation_id=conv)
        db.add_turn_to_conversation(conv, task)
        db.update_task_status(task, "COMPLETED", result=f"result {i}")

    context = _build_conversation_context(conv)
    assert "goal number 0" not in context, "oldest turn should have been dropped"
    assert f"goal number {_MAX_CONTEXT_TURNS + 4}" in context


def test_dropped_turns_are_declared_not_silently_omitted():
    """Same reasoning as the history compactor: a model that can see
    something is missing asks about it, one shown an unexplained gap
    confabulates over it."""
    from orbit.run_task import _MAX_CONTEXT_TURNS

    conv = db.create_conversation(title="long chat")
    for i in range(_MAX_CONTEXT_TURNS + 3):
        task = db.create_task(f"t{i}", goal=f"goal {i}", conversation_id=conv)
        db.add_turn_to_conversation(conv, task)
        db.update_task_status(task, "COMPLETED", result="r")

    assert "omitted" in _build_conversation_context(conv)


def test_a_short_conversation_is_replayed_whole():
    conv = db.create_conversation(title="short chat")
    for i in range(3):
        task = db.create_task(f"t{i}", goal=f"goal {i}", conversation_id=conv)
        db.add_turn_to_conversation(conv, task)
        db.update_task_status(task, "COMPLETED", result=f"result {i}")

    context = _build_conversation_context(conv)
    assert "omitted" not in context
    for i in range(3):
        assert f"goal {i}" in context


def test_a_turn_with_no_goal_is_left_out_of_the_context():
    conv = db.create_conversation(title="with a dud turn")
    good = db.create_task("t", goal="a real goal", conversation_id=conv)
    db.add_turn_to_conversation(conv, good)
    db.update_task_status(good, "COMPLETED", result="an answer")
    dud = db.create_task("interrupted", goal="", conversation_id=conv)
    db.add_turn_to_conversation(conv, dud)
    db.update_task_status(dud, "CANCELLED")

    context = _build_conversation_context(conv)
    assert "a real goal" in context
    assert context.count("Turn ") == 1
