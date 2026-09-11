"""Tests for the acknowledgement track and speech output (Day 2).

Split by what can be checked offline:

* `orbit.ack` — the SSE parser and request shape, driven against a fake
  transport. No model call.
* `gui.speech` — the spend guard and sentence splitting, which are pure. The
  synthesis/playback path needs a real audio device and a real Deepgram key,
  so it is exercised by hand, not here.
* `gui.main` — that a submission starts BOTH tracks, and that only a spoken
  one gets spoken back. Driven with stubs in place of the network.
"""

from __future__ import annotations

import json

import httpx
import pytest
from PySide6.QtWidgets import QApplication

from gui.speech import split_sentences
from gui.spend import SpendGuard, stt_guard, tts_guard
from orbit import ack


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["--platform", "offscreen"])
    return app


def _sse(*chunks: str, include_noise: bool = True) -> bytes:
    """Build an OpenRouter-shaped SSE body."""
    lines = []
    if include_noise:
        # OpenRouter really does send these as keepalives while a model warms
        # up, and a parser that treats them as data yields garbage.
        lines.append(": OPENROUTER PROCESSING")
        lines.append("")
    for text in chunks:
        payload = {"choices": [{"delta": {"content": text}}]}
        lines.append(f"data: {json.dumps(payload)}")
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines).encode()


def _client_returning(body: bytes, *, capture: dict | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["headers"] = dict(request.headers)
            capture["body"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- orbit.ack ---------------------------------------------------------------


def test_stream_ack_yields_content_deltas():
    client = _client_returning(_sse("I'll ", "search ", "Amazon."))
    out = list(ack.stream_ack("find a tv", api_key="k", client=client))
    assert out == ["I'll ", "search ", "Amazon."]


def test_stream_ack_skips_keepalives_and_the_done_sentinel():
    """`: OPENROUTER PROCESSING` is a comment and `[DONE]` is not JSON. A
    parser that mishandles either produces junk in the spoken reply."""
    client = _client_returning(_sse("ok", include_noise=True))
    assert list(ack.stream_ack("go", api_key="k", client=client)) == ["ok"]


def test_stream_ack_skips_chunks_with_no_content():
    """A role announcement or a reasoning-only step carries a delta with no
    content — real on reasoning models, and a KeyError if unhandled."""
    body = "\n".join([
        'data: {"choices": [{"delta": {"role": "assistant"}}]}',
        "",
        'data: {"choices": []}',
        "",
        'data: {"choices": [{"delta": {"content": "hi"}}]}',
        "",
        "data: [DONE]",
        "",
    ]).encode()
    client = _client_returning(body)
    assert list(ack.stream_ack("go", api_key="k", client=client)) == ["hi"]


def test_stream_ack_survives_malformed_json_mid_stream():
    body = "\n".join([
        "data: {not json at all",
        "",
        'data: {"choices": [{"delta": {"content": "still here"}}]}',
        "",
        "data: [DONE]",
        "",
    ]).encode()
    client = _client_returning(body)
    assert list(ack.stream_ack("go", api_key="k", client=client)) == ["still here"]


def test_stream_ack_sends_no_tools_and_a_small_budget():
    """The whole reason this track is fast is that it carries none of the
    agent's apparatus. A regression that started sending tool declarations
    would silently put the ~5,900-token tool schema back on the fast path."""
    capture: dict = {}
    client = _client_returning(_sse("ok"), capture=capture)
    list(ack.stream_ack("find a tv", api_key="k", client=client))

    body = capture["body"]
    assert "tools" not in body
    assert body["stream"] is True
    assert body["max_tokens"] <= 60
    assert capture["headers"]["authorization"] == "Bearer k"


def test_stream_ack_strips_the_openrouter_routing_prefix():
    """`openrouter/` is LiteLLM's routing prefix. Sent to OpenRouter itself it
    is part of the model name and does not resolve."""
    capture: dict = {}
    client = _client_returning(_sse("ok"), capture=capture)
    list(ack.stream_ack(
        "go", api_key="k", client=client, model="openrouter/google/gemini-2.5-flash",
    ))
    assert capture["body"]["model"] == "google/gemini-2.5-flash"


def test_stream_ack_includes_history_when_given():
    capture: dict = {}
    client = _client_returning(_sse("ok"), capture=capture)
    list(ack.stream_ack(
        "do that again", api_key="k", client=client,
        history="User: open notepad\nOrbit: Done.",
    ))
    user_msg = capture["body"]["messages"][-1]["content"]
    assert "open notepad" in user_msg
    assert "do that again" in user_msg


def test_stream_ack_without_a_key_raises_a_useful_message(monkeypatch):
    # An explicit api_key="" means "fall back to the environment", so the
    # environment is what has to be empty for this to be the no-key case.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        list(ack.stream_ack("go", client=_client_returning(_sse("x"))))


def test_recent_context_is_empty_without_a_conversation():
    assert ack.recent_context(None) == ""
    assert ack.recent_context("") == ""


def test_recent_context_reads_turns(tmp_path, monkeypatch):
    from orbit import db

    db.init_db()
    conv = db.create_conversation(title="ack ctx")
    task = db.create_task("t", goal="open notepad", conversation_id=conv)
    db.add_turn_to_conversation(conv, task)
    db.update_task_status(task, "COMPLETED", result="Notepad is open.")

    context = ack.recent_context(conv)
    assert "open notepad" in context
    assert "Notepad is open." in context


def test_recent_context_survives_a_db_error(monkeypatch):
    """History is a nicety. Losing it must degrade the acknowledgement, not
    break it."""
    from orbit import db

    monkeypatch.setattr(
        db, "conversation_turns",
        lambda _cid: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    assert ack.recent_context("CONV-anything") == ""


# --- gui.speech (the pure halves) -------------------------------------------


def test_split_sentences_splits_on_terminators():
    assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]


def test_split_sentences_keeps_a_trailing_fragment():
    assert split_sentences("Done. And then") == ["Done.", "And then"]


def test_split_sentences_on_empty_input():
    assert split_sentences("") == []


def test_spend_guard_counts_only_today(tmp_path):
    guard = SpendGuard("chars", path=tmp_path / "usage.json", daily_cap=100)
    assert guard.spent_today() == 0
    guard.record(40)
    assert guard.spent_today() == 40
    guard.record(30)
    assert guard.spent_today() == 70


def test_spend_guard_blocks_past_the_cap(tmp_path):
    guard = SpendGuard("chars", path=tmp_path / "usage.json", daily_cap=100)
    guard.record(95)
    assert guard.would_exceed(10)
    assert not guard.would_exceed(5)


def test_spend_guard_resets_on_a_new_day(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"date": "1999-01-01", "chars": 999_999}))
    guard = SpendGuard("chars", path=path, daily_cap=100)
    assert guard.spent_today() == 0
    assert not guard.would_exceed(50)


