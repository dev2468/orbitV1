"""Audio capture — Section 2/11 of the architecture spec. sounddevice,
16kHz mono captured directly at that rate rather than resampled after the
fact — verified against moonshine_voice's own C API (Transcriber.add_audio /
transcribe_without_streaming both default sample_rate=16000 throughout;
mic_transcriber.py's own InputStream config uses the same 16000/mono shape)
that this is what the model actually wants natively.

Two capture modes (requirement 3 of the latency work):

* **push-to-talk only** (pre_buffer_enabled=False, the privacy-default): the
  InputStream is open ONLY while the hotkey is held — opened in start(),
  closed in stop(). Nothing is captured otherwise. This is the original
  behaviour.

* **rolling pre-buffer** (pre_buffer_enabled=True): the InputStream is held
  open continuously and every block is written into a small ring buffer
  holding the last ~pre_buffer_ms of audio. The ring is NEVER transmitted and
  is constantly overwritten; it exists only so that on hotkey-down we can
  PREPEND it to the stream, so speech that starts as (or a moment before) the
  key is pressed still gets captured. That removes the learned "click, wait,
  speak" behaviour. Because this means the microphone is open the whole time
  the runtime is running — even though the audio never leaves the ring unless
  you press the key — it is a deliberate, visible setting (see voice.yaml and
  privacy.py), not silent.

`is_active` means "currently recording an utterance", NOT "the stream object
exists" — so runtime.py's press/release/flush logic is identical in both
modes. Raw float32 mono chunks are handed to `on_chunk` from the PortAudio
callback thread; callers must not block in there — heavy work (feeding a
transcriber) belongs on a separate thread.

A separate `on_monitor` callback (used for barge-in) fires for EVERY block
while the stream is open, independent of whether we're recording — the runtime
uses it to watch for the user talking over a spoken reply. Enabling it
(`monitor_enabled`) forces the stream open continuously the same way the
pre-buffer does; the two share that "keep the mic open" mechanism.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
from typing import Any, Callable, Optional

import sounddevice as sd

logger = logging.getLogger("orbit.voice.capture")

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 512  # 32ms at 16kHz — requirement 4 wants 20-50ms frames (was 1024/64ms)


class AudioCaptureError(Exception):
    """Base for capture errors meant to reach the user as a clean message,
    never a raw traceback."""


class NoMicrophoneError(AudioCaptureError):
    pass


class MicrophoneBusyError(AudioCaptureError):
    pass


class MicrophonePermissionError(AudioCaptureError):
    pass


def _has_input_device() -> bool:
    """Proactive check before ever touching PortAudio, so "no microphone"
    is a clean pre-flight message rather than whatever exception text a
    failed stream-open happens to produce."""
    try:
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001 — PortAudio itself is unhappy; no mic either way
        return False
    return any(d.get("max_input_channels", 0) > 0 for d in devices)


def _classify_portaudio_error(exc: Exception) -> AudioCaptureError:
    """Best-effort classification of a PortAudio failure into a clean
    message. Confirmed empirically for the "bad/nonexistent device" case
    (sounddevice.PortAudioError, "Error querying device N") — busy-device
    and permission-denied phrasing is written defensively from documented
    PortAudio behavior, NOT independently reproduced on this machine: this
    machine's default input device is shared-mode (multiple opens don't
    conflict) and forcing a real Windows microphone-privacy denial means
    changing OS privacy settings, which this build does not do on its own.
    Falls through to a generic-but-still-clean message either way, so an
    unclassified failure is never a raw traceback, just a less specific
    string."""
    msg = str(exc).lower()
    if "permission" in msg or "access is denied" in msg or "unanticipated host error" in msg:
        return MicrophonePermissionError(
            "Microphone access was denied by Windows. Check Settings > "
            "Privacy & security > Microphone and allow desktop apps access, "
            "then try again."
        )
    if "busy" in msg or "unavailable" in msg or "already in use" in msg:
        return MicrophoneBusyError(
            "The microphone is in use by another application. Close it and "
            "try again."
        )
    return AudioCaptureError(f"Could not open the microphone: {exc}")


class PushToTalkCapture:
    """Owns exactly one InputStream. In push-to-talk mode it is open only
    while the key is held; in rolling-pre-buffer mode it is open continuously
    and feeds a ring buffer that is prepended to the stream on start().
    """

    def __init__(
        self,
        *,
        samplerate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        blocksize: int = BLOCKSIZE,
        device: Optional[object] = None,
        pre_buffer_enabled: bool = False,
        pre_buffer_ms: int = 500,
        monitor_enabled: bool = False,
        on_monitor: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.device = device
        self.pre_buffer_enabled = pre_buffer_enabled
        self.pre_buffer_ms = pre_buffer_ms
        self.monitor_enabled = monitor_enabled
        self._on_monitor = on_monitor
        # Either feature needs the stream held open continuously.
        self._continuous = pre_buffer_enabled or monitor_enabled
        self._stream: Optional[sd.InputStream] = None

        # Number of blocks needed to hold pre_buffer_ms of audio.
        block_ms = (blocksize / samplerate) * 1000.0 if blocksize else 0.0
        ring_len = max(1, math.ceil(pre_buffer_ms / block_ms)) if block_ms else 0
        self._ring: "collections.deque[Any]" = collections.deque(maxlen=ring_len)
        self._lock = threading.Lock()
        self._recording = False
        self._on_chunk: Optional[Callable[[Any], None]] = None

    @property
    def is_active(self) -> bool:
        # "recording an utterance", independent of whether the stream is open.
        return self._recording

    def open(self) -> None:
        """Start continuous capture into the ring buffer (rolling-pre-buffer
        mode only). Called once at runtime startup so the ring is warm before
        the first press. A failure here is logged, not raised — start() will
        retry and surface a clean error on the first press, exactly as in
        push-to-talk mode."""
        if not self._continuous:
            return
        try:
            self._ensure_stream()
        except AudioCaptureError as exc:
            logger.warning("could not start continuous capture at startup: %s", exc)

    def start(self, on_chunk: Callable[[Any], None]) -> None:
        """Begin recording an utterance. In pre-buffer mode this prepends the
        ring (the last ~pre_buffer_ms of audio) before any live block, so the
        transcriber sees audio from just before the key went down."""
        self._on_chunk = on_chunk
        self._ensure_stream()  # opens the stream if it isn't already (may raise)
        with self._lock:
            if self.pre_buffer_enabled and self._ring:
                for chunk in list(self._ring):
                    try:
                        on_chunk(chunk)
                    except Exception:  # noqa: BLE001 — never let a bad callback wedge start()
                        logger.exception("pre-buffer on_chunk callback raised")
            # Set recording under the same lock that guards ring appends, so
            # the pre-buffer is fully flushed before any live block is
            # forwarded — no out-of-order or dropped block at the boundary.
            self._recording = True

    def stop(self) -> None:
        """Stop recording. In pre-buffer mode the stream stays open (the ring
        keeps filling); in push-to-talk mode the stream is closed."""
        self._recording = False
        if not self._continuous:
            self._close_stream()

    def close(self) -> None:
        """Full teardown — close the stream regardless of mode. Called on
        runtime shutdown."""
        self._recording = False
        self._close_stream()

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------

    def _ensure_stream(self) -> None:
        if self._stream is not None:
            return
        if not _has_input_device():
            raise NoMicrophoneError(
                "No microphone was found. Plug one in or check Windows "
                "Sound settings, then try again."
            )

        def _callback(indata, frames, time_info, status) -> None:
            if status:
                logger.warning("capture status flags: %s", status)
            try:
                mono = indata[:, 0].copy()
                # Barge-in monitor: every block while the stream is open,
                # regardless of recording. Kept first and cheap — it decides
                # whether the user is talking over a spoken reply.
                if self._on_monitor is not None:
                    self._on_monitor(mono)
                with self._lock:
                    if self.pre_buffer_enabled:
                        self._ring.append(mono)
                    recording = self._recording
                    cb = self._on_chunk
                if recording and cb is not None:
                    cb(mono)
            except Exception:  # noqa: BLE001 — never let a bad callback kill the audio thread
                logger.exception("audio on_chunk callback raised")

        try:
            stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="float32",
                blocksize=self.blocksize,
                device=self.device,
                callback=_callback,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001 — classify, never let raw PortAudio errors surface
            raise _classify_portaudio_error(exc) from exc
        self._stream = stream

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001 — stopping must never raise
                logger.exception("error stopping audio stream")
            self._stream = None
        with self._lock:
            self._ring.clear()
