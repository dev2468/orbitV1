"""Speech output: text → Deepgram Aura → the speakers.

The other half of `gui/voice.py`. That module turns speech into text with
Deepgram Nova-3; this one turns text back into speech with Deepgram Aura, so
one `DEEPGRAM_API_KEY` covers both directions and no new dependency was added
for either — `deepgram-sdk` already ships the `speak` client, and
`sounddevice` already opens an audio device for the microphone.

## Shape: a queue, not a function call

`enqueue()` appends an utterance and returns immediately; one worker thread
synthesises and plays them in order. A plain blocking `speak()` would have
been shorter and is the wrong shape for what comes next — speaking a task's
final answer means feeding sentences in as the model writes them, and the
queue is what lets audio for sentence one start while sentence two is still
being generated. The acknowledgement path happens to enqueue exactly one item.

## Voice choice is a latency decision

Measured 2026-09-07, same sentence, warm connection:

    aura-asteria-en    0.27s to first byte, 0.30s fully synthesised
    aura-luna-en       0.27s / 0.29s
    aura-athena-en     0.30s / 0.31s
    aura-2-thalia-en   0.31s / 1.9s

The Aura-2 voices sound better and take roughly six times as long to finish
synthesising. Since every one of these produces ~5.7s of audio in well under
a second, playback starts on the first chunk and never underruns either way —
but Aura-1 is the safer default when the whole point is arriving quickly.
Override with `ORBIT_TTS_VOICE`.

## Two things cost ~0.9s each, once, and both are paid at startup instead

Measured on this machine, and neither is obvious from reading the code:

* **The first HTTPS request in the process** takes 0.89-1.02s against 0.27s
  warm. Worse, *abandoning* a response part-way leaves the connection unfit
  for reuse — an early-`break` prewarm measured 0.80s on the next real call,
  where a fully-drained one measured 0.27s. So `prewarm()` reads its throwaway
  response to the end. It looks wasteful and is the entire point.
* **`sd.RawOutputStream` open + start** takes 0.90s. That was most of the
  first utterance's latency, and it is why the output stream is opened once
  and kept, rather than per utterance. It closes itself after
  `_IDLE_CLOSE_SECONDS` of silence so a quiet session is not holding the audio
  device indefinitely.

Together they took the first spoken reply from 1.39s to roughly 0.3s.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Optional

import sounddevice as sd
from dotenv import load_dotenv
from PySide6.QtCore import QObject, Signal

from gui.spend import SpendGuard, tts_guard  # noqa: F401  (re-exported)

try:
    from deepgram import DeepgramClient
    _DEEPGRAM_AVAILABLE = True
except ImportError:
    _DEEPGRAM_AVAILABLE = False

load_dotenv()

_SAMPLE_RATE = 24_000
_DEFAULT_VOICE = "aura-asteria-en"

# How long the worker keeps the audio device open after the queue drains.
# Opening it costs 0.90s here, so re-opening per utterance would put that on
# every reply in a back-and-forth. Long enough to cover a conversation's
# natural gaps, short enough that walking away releases the device.
_IDLE_CLOSE_SECONDS = 45.0

# Bytes per audio frame on the output stream: int16 (2 bytes) x 1 channel.
#
# This number is why `_speak_one` buffers instead of writing each HTTP chunk
# straight through. Deepgram's chunked transfer splits the PCM stream at
# arbitrary BYTE boundaries, so a chunk can end half way through a 16-bit
# sample. sounddevice then refuses the write with
#
#     ValueError: len(data) not divisible by samplesize
#
# and the utterance dies mid-sentence. It is intermittent in the worst way:
# whether a split lands mid-sample depends on network packetisation, so the
# same text can play cleanly a hundred times and then fail.
_BYTES_PER_FRAME = 2

# A single utterance far past this is a bug somewhere upstream — an agent
# answer pasted whole into speech, say — not something the user wants read
# aloud in full. The daily budget lives in gui/spend.py; this is the per-call
# sanity limit.
_MAX_UTTERANCE_CHARS = 2_000


class SpeechPlayer(QObject):
    """Speaks queued text. One worker thread, cancellable mid-utterance.

    Signals are how anything reaches the GUI thread — the worker never touches
    a widget, the same rule `gui/voice.py` follows.
    """

    started = Signal()          # first audio of an utterance is going out
    finished = Signal()         # queue drained, nothing playing
    failed = Signal(str)        # diagnostic; speech is optional, never fatal

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._queue: queue.Queue[Optional[str]] = queue.Queue()
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._lock = threading.Lock()
        self._stream = None
        self._stream_lock = threading.Lock()
        self.guard = tts_guard()
        self.voice = os.environ.get("ORBIT_TTS_VOICE", "").strip() or _DEFAULT_VOICE

    # -- availability ---------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(_DEEPGRAM_AVAILABLE and os.environ.get("DEEPGRAM_API_KEY"))

    def _get_client(self):
        with self._lock:
            if self._client is None:
                self._client = DeepgramClient(
                    api_key=os.environ.get("DEEPGRAM_API_KEY", "")
                )
            return self._client

    def prewarm(self) -> None:
        """Pay the two one-off ~0.9s costs at startup, not on first speech.

        Synthesises a single character and reads the response **to the end**
        before discarding it. Draining is not tidiness: an abandoned response
        leaves the pooled connection unusable, and the next real request pays
        setup again (measured 0.80s after an early break, 0.27s after a full
        drain). The audio it throws away is a few milliseconds long.

        Also opens the output stream, which is the other 0.90s.

        Fire-and-forget on its own thread. A failure here is not worth
        reporting — the first real utterance will report it properly.

        Skipped when `ORBIT_DISABLE_PREWARM` is set, which `tests/conftest.py`
        does: constructing an OrbitWindow would otherwise open the machine's
        audio device and make a real Deepgram request once per test.
        """
        if not self.available or os.environ.get("ORBIT_DISABLE_PREWARM"):
            return

        def _warm() -> None:
            try:
                client = self._get_client()
                for _ in client.speak.v1.audio.generate(
                    text=".", model=self.voice, encoding="linear16",
                    sample_rate=_SAMPLE_RATE, container="none",
                ):
                    pass  # drained deliberately — see the docstring
            except Exception:  # noqa: BLE001
                pass
            try:
                self._ensure_stream()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_warm, daemon=True).start()

    # -- the shared output stream --------------------------------------------

    def _ensure_stream(self):
        with self._stream_lock:
            if self._stream is None:
                stream = sd.RawOutputStream(
                    samplerate=_SAMPLE_RATE, channels=1, dtype="int16"
                )
                stream.start()
                self._stream = stream
            return self._stream

    def _close_stream(self, *, abort: bool = False) -> None:
        with self._stream_lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            return
        try:
            stream.abort() if abort else stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self) -> None:
        """Release the audio device. Call on application close."""
        self.stop()
        self._close_stream(abort=True)

    # -- queue ----------------------------------------------------------------

    def enqueue(self, text: str) -> None:
        """Queue one utterance. Returns immediately."""
        text = (text or "").strip()
        if not text or not self.available:
            return
        if len(text) > _MAX_UTTERANCE_CHARS:
            text = text[:_MAX_UTTERANCE_CHARS]
        if self.guard.would_exceed(len(text)):
            self.failed.emit(
                f"Daily speech budget reached "
                f"({self.guard.spent_today():,}/{self.guard.daily_cap:,} chars). "
                f"Raise ORBIT_TTS_DAILY_CHAR_CAP in .env to continue."
            )
            return

        self._stop_flag.clear()
        self._queue.put(text)
        self._ensure_thread()

    def stop(self) -> None:
        """Cut playback and drop anything queued.

        This is what makes barge-in possible: pressing the hotkey while Orbit
        is talking should stop it talking, not queue the user behind it. Safe
        to call when nothing is playing.
        """
        self._stop_flag.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    @property
    def is_speaking(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    # -- worker ---------------------------------------------------------------

    def _run(self) -> None:
        """Speak the queue, then linger holding the audio device.

        The lingering is the point: re-opening the output stream costs 0.90s,
        so a thread that exited the moment the queue emptied would put that on
        every reply of a back-and-forth. It exits (releasing the device) after
        _IDLE_CLOSE_SECONDS of silence.

        `finished` fires when the queue drains, not when the thread exits, so
        a listener hears "done speaking" immediately rather than 45s later.
        """
        idle_since: Optional[float] = None
        announced_finished = True
        try:
            while not self._stop_flag.is_set():
                try:
                    text = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if not announced_finished:
                        announced_finished = True
                        self.finished.emit()
                    if idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since > _IDLE_CLOSE_SECONDS:
                        break
                    continue
                if text is None:
                    break
                idle_since = None
                announced_finished = False
                self._speak_one(text)
        finally:
            self._close_stream(abort=self._stop_flag.is_set())
            if not announced_finished:
                self.finished.emit()

    def _speak_one(self, text: str) -> None:
        try:
            client = self._get_client()
            audio = client.speak.v1.audio.generate(
                text=text, model=self.voice, encoding="linear16",
                sample_rate=_SAMPLE_RATE, container="none",
            )
            self.guard.record(len(text))

            first = True
            # Carries at most one byte between chunks: the trailing half of a
            # sample that the next chunk completes. See _BYTES_PER_FRAME.
            pending = b""
            for chunk in audio:
                if self._stop_flag.is_set():
                    break
                if not chunk:
                    continue

                buffered = pending + chunk
                whole = len(buffered) - (len(buffered) % _BYTES_PER_FRAME)
                pending = buffered[whole:]
                if not whole:
                    continue  # a chunk so small it did not complete one frame

                stream = self._ensure_stream()
                if first:
                    self.started.emit()
                    first = False
                stream.write(buffered[:whole])
            # Any leftover is a partial sample with no successor — one byte,
            # inaudible. Dropped rather than padded, because padding invents a
            # sample and produces a click.
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def split_sentences(text: str) -> list[str]:
    """Split on sentence boundaries, for feeding synthesis incrementally.

    Not a general sentence tokeniser and does not try to be — it exists so a
    long spoken answer can start playing before it has finished generating.
    Over-splitting costs one extra request; under-splitting costs a pause.
    Neither is a correctness problem, so the naive rule is the right one.
    """
    out: list[str] = []
    current = ""
    for char in text:
        current += char
        if char in ".!?\n" and len(current.strip()) > 1:
            out.append(current.strip())
            current = ""
    if current.strip():
        out.append(current.strip())
    return out