def test_spend_guard_cap_of_zero_disables_it(tmp_path):
    guard = SpendGuard("chars", path=tmp_path / "usage.json", daily_cap=0)
    guard.record(10_000_000)
    assert not guard.would_exceed(10_000_000)


def test_spend_guard_tolerates_a_corrupt_file(tmp_path):
    """A broken counter must not stop the assistant speaking."""
    path = tmp_path / "usage.json"
    path.write_text("{{{ not json")
    guard = SpendGuard("chars", path=path, daily_cap=100)
    assert guard.spent_today() == 0
    guard.record(10)
    assert guard.spent_today() == 10


# --- the window starts both tracks ------------------------------------------


class _RecordingAck:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.cancelled = 0

    def start(self, goal, conversation_id=None):
        self.started.append((goal, conversation_id))

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

    def enqueue(self, text):
        self.spoken.append(text)

    def stop(self):
        self.stops += 1

    def prewarm(self):
        pass

    def shutdown(self):
        pass


def _window(qapp, monkeypatch):
    from gui.main import OrbitWindow

    w = OrbitWindow()
    w._ack = _RecordingAck()
    w._speech = _RecordingSpeech()
    # Never spawn a real worker process from a test.
    monkeypatch.setattr(w, "_ensure_worker", lambda: None)

    class _FakeProc:
        def write(self, _data):
            return None

        def kill(self):
            return None

    w._worker = _FakeProc()
    return w


