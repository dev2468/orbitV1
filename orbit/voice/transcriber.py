"""Transcriber protocol + backends.

Moonshine is gone entirely (245M-param accuracy on accented English wasn't
good enough — a model-capacity problem, not a streaming-architecture one).
Deepgram nova-3 streaming is now primary; faster-whisper (already a
dependency for nothing else — added here) is the offline fallback when
Deepgram's WebSocket won't connect or drops mid-utterance. Both sit behind
one TranscriberBackend protocol, same reason the Moonshine build gave for
having the protocol at all: we've swapped the backend once already, this
proves out swapping it again without touching runtime.py.

Interface is callback-based (on_partial/on_final), not the old build's
poll-based partial_text()/final_text() — Deepgram delivers results as
server-pushed WebSocket messages on its own schedule, so a callback is the
natural shape, and a poll-based getter would just be a callback in
disguise with extra state to keep in sync.

LATENCY MODEL (the point of the Aug-2026 latency work): the Deepgram socket
is opened ONCE, at construction, and held open for the whole session with
application-level KeepAlive frames; a drop reconnects in the background
immediately. A hotkey press no longer pays a DNS+TLS+WS handshake — from
India that cold handshake was 800ms-1.5s, i.e. the whole "press, pause, THEN
speak" symptom. start_stream()/stop_and_flush() now only mark the beginning
and end of an utterance on the already-warm socket. See orbit/voice/CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable, Optional, Protocol, runtime_checkable
from urllib.parse import urlencode

import numpy as np

from orbit.voice import instrumentation as perf

logger = logging.getLogger("orbit.voice.transcriber")


@runtime_checkable
class TranscriberBackend(Protocol):
    """start_stream / feed_audio / on_partial / on_final / stop_and_flush / close.

    on_partial(text): interim (is_final=false) text — render as the greyed
    provisional tail per the home-screen mockup; it WILL change. [No such
    mockup file exists anywhere in this project as of this build — see
    tts.py and voice.yaml's privacy-copy comments for the same gap. The
    callback exists so a UI can bind to it once one exists; nothing
    currently subscribes.]

    on_final(text): fires once per settled (is_final=true) SEGMENT, in
    arrival order. A single sentence produces several of these — that's
    exactly why stop_and_flush()'s job is to concatenate them in order
    rather than have a caller keep only the latest.

    stop_and_flush(timeout): the "flush the tail" step. Stop feeding new
    audio, force whatever's still in flight to be processed, wait (bounded
    by timeout) for the remaining segments, return the full accumulated
    transcript for the utterance. Call exactly once per start_stream().

    close(): unconditional teardown, safe at any time including
    mid-utterance or after stop_and_flush() already ran.
    """

    def start_stream(self) -> None: ...
    def feed_audio(self, chunk: np.ndarray, sample_rate: int) -> None: ...
    def on_partial(self, callback: Callable[[str], None]) -> None: ...
    def on_final(self, callback: Callable[[str], None]) -> None: ...
    def stop_and_flush(self, timeout: float = 3.0) -> str: ...
    def close(self) -> None: ...


class TranscriberConnectError(Exception):
    """The Deepgram socket is not usable for this utterance (it hasn't
    finished its first connect, or is mid-reconnect after a drop).
    ResilientTranscriber catches this to fall back to offline transcription
    for THIS utterance while the background connection keeps warming itself
    for the next one."""


_DG_WS_URL = "wss://api.deepgram.com/v1/listen"

# Queue sentinels distinguishable from audio `bytes` payloads.
_FINALIZE = object()  # "flush the tail now": send {"type":"Finalize"}


class DeepgramTranscriber:
    """Nova-3 streaming over Deepgram's documented WebSocket wire protocol
    (developers.deepgram.com, checked live Aug 2026) via the `websockets`
    library directly — NOT the deepgram-sdk package. The SDK's Python API
    has gone through repeated incompatible reshapes across its 0.x -> 7.x
    releases; the wire protocol underneath — a JSON query string, binary
    audio frames, JSON control/result messages — is the far more stable
    thing to depend on directly, and it's small enough to own.

    ONE persistent connection for the whole session. A dedicated daemon
    thread owns an asyncio loop that:
      * opens the socket at warm_up() (called from __init__), sets `_ready`,
      * sends an application-level {"type":"KeepAlive"} every
        `keepalive_interval_s` so Deepgram doesn't drop the idle socket
        (NET-0001 after 10s of silence),
      * reconnects in the background the instant the socket drops — a good
        connection reconnects immediately; only a genuinely-failing connect
        backs off, and resets on success.

    A hotkey press calls start_stream() (begin utterance) and
    stop_and_flush() (Finalize + collect the tail); neither opens or closes
    the socket. feed_audio()/on_partial()/on_final() are called from
    whatever thread the caller is on (PortAudio's callback thread, here);
    audio crosses to the loop thread via call_soon_threadsafe onto an
    asyncio.Queue, which is natively cancellable — no blocking executor
    thread to leak on reconnect.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "nova-3",
        language: str = "multi",
        keyterms: Optional[list[str]] = None,
        on_cost: Optional[Callable[[float], None]] = None,
        cost_per_minute: float = 0.0058,
        connect_timeout: float = 5.0,
        keepalive_interval_s: float = 5.0,
        finalize_quiet_s: float = 0.25,
        reconnect_backoff_initial_s: float = 0.5,
        reconnect_backoff_max_s: float = 8.0,
        on_mark: Optional[Callable[[str], None]] = None,
        auto_connect: bool = True,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._language = language
        self._keyterms = keyterms or []
        self._on_cost = on_cost
        self._cost_per_minute = cost_per_minute
        self._connect_timeout = connect_timeout
        self._keepalive_interval_s = keepalive_interval_s
        self._finalize_quiet_s = finalize_quiet_s
        self._backoff_initial = reconnect_backoff_initial_s
        self._backoff_max = reconnect_backoff_max_s
        self._on_mark = on_mark

        self._on_partial_cb: Optional[Callable[[str], None]] = None
        self._on_final_cb: Optional[Callable[[str], None]] = None
        self._final_segments: list[str] = []
        self._segments_lock = threading.Lock()

        # Connection-thread state.
        self._conn_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._audio_q: "Optional[asyncio.Queue[object]]" = None
        self._manager_task: "Optional[asyncio.Task]" = None
        self._loop_ready = threading.Event()   # loop + queue constructed
        self._ready = threading.Event()         # socket connected & serving
        self._shutdown = threading.Event()

        # Per-utterance state.
        self._utterance_active = False
        self._first_frame_marked = False
        self._audio_seconds_sent = 0.0
        self._final_arrived = threading.Event()  # pulsed on each is_final

        if auto_connect:
            self.warm_up()

    # ------------------------------------------------------------------
    # Backend protocol.
    # ------------------------------------------------------------------

    def on_partial(self, callback: Callable[[str], None]) -> None:
        self._on_partial_cb = callback

    def on_final(self, callback: Callable[[str], None]) -> None:
        self._on_final_cb = callback

    def start_stream(self) -> None:
        """Begin an utterance on the already-warm socket. Non-blocking: it
        does NOT open a connection. Raises TranscriberConnectError if the
        socket isn't currently ready, so ResilientTranscriber falls back for
        this utterance while the background thread keeps reconnecting."""
        if self._shutdown.is_set():
            raise TranscriberConnectError("Deepgram transcriber is closed")
        if not self._ready.is_set():
            raise TranscriberConnectError(
                "Deepgram socket not ready (still warming or mid-reconnect)"
            )
        with self._segments_lock:
            self._final_segments = []
        self._audio_seconds_sent = 0.0
        self._first_frame_marked = False
        self._final_arrived.clear()
        self._drain_audio_queue()
        self._utterance_active = True
        self._mark(perf.WS_READY)

    def feed_audio(self, chunk: np.ndarray, sample_rate: int) -> None:
        if sample_rate != 16000:
            raise ValueError(f"DeepgramTranscriber expects 16000Hz audio, got {sample_rate}Hz")
        if self._shutdown.is_set():
            raise RuntimeError("Deepgram transcriber is closed")
        if not self._ready.is_set():
            # Dropped mid-utterance — surface so ResilientTranscriber replays
            # the buffered audio to the offline fallback.
            raise RuntimeError("Deepgram connection dropped mid-utterance")
        pcm16 = (np.clip(chunk, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        self._audio_seconds_sent += len(chunk) / sample_rate
        self._enqueue(pcm16)

    def stop_and_flush(self, timeout: float = 1.0) -> str:
        """Fast release (requirement 5): send Finalize, collect the remaining
        is_final segments, return. Does NOT send CloseStream and does NOT
        tear down the socket — it stays warm for the next utterance, so we
        never pay a round trip for teardown/reconnect.

        `timeout` is the HARD cap on waiting for the tail. Once is_final
        segments start arriving we return after `finalize_quiet_s` of quiet
        (requirement 5's "~250ms"); the hard cap only bites when the tail
        never comes (e.g. a silent release). On a high-RTT link the
        Finalize->last-is_final round trip is one such trip — item 1's
        measurement of that stage is how you tune both numbers."""
        self._final_arrived.clear()
        try:
            self._enqueue(_FINALIZE)
        except RuntimeError:
            # Socket went away between the last frame and here; just return
            # whatever we already have.
            self._utterance_active = False
            self._emit_cost()
            with self._segments_lock:
                return " ".join(s for s in self._final_segments if s).strip()

        hard_deadline = time.monotonic() + max(timeout, 0.0)
        got_any = False
        while True:
            now = time.monotonic()
            if now >= hard_deadline:
                break
            wait = self._finalize_quiet_s if got_any else (hard_deadline - now)
            wait = min(wait, hard_deadline - now)
            if wait <= 0:
                break
            if self._final_arrived.wait(wait):
                self._final_arrived.clear()
                got_any = True
                continue
            break  # quiet window elapsed with no new segment — tail is settled

        self._utterance_active = False
        self._emit_cost()
        with self._segments_lock:
            return " ".join(s for s in self._final_segments if s).strip()

    def cancel_utterance(self) -> None:
        """Abandon the current utterance WITHOUT closing the socket — used by
        ResilientTranscriber when it switches to the offline fallback so the
        warm connection survives for the next press. (Distinct from close(),
        which is full teardown.)"""
        self._utterance_active = False
        self._final_arrived.set()
        self._drain_audio_queue()

    def close(self) -> None:
        """Full teardown: stop KeepAlive/reconnect and join the thread."""
        self._shutdown.set()
        self._utterance_active = False
        self._ready.clear()
        self._final_arrived.set()
        loop, task = self._loop, self._manager_task
        if loop is not None and not loop.is_closed():
            def _cancel() -> None:
                if task is not None and not task.done():
                    task.cancel()
            try:
                loop.call_soon_threadsafe(_cancel)
            except RuntimeError:
                pass
        if self._conn_thread is not None and self._conn_thread.is_alive():
            self._conn_thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Warm-up + helpers.
    # ------------------------------------------------------------------

    def warm_up(self) -> None:
        """Open the persistent socket in the background (requirement 2:
        'open at app start, not on hotkey'). Idempotent."""
        if self._conn_thread is not None and self._conn_thread.is_alive():
            return
        self._shutdown.clear()
        self._conn_thread = threading.Thread(target=self._run, daemon=True, name="deepgram-ws")
        self._conn_thread.start()

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def _mark(self, name: str) -> None:
        if self._on_mark is None:
            return
        try:
            self._on_mark(name)
        except Exception:  # noqa: BLE001 — a timing hook must never break the stream
            logger.exception("on_mark callback raised")

    def _enqueue(self, item: object) -> None:
        loop, q = self._loop, self._audio_q
        if loop is None or q is None or loop.is_closed():
            raise RuntimeError("Deepgram connection is not running")
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError as exc:  # loop stopped between the check and the call
            raise RuntimeError(f"Deepgram connection is not running ({exc})") from exc

    def _drain_audio_queue(self) -> None:
        """Discard any residual queued audio (defensive; single-flight should
        leave it empty between utterances). Scheduled on the loop thread
        because asyncio.Queue is not thread-safe."""
        loop, q = self._loop, self._audio_q
        if loop is None or q is None or loop.is_closed():
            return

        def _drain() -> None:
            try:
                while True:
                    q.get_nowait()
            except asyncio.QueueEmpty:
                pass

        try:
            loop.call_soon_threadsafe(_drain)
        except RuntimeError:
            pass

    def _emit_cost(self) -> None:
        if self._on_cost is not None and self._audio_seconds_sent > 0:
            self._on_cost(self._audio_seconds_sent / 60.0 * self._cost_per_minute)

    def _build_url(self) -> str:
        params = [
            ("model", self._model),
            ("language", self._language),
            ("encoding", "linear16"),
            ("sample_rate", "16000"),
            ("channels", "1"),
            ("interim_results", "true"),  # requirement 4: MUST be true
            ("punctuate", "true"),
            ("smart_format", "true"),
        ]
        # Deliberately NO endpointing / vad_events / utterance_end_ms —
        # requirement 4: push-to-talk's own key-release IS the utterance
        # boundary, not a silence gap. We ignore speech_final entirely
        # (never read it below) rather than configuring it off, since an
        # unread field can't affect behavior either way; not touching the
        # param at all keeps the query string honest about that.
        for term in self._keyterms:
            params.append(("keyterm", term))
        return f"{_DG_WS_URL}?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Background thread: owns its own asyncio loop and the persistent socket.
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            asyncio.run(self._amain())
        except Exception:  # noqa: BLE001 — report, never let the thread die silently
            logger.exception("Deepgram connection thread crashed")

    async def _amain(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._audio_q = asyncio.Queue()
        self._manager_task = asyncio.current_task()
        self._loop_ready.set()
        try:
            await self._connection_manager()
        except asyncio.CancelledError:
            pass  # close() cancelled us — normal shutdown

    async def _connection_manager(self) -> None:
        import websockets

        url = self._build_url()
        delay = 0.0
        while not self._shutdown.is_set():
            if delay > 0:
                await asyncio.sleep(delay)
            if self._shutdown.is_set():
                break
            connected = False
            try:
                async with websockets.connect(
                    url,
                    additional_headers={"Authorization": f"Token {self._api_key}"},
                    open_timeout=self._connect_timeout,
                ) as ws:
                    connected = True
                    logger.info(
                        "Deepgram socket warm (keep-alive every %.0fs, %s/%s)",
                        self._keepalive_interval_s, self._model, self._language,
                    )
                    await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not self._shutdown.is_set():
                    logger.warning("Deepgram socket down (%s); reconnecting", exc)
            finally:
                self._ready.clear()
            if self._shutdown.is_set():
                break
            # A good connection that dropped reconnects immediately; a
            # connect that never succeeded backs off exponentially and resets
            # the moment one succeeds.
            delay = 0.0 if connected else (self._backoff_initial if delay == 0.0
                                           else min(delay * 2, self._backoff_max))

    async def _serve(self, ws) -> None:
        self._drain_audio_queue_on_loop()
        self._ready.set()
        sender = asyncio.ensure_future(self._sender(ws))
        receiver = asyncio.ensure_future(self._receiver(ws))
        keepalive = asyncio.ensure_future(self._keepalive(ws))
        tasks = {sender, receiver, keepalive}
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc is not None:
                raise exc  # propagate to _connection_manager -> reconnect

    def _drain_audio_queue_on_loop(self) -> None:
        # Already on the loop thread here (called from _serve).
        q = self._audio_q
        if q is None:
            return
        try:
            while True:
                q.get_nowait()
        except asyncio.QueueEmpty:
            pass

    async def _sender(self, ws) -> None:
        assert self._audio_q is not None
        while True:
            item = await self._audio_q.get()
            if item is _FINALIZE:
                await ws.send(json.dumps({"type": "Finalize"}))
                self._mark(perf.FINALIZE_SENT)
                continue
            if not isinstance(item, (bytes, bytearray)):
                continue
            await ws.send(item)
            if self._utterance_active and not self._first_frame_marked:
                self._first_frame_marked = True
                self._mark(perf.FIRST_FRAME_SENT)

    async def _keepalive(self, ws) -> None:
        # requirement 2: a JSON *control* frame, not audio — Deepgram does not
        # bill it and it produces no Results, so it can't be logged as a
        # transcription event or counted toward audio seconds. It only exists
        # to stop the idle-socket close (NET-0001 after ~10s).
        while True:
            await asyncio.sleep(self._keepalive_interval_s)
            await ws.send(json.dumps({"type": "KeepAlive"}))

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            mtype = msg.get("type")
            if mtype == "Results":
                if not self._utterance_active:
                    continue  # stray tail from a previous utterance's flush
                alternatives = msg.get("channel", {}).get("alternatives", [{}])
                text = (alternatives[0].get("transcript") or "").strip() if alternatives else ""
                if not text:
                    continue
                # Deliberately not reading msg["speech_final"] — requirement 4.
                if msg.get("is_final"):
                    with self._segments_lock:
                        self._final_segments.append(text)
                    self._mark(perf.LAST_IS_FINAL)
                    self._final_arrived.set()  # unblock stop_and_flush's collector
                    if self._on_final_cb:
                        try:
                            self._on_final_cb(text)
                        except Exception:  # noqa: BLE001
                            logger.exception("on_final callback raised")
                else:
                    self._mark(perf.FIRST_INTERIM)
                    if self._on_partial_cb:
                        try:
                            self._on_partial_cb(text)
                        except Exception:  # noqa: BLE001
                            logger.exception("on_partial callback raised")
            elif mtype == "Metadata":
                # We no longer send CloseStream, so this is not an
                # end-of-stream signal; ignore it and keep the socket open.
                continue
            elif mtype == "Error":
                logger.error("Deepgram reported an error: %s", msg)


class FasterWhisperTranscriber:
    """Offline fallback — requirement 6. Batch-only: buffers the whole
    utterance and transcribes once in stop_and_flush(), same shape as the
    retired MoonshineBatchTranscriber, because faster-whisper has no true
    streaming partial-decode API. on_partial() is accepted for interface
    conformance but never actually fires.

    Model size defaults to "small" as a speed/local-footprint compromise
    for a path that only exists to catch a dropped network mid-task — this
    has NOT been independently benchmarked here for accented-English WER
    against Deepgram (or the retired Moonshine model); it's a safety net,
    not a claim of matching nova-3's accuracy. Configurable via voice.yaml.
    """

    def __init__(self, model_size: str = "small", *, device: str = "cpu", compute_type: str = "int8"):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model = None
        self._buffer: list[np.ndarray] = []
        self._on_partial_cb: Optional[Callable[[str], None]] = None
        self._on_final_cb: Optional[Callable[[str], None]] = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        started = time.monotonic()
        self._model = WhisperModel(self._model_size, device=self._device, compute_type=self._compute_type)
        logger.info("faster-whisper %s loaded in %.1fs", self._model_size, time.monotonic() - started)

    def on_partial(self, callback: Callable[[str], None]) -> None:
        self._on_partial_cb = callback  # batch mode has no partials by design

    def on_final(self, callback: Callable[[str], None]) -> None:
        self._on_final_cb = callback

    def start_stream(self) -> None:
        self._ensure_model()
        self._buffer = []

    def feed_audio(self, chunk: np.ndarray, sample_rate: int) -> None:
        if sample_rate != 16000:
            raise ValueError(f"FasterWhisperTranscriber expects 16000Hz audio, got {sample_rate}Hz")
        self._buffer.append(chunk.copy())

    def stop_and_flush(self, timeout: float = 3.0) -> str:
        if not self._buffer:
            return ""
        audio = np.concatenate(self._buffer).astype(np.float32)
        self._buffer = []
        segments, _info = self._model.transcribe(audio, language="en", beam_size=5)
        parts: list[str] = []
        for seg in segments:
            t = seg.text.strip()
            if not t:
                continue
            parts.append(t)
            if self._on_final_cb:
                try:
                    self._on_final_cb(t)
                except Exception:  # noqa: BLE001
                    logger.exception("on_final callback raised")
        return " ".join(parts).strip()

    def cancel_utterance(self) -> None:
        # Batch backend holds no socket; abandoning an utterance is just
        # dropping the buffered audio. Mirrors DeepgramTranscriber's method so
        # ResilientTranscriber can treat either backend uniformly.
        self._buffer = []

    def close(self) -> None:
        self._buffer = []


class ResilientTranscriber:
    """Wraps a primary (Deepgram) and a fallback factory (faster-whisper),
    presenting exactly the TranscriberBackend shape — runtime.py never
    knows which one actually answered. Buffers this utterance's raw audio
    so a connect failure OR a mid-utterance drop can hand the SAME audio to
    the fallback rather than making the user repeat themselves.

    On switch, the fallback re-transcribes the ENTIRE buffered utterance
    from scratch and its result supersedes anything the primary had
    already produced — no attempt to merge partial-Deepgram-progress with
    fallback output, that's a much harder problem for a rare edge case.
    One consequence worth flagging for whenever a live-rendering UI exists:
    on_final segments already fired by the primary before a switch are
    superseded by the fallback's replay; such a UI should replace its
    displayed transcript when on_fallback() fires, not append to it.

    A switch to the fallback abandons the primary's *current utterance*
    (cancel_utterance) but does NOT close it — the persistent warm socket
    stays alive so the very next press can use Deepgram again. Only close()
    (runtime teardown) tears the primary down.
    """

    _MAX_BUFFERED_SAMPLES = 16000 * 60  # ~60s safety cap, independent of blocksize

    def __init__(
        self,
        primary: TranscriberBackend,
        fallback_factory: Callable[[], TranscriberBackend],
        *,
        on_fallback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._primary = primary
        self._fallback_factory = fallback_factory
        self._fallback: Optional[TranscriberBackend] = None
        self._active: TranscriberBackend = primary
        self._on_fallback = on_fallback
        self._buffer: list[tuple[np.ndarray, int]] = []
        self._buffered_samples = 0
        self._external_on_partial: Optional[Callable[[str], None]] = None
        self._external_on_final: Optional[Callable[[str], None]] = None
        self._using_fallback = False

    def on_partial(self, callback: Callable[[str], None]) -> None:
        self._external_on_partial = callback
        self._active.on_partial(callback)

    def on_final(self, callback: Callable[[str], None]) -> None:
        self._external_on_final = callback
        self._active.on_final(callback)

    def _end_primary_utterance(self) -> None:
        # Keep the warm socket alive across a fallback switch.
        cancel = getattr(self._primary, "cancel_utterance", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                logger.exception("primary.cancel_utterance raised")
        else:
            try:
                self._primary.close()
            except Exception:  # noqa: BLE001
                logger.exception("primary.close raised")

    def _switch_to_fallback(self, reason: str) -> None:
        logger.warning("switching to offline fallback transcriber: %s", reason)
        if self._on_fallback:
            try:
                self._on_fallback(reason)
            except Exception:  # noqa: BLE001
                logger.exception("on_fallback callback raised")
        self._end_primary_utterance()
        self._fallback = self._fallback_factory()
        if self._external_on_partial:
            self._fallback.on_partial(self._external_on_partial)
        if self._external_on_final:
            self._fallback.on_final(self._external_on_final)
        self._fallback.start_stream()
        for chunk, sr in self._buffer:
            self._fallback.feed_audio(chunk, sr)
        self._active = self._fallback
        self._using_fallback = True

    def start_stream(self) -> None:
        self._buffer = []
        self._buffered_samples = 0
        self._using_fallback = False
        self._active = self._primary
        try:
            self._primary.start_stream()
        except TranscriberConnectError as exc:
            self._switch_to_fallback(f"no connection to Deepgram ({exc})")
            return
        if self._external_on_partial:
            self._primary.on_partial(self._external_on_partial)
        if self._external_on_final:
            self._primary.on_final(self._external_on_final)

    def feed_audio(self, chunk: np.ndarray, sample_rate: int) -> None:
        self._buffer.append((chunk.copy(), sample_rate))
        self._buffered_samples += len(chunk)
        while self._buffered_samples > self._MAX_BUFFERED_SAMPLES and len(self._buffer) > 1:
            old_chunk, _ = self._buffer.pop(0)
            self._buffered_samples -= len(old_chunk)
        if self._using_fallback:
            self._active.feed_audio(chunk, sample_rate)
            return
        try:
            self._active.feed_audio(chunk, sample_rate)
        except Exception as exc:  # noqa: BLE001 — mid-utterance drop
            self._switch_to_fallback(f"connection dropped mid-utterance ({exc})")

    def stop_and_flush(self, timeout: float = 3.0) -> str:
        if self._using_fallback:
            return self._active.stop_and_flush(timeout)
        try:
            return self._primary.stop_and_flush(timeout)
        except Exception as exc:  # noqa: BLE001
            self._switch_to_fallback(f"connection dropped during flush ({exc})")
            return self._active.stop_and_flush(timeout)

    def close(self) -> None:
        self._primary.close()
        if self._fallback is not None:
            self._fallback.close()


def load_transcriber(
    config: dict,
    *,
    on_cost: Optional[Callable[[float], None]] = None,
    on_fallback: Optional[Callable[[str], None]] = None,
    on_mark: Optional[Callable[[str], None]] = None,
) -> TranscriberBackend:
    """Builds the ResilientTranscriber from orbit/config/voice.yaml values:
    deepgram_api_key, deepgram_model, deepgram_language, keyterms,
    deepgram_cost_per_minute_usd, fallback_model_size, plus the latency knobs
    (keepalive_interval_s, finalize_quiet_s, reconnect_backoff_*). Constructing
    the DeepgramTranscriber opens the persistent socket immediately."""
    primary = DeepgramTranscriber(
        config["deepgram_api_key"],
        model=config["deepgram_model"],
        language=config["deepgram_language"],
        keyterms=config.get("keyterms") or [],
        on_cost=on_cost,
        cost_per_minute=config["deepgram_cost_per_minute_usd"],
        keepalive_interval_s=config.get("keepalive_interval_s", 5.0),
        finalize_quiet_s=config.get("finalize_quiet_s", 0.25),
        reconnect_backoff_initial_s=config.get("reconnect_backoff_initial_s", 0.5),
        reconnect_backoff_max_s=config.get("reconnect_backoff_max_s", 8.0),
        on_mark=on_mark,
    )
    fallback_factory = lambda: FasterWhisperTranscriber(config["fallback_model_size"])
    return ResilientTranscriber(primary, fallback_factory, on_fallback=on_fallback)
