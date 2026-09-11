"""Tests for voice as the primary input (Day 3).

Three things landed together and they interlock, so they are tested together:

* **auto-submit** — a finished transcript submits itself, so a spoken goal
  does not end at the keyboard;
* **deferred dispatch** — the work track waits ~1s for the acknowledgement to
  classify the turn, so a purely social one never spawns six MCP servers;
* **speaking back** — a finished task's result is re-written for speech.

The invariant that matters most here is a negative one: **a real task must
never fail to be dispatched.** Several tests exist only to pin that down on
each failure path, because the symptom — nothing happens, silently — is the
worst outcome this design can produce.
"""

from __future__ import annotations

import json

import httpx
import pytest
from PySide6.QtWidgets import QApplication

from orbit import ack
from orbit.ack import MARKER_CHAT, MARKER_TASK, MarkerSplitter


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["--platform", "offscreen"])
    return app


# --- MarkerSplitter ----------------------------------------------------------


def _drain(splitter: MarkerSplitter, *deltas: str) -> str:
    return "".join(splitter.feed(d) for d in deltas) + splitter.flush()


def test_marker_is_stripped_from_a_chat_reply():
    s = MarkerSplitter()
    assert _drain(s, f"{MARKER_CHAT} Hello there.") == "Hello there."
    assert s.chat_only is True


def test_marker_is_stripped_from_a_task_reply():
    s = MarkerSplitter()
    assert _drain(s, f"{MARKER_TASK} Looking that up now.") == "Looking that up now."
    assert s.chat_only is False


def test_marker_split_across_chunks_is_still_recognised():
    """A stream arrives in arbitrary pieces — the first delta was 2 characters
    in a real run. A startswith() check on the first chunk would miss this."""
    s = MarkerSplitter()
    assert _drain(s, "[", "CH", "AT", "] Hi", " there.") == "Hi there."
    assert s.chat_only is True


def test_one_character_at_a_time_still_works():
    s = MarkerSplitter()
    assert _drain(s, *list(f"{MARKER_TASK} Doing it.")) == "Doing it."
    assert s.chat_only is False


def test_a_reply_with_no_marker_is_treated_as_work():
    """The safe default. A missed marker costs a few seconds of unnecessary
    work; a wrongly-assumed CHAT means a real request silently does nothing."""
    s = MarkerSplitter()
    assert _drain(s, "Looking that up for you.") == "Looking that up for you."
    assert s.chat_only is False


def test_text_that_merely_starts_with_a_bracket_is_released():
    s = MarkerSplitter()
    assert _drain(s, "[not a marker] hello") == "[not a marker] hello"
    assert s.chat_only is False


def test_marker_is_matched_case_insensitively():
    s = MarkerSplitter()
    assert _drain(s, "[chat] Hey.") == "Hey."
    assert s.chat_only is True


def test_empty_stream_decides_work():
    s = MarkerSplitter()
    assert _drain(s) == ""
    assert s.chat_only is False


def test_nothing_is_held_back_after_the_marker_is_decided():
    """Once decided, every later delta passes straight through — otherwise
    the spoken reply would lag the generation by a chunk."""
    s = MarkerSplitter()
    s.feed(f"{MARKER_TASK} Start")
    assert s.feed("ing now.") == "ing now."


# --- the spoken summary ------------------------------------------------------


