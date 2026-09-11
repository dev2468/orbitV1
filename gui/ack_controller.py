"""Qt wrapper around the acknowledgement track (`orbit.ack`).

Mirrors what `gui/voice.py` does for Deepgram STT: the network work happens on
a daemon thread, and Qt signals are the only thing that crosses back to the
GUI thread. No widget is ever touched off the main thread.

## Why the acknowledgement runs here and not in the worker process

The warm worker (`orbit.run_task --serve`) is single-threaded and, at the
moment an acknowledgement is wanted, busy: it is connecting MCP servers for
the very goal being acknowledged. Asking it to also produce the fast reply
would serialise the two things that most need to happen at once.

Running it in the GUI process is what makes the two tracks genuinely
concurrent — the acknowledgement is already being spoken while the worker is
still spawning subprocesses. That is only affordable because `orbit.ack`
deliberately avoids litellm and google-adk (10.6s of imports the GUI does not
pay); see that module's docstring.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

import httpx
from PySide6.QtCore import QObject, Signal

from orbit import ack


class AckController(QObject):
    """Runs one acknowledgement at a time and streams it back as signals."""

    delta = Signal(str)       # incremental text, marker already stripped
    completed = Signal(str)   # the whole line, once
    failed = Signal(str)      # diagnostic only — never fatal to the task
    # True when the turn was purely social and there is nothing to do. Fires
    # as soon as the marker clears the stream — roughly one second in, well
    # before `completed` — because the window decides whether to dispatch the
    # work track on it, and every millisecond of that wait is added to every
    # real task.
    classified = Signal(bool)

    summary_completed = Signal(str)  # spoken-length version of a task result

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        # One shared client so TLS setup is paid once. `prewarm` fills the
        # connection pool at startup; without it the first acknowledgement of
        # a session pays roughly 0.6s more than every later one.
        self._client = httpx.Client(timeout=15.0)
        self._generation = 0
        self._summary_generation = 0
        self._lock = threading.Lock()

    def prewarm(self) -> None:
        """Open the HTTPS connection to OpenRouter ahead of first use.

        Skipped when `ORBIT_DISABLE_PREWARM` is set, which `tests/conftest.py`
        does: constructing an OrbitWindow would otherwise make a real network
        request per test, and the offline suite stays offline on purpose.
        """
        if os.environ.get("ORBIT_DISABLE_PREWARM"):
            return

        def _warm() -> None:
            try:
                self._client.get(
                    "https://openrouter.ai/api/v1/models", timeout=5.0
                )
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_warm, daemon=True).start()

    def start(self, goal: str, conversation_id: Optional[str] = None) -> None:
        """Begin an acknowledgement for `goal`, cancelling any in flight.

        Cancellation is by generation counter rather than by killing the
        thread: the HTTP read cannot be interrupted safely mid-stream, so a
        superseded run is allowed to finish and its output is discarded. The
        cost is one wasted short completion, which is the right trade against
        a torn connection that the *next* acknowledgement would pay for.
        """
        goal = (goal or "").strip()
        if not goal:
            return
        with self._lock:
            self._generation += 1
            generation = self._generation

        def _run() -> None:
            collected: list[str] = []
            splitter = ack.MarkerSplitter()
            announced = False
            try:
                history = ack.recent_context(conversation_id)
                for piece in ack.stream_ack(
                    goal, history=history, client=self._client
                ):
                    if generation != self._generation:
                        return  # superseded — drop it silently
                    visible = splitter.feed(piece)
                    if not announced and splitter.chat_only is not None:
                        announced = True
                        self.classified.emit(bool(splitter.chat_only))
                    if visible:
                        collected.append(visible)
                        self.delta.emit(visible)
            except Exception as exc:  # noqa: BLE001
                if generation == self._generation:
                    # Announce before reporting: the window gates the work
                    # track on `classified`, so a failure that emitted only
                    # `failed` would leave a real task never dispatched. False
                    # is the safe answer — run the work.
                    if not announced:
                        self.classified.emit(False)
                    self.failed.emit(f"{type(exc).__name__}: {exc}")
                return
            if generation != self._generation:
                return
            tail = splitter.flush()
            if tail:
                collected.append(tail)
                self.delta.emit(tail)
            if not announced:
                self.classified.emit(bool(splitter.chat_only))
            self.completed.emit("".join(collected).strip())

        threading.Thread(target=_run, daemon=True).start()

    def summarize(self, answer: str, goal: str = "") -> None:
        """Turn a finished task's result into something worth saying aloud.

        Runs on its own generation counter, not the acknowledgement's: a
        summary belongs to the task that just finished, and the user may well
        have started speaking the next goal before it arrives. Superseding the
        acknowledgement must not silently cancel this, or vice versa.
        """
        answer = (answer or "").strip()
        if not answer:
            return
        with self._lock:
            self._summary_generation += 1
            generation = self._summary_generation

        def _run() -> None:
            collected: list[str] = []
            try:
                for piece in ack.stream_spoken_summary(
                    answer, goal=goal, client=self._client
                ):
                    if generation != self._summary_generation:
                        return
                    collected.append(piece)
            except Exception as exc:  # noqa: BLE001
                if generation == self._summary_generation:
                    self.failed.emit(f"summary: {type(exc).__name__}: {exc}")
                return
            if generation == self._summary_generation:
                self.summary_completed.emit("".join(collected).strip())

        threading.Thread(target=_run, daemon=True).start()

    def cancel(self) -> None:
        """Discard whatever is in flight. Its signals will not be emitted.

        Both counters, because Stop means stop: a summary still generating for
        the task the user just abandoned would arrive and be spoken after they
        asked for silence.
        """
        with self._lock:
            self._generation += 1
            self._summary_generation += 1

    def shutdown(self) -> None:
        self.cancel()
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