def test_submitting_starts_the_ack_track(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w.goal_input.setText("find me a tv")
    w._submit_task()
    assert w._ack.started == [("find me a tv", w._conversation_id)]


def test_a_typed_goal_is_acknowledged_but_not_spoken(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w.goal_input.setText("find me a tv")
    w._submit_task()
    w._on_ack_completed("I'll look for a TV.")
    assert w._speech.spoken == []


def test_a_spoken_goal_is_spoken_back(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w._on_transcript_ready("find me a tv")
    assert w._voice_originated
    w._submit_task()
    w._on_ack_completed("I'll look for a TV.")
    assert w._speech.spoken == ["I'll look for a TV."]


def test_voice_origin_does_not_leak_into_the_next_typed_goal(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w._on_transcript_ready("first, spoken")
    w._submit_task()
    w._task_running = False

    w.goal_input.setText("second, typed")
    w._submit_task()
    w._on_ack_completed("Second reply.")
    assert w._speech.spoken == []


def test_stopping_cancels_the_ack_and_the_speech(qapp, monkeypatch):
    w = _window(qapp, monkeypatch)
    w._on_transcript_ready("do a thing")
    w._submit_task()
    w._stop_task()
    assert w._ack.cancelled == 1
    assert w._speech.stops >= 1


def test_ack_text_survives_the_final_render(qapp, monkeypatch):
    """The ack streams into the pane, then _render_final_output replaces the
    pane from _raw_buffer — which never held it. It has to be re-added."""
    w = _window(qapp, monkeypatch)
    w.goal_input.setText("find me a tv")
    w._submit_task()
    w._on_ack_completed("I'll look for a TV.")
    w._raw_buffer = "The cheapest is 40,000 rupees.\n"
    w._render_final_output(0)
    assert "I'll look for a TV." in w.output_text.toPlainText()


# --- the two voice budgets --------------------------------------------------
#
# Deepgram bills both directions and nothing else in the codebase watches
# either — db.get_daily_cost has had no callers since the old voice runtime
# was removed. These pin that the guards are wired to *different* counters,
# because sharing one would let speech output silently spend the microphone's
# budget and vice versa.


def test_the_two_guards_use_separate_counters(tmp_path):
    tts = SpendGuard("tts_chars", path=tmp_path / "voice.json", daily_cap=100)
    stt = SpendGuard("stt_seconds", path=tmp_path / "voice.json", daily_cap=100)

    tts.record(60)
    assert tts.spent_today() == 60
    assert stt.spent_today() == 0, "speech output spent the microphone's budget"

    stt.record(10)
    assert tts.spent_today() == 60, "the microphone spent the speech budget"
    assert stt.spent_today() == 10


def test_a_new_day_resets_every_counter(tmp_path):
    """A stale bucket for a direction that happened not to be used yesterday
    must not survive the rollover."""
    path = tmp_path / "voice.json"
    path.write_text(json.dumps(
        {"date": "1999-01-01", "tts_chars": 999, "stt_seconds": 999}
    ))
    assert SpendGuard("tts_chars", path=path, daily_cap=100).spent_today() == 0
    assert SpendGuard("stt_seconds", path=path, daily_cap=100).spent_today() == 0


def test_recording_zero_or_negative_is_a_no_op(tmp_path):
    guard = SpendGuard("tts_chars", path=tmp_path / "voice.json", daily_cap=100)
    guard.record(0)
    guard.record(-5)
    assert guard.spent_today() == 0


def test_remaining_today_is_none_when_the_cap_is_disabled(tmp_path):
    assert SpendGuard("x", path=tmp_path / "v.json", daily_cap=0).remaining_today() is None
    g = SpendGuard("x", path=tmp_path / "v.json", daily_cap=100)
    g.record(30)
    assert g.remaining_today() == 70


def test_the_factory_guards_are_distinct_and_env_driven(monkeypatch):
    monkeypatch.setenv("ORBIT_TTS_DAILY_CHAR_CAP", "1234")
    monkeypatch.setenv("ORBIT_STT_DAILY_SECOND_CAP", "77")
    assert tts_guard().daily_cap == 1234
    assert stt_guard().daily_cap == 77
    assert tts_guard().bucket != stt_guard().bucket


def test_a_bad_env_cap_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("ORBIT_TTS_DAILY_CHAR_CAP", "not-a-number")
    assert tts_guard().daily_cap > 0


def test_voice_controller_refuses_to_open_the_mic_past_its_budget(tmp_path, monkeypatch):
    """Pressing F9 and getting silence is indistinguishable from a broken
    microphone, so the refusal has to be announced, not just logged."""
    from gui.voice import VoiceController

    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake-key-for-this-test")
    ctrl = VoiceController()
    ctrl.guard = SpendGuard("stt_seconds", path=tmp_path / "v.json", daily_cap=10)
    ctrl.guard.record(999)

    seen: list[str] = []
    ctrl.budget_exceeded.connect(seen.append)
    ctrl.toggle()

    assert not ctrl.is_active, "the microphone opened past its daily budget"
    assert seen and "budget" in seen[0].lower()


# --- audio frame alignment ---------------------------------------------------
#
# Regression test for a real failure seen in use:
#
#     [speech] ValueError: len(data) not divisible by samplesize
#
# The output stream is int16 mono — 2 bytes per frame — and Deepgram's chunked
# transfer splits the PCM at arbitrary BYTE boundaries, so a chunk can end half
# way through a sample. Writing chunks straight through therefore fails
# whenever a split happens to land mid-sample, which depends on network
# packetisation: the same sentence plays a hundred times and then dies.


class _FakeStream:
    """Stands in for sd.RawOutputStream, with its actual constraint."""

    def __init__(self):
        self.written = bytearray()

    def write(self, data):
        if len(data) % 2:
            raise ValueError("len(data) not divisible by samplesize")
        self.written.extend(data)


def _speak_with_chunks(monkeypatch, chunks):
    from gui.speech import SpeechPlayer

    player = SpeechPlayer()
    stream = _FakeStream()
    monkeypatch.setattr(player, "_ensure_stream", lambda: stream)
    monkeypatch.setattr(player, "_get_client", lambda: None)
    monkeypatch.setattr(player.guard, "record", lambda _n: None)

    class _Speak:
        class v1:
            class audio:
                @staticmethod
                def generate(**_kw):
                    return iter(chunks)

    monkeypatch.setattr(player, "_get_client", lambda: type(
        "C", (), {"speak": _Speak}
    )())

    failures: list[str] = []
    player.failed.connect(failures.append)
    player._speak_one("hello")
    return stream, failures


def test_odd_sized_chunks_do_not_break_playback(monkeypatch):
    """The exact shape that failed: a chunk ending mid-sample."""
    stream, failures = _speak_with_chunks(monkeypatch, [b"\x01\x02\x03", b"\x04\x05\x06"])
    assert failures == [], failures
    # All six bytes are three whole frames once rejoined.
    assert bytes(stream.written) == b"\x01\x02\x03\x04\x05\x06"


def test_every_byte_survives_an_awkward_split(monkeypatch):
    """Buffering must not drop audio — a lost byte shifts every later sample
    and turns the rest of the utterance into noise."""
    payload = bytes(range(256)) * 4
    chunks, i = [], 0
    for size in (1, 7, 3, 101, 5, 1, 199, 2):   # deliberately odd sizes
        chunks.append(payload[i:i + size])
        i += size
    chunks.append(payload[i:])

    stream, failures = _speak_with_chunks(monkeypatch, chunks)
    assert failures == []
    assert bytes(stream.written) == payload[:len(payload) - len(payload) % 2]


def test_a_trailing_half_sample_is_dropped_not_padded(monkeypatch):
    """One orphan byte is inaudible; padding it invents a sample and clicks."""
    stream, failures = _speak_with_chunks(monkeypatch, [b"\x01\x02\x03"])
    assert failures == []
    assert bytes(stream.written) == b"\x01\x02"


def test_a_chunk_too_small_to_complete_a_frame_is_held(monkeypatch):
    stream, failures = _speak_with_chunks(monkeypatch, [b"\x01", b"\x02"])
    assert failures == []
    assert bytes(stream.written) == b"\x01\x02"