def _client_returning(body: bytes, capture: dict | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["body"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _sse(text: str) -> bytes:
    payload = json.dumps({"choices": [{"delta": {"content": text}}]})
    return f"data: {payload}\n\ndata: [DONE]\n\n".encode()


def test_spoken_summary_streams_text():
    client = _client_returning(_sse("It is twelve degrees."))
    out = "".join(ack.stream_spoken_summary(
        "**12C** and raining, see https://example.com",
        api_key="k", client=client,
    ))
    assert out == "It is twelve degrees."


def test_spoken_summary_of_an_empty_answer_makes_no_request():
    """Nothing to say. A request here would bill tokens for silence."""
    def handler(_request):  # pragma: no cover - must never be reached
        raise AssertionError("no request should be made")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert list(ack.stream_spoken_summary("   ", api_key="k", client=client)) == []


def test_spoken_summary_truncates_a_huge_answer():
    """A 50KB browser dump does not change a forty-word summary, and sending
    the tail costs real tokens on every task."""
    capture: dict = {}
    client = _client_returning(_sse("ok"), capture)
    list(ack.stream_spoken_summary("x" * 20_000, api_key="k", client=client))
    assert len(capture["body"]["messages"][-1]["content"]) < 7_000
    assert "truncated" in capture["body"]["messages"][-1]["content"]


def test_spoken_summary_sends_no_tools():
    capture: dict = {}
    client = _client_returning(_sse("ok"), capture)
    list(ack.stream_spoken_summary("done", api_key="k", client=client))
    assert "tools" not in capture["body"]


# --- the window: deferred dispatch ------------------------------------------


class _RecordingAck:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.summarized: list[str] = []
        self.cancelled = 0

    def start(self, goal, conversation_id=None):
        self.started.append(goal)

    def summarize(self, answer, goal=""):
        self.summarized.append(answer)

    def cancel(self):
        self.cancelled += 1

    def prewarm(self):
        pass

    def shutdown(self):
        pass


class _RecordingSpeech:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stops = 0
        self.is_speaking = False

    def enqueue(self, text):
        self.spoken.append(text)

    def stop(self):
        self.stops += 1

    def prewarm(self):
        pass

    def shutdown(self):
        pass


class _RecordingProc:
    def __init__(self) -> None:
        self.written: list[dict] = []

    def write(self, data):
        self.written.append(json.loads(bytes(data).decode()))

    def kill(self):
        return None

    def state(self):
        # QProcess.Running — _new_conversation checks this before sending the
        # close_conversation line to the worker.
        from PySide6.QtCore import QProcess

        return QProcess.Running


def _window(qapp, monkeypatch):
    from gui.main import OrbitWindow

    w = OrbitWindow()
    w._ack = _RecordingAck()
    w._speech = _RecordingSpeech()
    w._worker = _RecordingProc()
    monkeypatch.setattr(w, "_ensure_worker", lambda: None)
    return w


def _submit(w, goal: str, *, lane: str = "headless"):
    w.lane_toggle.set_value(lane)
    w.goal_input.setText(goal)
    w._submit_task()


def test_the_work_track_is_not_sent_before_classification(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "find me a tv")
    assert w._worker.written == []
    assert w._ack.started == ["find me a tv"]


def test_classifying_as_task_dispatches_the_work(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "find me a tv")
    w._on_ack_classified(False)
    assert [r["goal"] for r in w._worker.written] == ["find me a tv"]


def test_classifying_as_chat_never_dispatches(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "hi")
    w._on_ack_classified(True)
    assert w._worker.written == []


def test_a_chat_turn_leaves_the_goal_for_a_forced_retry(qapp, monkeypatch):
    """The recovery path for a wrong classification, and the reason it needs
    no new widget: the text stays put, and Enter forces the work track."""
    w = _window(qapp, monkeypatch)
    _submit(w, "what can you do")
    w._on_ack_classified(True)
    # The turn is not over until the reply finishes streaming — see
    # _on_ack_classified on why finishing at classification time would render
    # a half-written sentence.
    w._on_ack_completed("I can help with things on your computer.")
    assert w.goal_input.text() == "what can you do"

    w._submit_task()  # the user presses Enter on the same text
    assert [r["goal"] for r in w._worker.written] == ["what can you do"]


def test_a_chat_turn_stays_open_until_the_reply_finishes(qapp, monkeypatch):
    """`classified` fires on the stream's first tokens, with the sentence
    still arriving. Ending the turn there would run the final render over a
    half-written reply and then append the rest below it."""
    w = _window(qapp, monkeypatch)
    _submit(w, "hey there")
    w._on_ack_classified(True)
    assert w._task_running          # still open, reply still streaming

    w._on_ack_completed("Hi there, how can I help?")
    assert not w._task_running      # now it is done
    assert "Hi there, how can I help?" in w.output_text.toPlainText()


def test_a_forced_retry_does_not_wait_for_classification(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "hi")
    w._on_ack_classified(True)
    w._on_ack_completed("Hello.")
    w._submit_task()
    assert len(w._worker.written) == 1  # sent without any classify call


def test_the_guard_timer_dispatches_when_classification_never_arrives(qapp, monkeypatch):
    """Provider down, network gone: the task must still run."""
    w = _window(qapp, monkeypatch)
    _submit(w, "find me a tv")
    w._dispatch_pending_work()  # what the guard timer calls
    assert [r["goal"] for r in w._worker.written] == ["find me a tv"]


def test_dispatch_is_idempotent(qapp, monkeypatch):
    """Classification and the guard timer can both fire. Exactly one send."""
    w = _window(qapp, monkeypatch)
    _submit(w, "find me a tv")
    w._on_ack_classified(False)
    w._dispatch_pending_work()
    assert len(w._worker.written) == 1


def test_foreground_holds_the_full_window_even_once_classified(qapp, monkeypatch):
    """The foreground lane is the one that moves the real mouse. Its window is
    the user's chance to countermand a misheard goal, so an early
    classification must not short-circuit it."""
    w = _window(qapp, monkeypatch)
    _submit(w, "open notepad and type hello", lane="foreground")
    w._on_ack_classified(False)
    assert w._worker.written == []          # still held

    w._dispatch_pending_work()               # the timer, when it fires
    assert len(w._worker.written) == 1


def test_headless_does_not_wait_for_the_full_window(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "find me a tv", lane="headless")
    w._on_ack_classified(False)
    assert len(w._worker.written) == 1


def test_stopping_drops_a_goal_that_has_not_been_sent(qapp, monkeypatch):
    """Kill() cannot stop what was never written — it would arrive after."""
    w = _window(qapp, monkeypatch)
    _submit(w, "open notepad", lane="foreground")
    w._stop_task()
    w._dispatch_pending_work()
    assert w._worker.written == []


# --- the window: auto-submit and barge-in -----------------------------------


def test_a_transcript_arms_auto_submit(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w._on_transcript_ready("find me a tv")
    assert w._auto_submit_timer.isActive()
    assert w.goal_input.text() == "find me a tv"
    assert w._voice_originated


def test_auto_submit_actually_submits(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w._on_transcript_ready("find me a tv")
    w._auto_submit()
    assert w._ack.started == ["find me a tv"]


def test_escape_cancels_a_pending_auto_submit(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w._on_transcript_ready("find me a tv")
    w._on_escape()
    # The stopped timer is the assertion. Calling _auto_submit() by hand would
    # only prove that calling it submits, which is not what Esc prevents.
    assert not w._auto_submit_timer.isActive()
    assert w._ack.started == []


def test_a_transcript_during_a_running_task_does_not_auto_submit(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w._task_running = True
    w._on_transcript_ready("another thing")
    assert not w._auto_submit_timer.isActive()


def test_starting_to_listen_stops_speech(qapp, monkeypatch):
    """Barge-in. Without it the hotkey queues you behind an answer you have
    already moved on from."""
    w = _window(qapp, monkeypatch)

    class _Ctrl:
        is_active = False

        def toggle(self):
            self.toggled = True

    w._voice_ctrl = _Ctrl()
    w._toggle_voice()
    assert w._speech.stops >= 1


def test_stopping_listening_does_not_stop_speech(qapp, monkeypatch):
    """The second F9 press ends the recording. Nothing is playing then, and
    clearing the queue would cancel a reply that has just been queued."""
    w = _window(qapp, monkeypatch)

    class _Ctrl:
        is_active = True

        def toggle(self):
            pass

    w._voice_ctrl = _Ctrl()
    before = w._speech.stops
    w._toggle_voice()
    assert w._speech.stops == before


def test_escape_silences_speech_without_touching_the_task(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w._speech.is_speaking = True
    w._on_escape()
    assert w._speech.stops >= 1


# --- the window: speaking the result ----------------------------------------


def test_a_spoken_task_summarises_its_result(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w._on_transcript_ready("find me a tv")
    w._auto_submit()
    w._on_result_ready("COMPLETED", "The cheapest is the TCL at 42,990 rupees.")
    assert w._ack.summarized == ["The cheapest is the TCL at 42,990 rupees."]


def test_a_typed_task_does_not_summarise(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "find me a tv")
    w._on_ack_classified(False)
    w._on_result_ready("COMPLETED", "Some answer.")
    assert w._ack.summarized == []


def test_the_summary_is_spoken_sentence_by_sentence(qapp, monkeypatch):
    """Queued per sentence so playback starts on the first one instead of
    waiting for the whole thing to synthesise."""
    w = _window(qapp, monkeypatch)
    w._on_transcript_ready("find me a tv")
    w._auto_submit()
    w._on_summary_ready("The TCL is cheapest at 42,990. I saved a note.")
    assert w._speech.spoken == [
        "The TCL is cheapest at 42,990.", "I saved a note.",
    ]


def test_a_summary_for_a_typed_task_is_not_spoken(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "find me a tv")
    w._on_summary_ready("Something happened.")
    assert w._speech.spoken == []


# --- the recovery affordance for a wrong classification ---------------------


def test_a_chat_turn_reveals_the_rerun_button(qapp, monkeypatch):
    """A line of grey prose is not a recovery path. The button has to be
    visible in the two seconds before the user concludes they were ignored."""
    w = _window(qapp, monkeypatch)
    assert w.rerun_row.isHidden()

    _submit(w, "what can you do")
    w._on_ack_classified(True)
    w._on_ack_completed("I can help with things on your computer.")
    assert not w.rerun_row.isHidden()


def test_the_rerun_button_forces_the_work_track(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "what can you do")
    w._on_ack_classified(True)
    w._on_ack_completed("Sure.")

    w._force_task_rerun()
    assert [r["goal"] for r in w._worker.written] == ["what can you do"]
    assert w.rerun_row.isHidden()


def test_the_rerun_button_hides_on_the_next_submission(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "hello")
    w._on_ack_classified(True)
    w._on_ack_completed("Hi.")
    assert not w.rerun_row.isHidden()

    _submit(w, "something else entirely")
    assert w.rerun_row.isHidden()


def test_a_task_turn_never_shows_the_rerun_button(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "find me a tv")
    w._on_ack_classified(False)
    w._on_ack_completed("Looking now.")
    assert w.rerun_row.isHidden()


# --- how a finished task is reported ----------------------------------------


def test_a_failure_shows_the_workers_own_reason(qapp, monkeypatch):
    """The exit code alone cannot distinguish a provider outage from a tool
    bug. The `result` event carries the difference; the card must show it."""
    w = _window(qapp, monkeypatch)
    _submit(w, "do a thing")
    w._on_ack_classified(False)
    w._on_result_ready("FAILED", "Task failed: KeyError: 'session_id'")
    w._task_done(1)

    rendered = w.output_text.toPlainText()
    assert "Task failed" in rendered
    assert "KeyError" in rendered


def test_a_cancelled_task_is_not_reported_as_a_failure(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "do a thing")
    w._on_ack_classified(False)
    w._stop_task()
    w._task_done(1)

    rendered = w.output_text.toPlainText()
    assert "cancelled" in rendered.lower()
    assert "Task failed" not in rendered


def test_a_failure_with_no_reason_still_says_something_useful(qapp, monkeypatch):
    """A worker that dies without emitting `result` — a crash, a kill —
    leaves only the exit code. Say so rather than showing a bare number."""
    w = _window(qapp, monkeypatch)
    _submit(w, "do a thing")
    w._on_ack_classified(False)
    w._task_done(9)

    rendered = w.output_text.toPlainText()
    assert "Task failed" in rendered
    assert "9" in rendered


def test_a_successful_task_says_so(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "do a thing")
    w._on_ack_classified(False)
    w._on_result_ready("COMPLETED", "All done.")
    w._task_done(0)
    assert "completed successfully" in w.output_text.toPlainText().lower()


# --- the output pane is a thread, not a single exchange ---------------------


def test_finished_turns_stay_on_screen(qapp, monkeypatch):
    """"Conversational, not fire-and-forget" means seeing what you asked two
    turns ago. The pane used to clear on every submission."""
    w = _window(qapp, monkeypatch)

    _submit(w, "first question")
    w._on_ack_classified(False)
    w._raw_buffer = "First answer.\n"
    w._on_result_ready("COMPLETED", "First answer.")
    w._task_done(0)

    _submit(w, "second question")
    w._on_ack_classified(False)
    w._raw_buffer = "Second answer.\n"
    w._on_result_ready("COMPLETED", "Second answer.")
    w._task_done(0)

    rendered = w.output_text.toPlainText()
    assert "first question" in rendered and "First answer." in rendered
    assert "second question" in rendered and "Second answer." in rendered


def test_a_new_conversation_starts_an_empty_thread(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "first question")
    w._on_ack_classified(False)
    w._raw_buffer = "First answer.\n"
    w._on_result_ready("COMPLETED", "First answer.")
    w._task_done(0)

    w._new_conversation()
    assert w._turn_html == []
    assert "First answer." not in w.output_text.toPlainText()


def test_the_thread_is_bounded(qapp, monkeypatch):
    """QTextEdit re-lays out the whole document on setHtml, so an unbounded
    thread makes every later turn slower to render."""
    from gui.main import _MAX_THREAD_TURNS

    w = _window(qapp, monkeypatch)
    for i in range(_MAX_THREAD_TURNS + 5):
        _submit(w, f"question {i}")
        w._on_ack_classified(False)
        w._raw_buffer = f"answer {i}\n"
        w._on_result_ready("COMPLETED", f"answer {i}")
        w._task_done(0)

    assert len(w._turn_html) == _MAX_THREAD_TURNS
    rendered = w.output_text.toPlainText()
    assert "question 0" not in rendered      # oldest dropped
    assert f"question {_MAX_THREAD_TURNS + 4}" in rendered


def test_clearing_the_output_clears_the_thread(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    _submit(w, "a question")
    w._on_ack_classified(False)
    w._raw_buffer = "an answer\n"
    w._on_result_ready("COMPLETED", "an answer")
    w._task_done(0)

    w._clear_output()
    assert w._turn_html == []


# --- resuming an earlier chat ------------------------------------------------
#
# The worker already supported this: sending a goal with an existing
# conversation_id replays that conversation's stored turns into the prompt.
# What these cover is the GUI half — putting the thread back on screen and
# pointing new goals at the right conversation.


def _seed_conversation(n_turns: int = 2, *, with_empty: bool = False):
    from orbit import db

    conv = db.create_conversation(title="an earlier chat")
    for i in range(n_turns):
        task = db.create_task(f"t{i}", goal=f"question {i}", conversation_id=conv)
        db.add_turn_to_conversation(conv, task)
        db.update_task_status(task, "COMPLETED", result=f"answer {i}")
    if with_empty:
        task = db.create_task("interrupted", goal="", conversation_id=conv)
        db.add_turn_to_conversation(conv, task)
        db.update_task_status(task, "CANCELLED")
    return conv


def test_resuming_restores_the_thread_and_the_conversation(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    conv = _seed_conversation(3)

    w._resume_conversation(conv)

    assert w._conversation_id == conv
    rendered = w.output_text.toPlainText()
    for i in range(3):
        assert f"question {i}" in rendered
        assert f"answer {i}" in rendered


def test_a_goal_after_resuming_goes_to_that_conversation(qapp, monkeypatch):
    """The whole point: carry on in the old chat, not beside it."""
    w = _window(qapp, monkeypatch)
    conv = _seed_conversation(2)
    w._resume_conversation(conv)

    _submit(w, "a follow-up question")
    w._on_ack_classified(False)

    assert w._worker.written[-1]["conversation_id"] == conv


def test_resuming_keeps_earlier_turns_above_the_new_one(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    conv = _seed_conversation(2)
    w._resume_conversation(conv)

    _submit(w, "a follow-up question")
    w._on_ack_classified(False)
    w._raw_buffer = "the new answer\n"
    w._on_result_ready("COMPLETED", "the new answer")
    w._task_done(0)

    rendered = w.output_text.toPlainText()
    assert "question 0" in rendered
    assert "the new answer" in rendered


def test_a_turn_with_no_goal_is_skipped(qapp, monkeypatch):
    """An interrupted task leaves a row with nothing in it. A blank card in
    the thread reads as a rendering bug, not an abandoned turn."""
    w = _window(qapp, monkeypatch)
    conv = _seed_conversation(2, with_empty=True)
    w._resume_conversation(conv)
    assert len(w._turn_html) == 2


def test_resuming_an_empty_conversation_changes_nothing(qapp, monkeypatch):
    from orbit import db

    w = _window(qapp, monkeypatch)
    before = w._conversation_id
    w._resume_conversation(db.create_conversation(title="never used"))
    assert w._conversation_id == before


def test_resuming_is_refused_while_a_task_runs(qapp, monkeypatch):
    """Switching conversations mid-task would leave the running task writing
    into a thread the user is no longer looking at."""
    w = _window(qapp, monkeypatch)
    conv = _seed_conversation(2)
    _submit(w, "something in flight")
    w._resume_conversation(conv)
    assert w._conversation_id != conv


def test_resuming_releases_the_previous_conversation(qapp, monkeypatch):
    """Each cached runner holds six MCP subprocesses and possibly a browser."""
    w = _window(qapp, monkeypatch)
    old = w._conversation_id
    w._resume_conversation(_seed_conversation(1))

    closes = [r for r in w._worker.written if r.get("close_conversation")]
    assert closes and closes[-1]["close_conversation"] == old


def test_the_resumed_thread_is_bounded(qapp, monkeypatch):
    from gui.main import _MAX_THREAD_TURNS

    w = _window(qapp, monkeypatch)
    w._resume_conversation(_seed_conversation(_MAX_THREAD_TURNS + 6))
    assert len(w._turn_html) == _MAX_THREAD_TURNS


def test_the_picker_lists_past_chats_and_skips_the_open_one(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    conv = _seed_conversation(1)
    w._conversation_id = conv

    w._refresh_chat_picker()
    labels = [a.text() for a in w.chats_btn.menu().actions()]
    assert "an earlier chat" not in labels or labels.count("an earlier chat") == 0
