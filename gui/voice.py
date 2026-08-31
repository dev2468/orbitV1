"""Voice input: Win32 hotkey → mic → Deepgram stream → transcript signals.

Deepgram SDK 7.x API (Fern-generated):
    client.listen.v1.connect(model="nova-3", ...) → context manager → V1SocketClient
    conn.on(EventType.MESSAGE, handler)   — handler(result: ListenV1Results)
    conn.start_listening()                — blocking recv loop (run in a thread)
    conn.send_media(bytes)                — send raw PCM from mic callback
    conn.send_close_stream()             — gracefully end the session
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import threading
from typing import Optional

import numpy as np
import sounddevice as sd
from PySide6.QtCore import (
    QAbstractNativeEventFilter,
    QObject,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QWidget

try:
    from deepgram import DeepgramClient
    from deepgram.core.events import EventType as DgEventType
    from deepgram.listen.v1.types import ListenV1Results
    _DEEPGRAM_AVAILABLE = True
except ImportError:
    _DEEPGRAM_AVAILABLE = False

# ── Win32 constants ──────────────────────────────────────────────────────────
_WM_HOTKEY = 0x0312
_HOTKEY_ID = 42
_VK_F9 = 0x78
_MOD_NONE = 0
_SAMPLE_RATE = 16_000
_BLOCK_MS = 20
_BLOCK_SIZE = _SAMPLE_RATE * _BLOCK_MS // 1000  # 320 samples per 20 ms


# ── Phase 1 — Global hotkey ──────────────────────────────────────────────────

class HotkeyFilter(QAbstractNativeEventFilter):
    """Intercepts Win32 WM_HOTKEY from Qt's native event loop.

    Install with `QApplication.instance().installNativeEventFilter(filter)`.
    The `toggled` signal fires on every press; VoiceController.toggle()
    treats alternating presses as start / stop.
    """

    toggled = Signal()

    def __init__(self, vk: int = _VK_F9) -> None:
        super().__init__()
        self._registered = False
        try:
            ok = ctypes.windll.user32.RegisterHotKey(None, _HOTKEY_ID, _MOD_NONE, vk)
            self._registered = bool(ok)
        except Exception:
            pass  # not on Windows, or another app holds the key

    def nativeEventFilter(self, event_type: bytes, message: object) -> tuple[bool, int]:
        if event_type == b"windows_generic_MSG":
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))  # type: ignore[arg-type]
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self.toggled.emit()
            except Exception:
                pass
        return False, 0

    def unregister(self) -> None:
        if self._registered:
            try:
                ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
            except Exception:
                pass
            self._registered = False

    def __del__(self) -> None:
        self.unregister()


# ── Phase 1 — Voice controller ───────────────────────────────────────────────

class VoiceController(QObject):
    """Manages one voice session: mic → Deepgram streaming STT → Qt signals.

    All Deepgram + sounddevice work runs in a daemon thread. Qt signals
    bridge results safely back to the main thread.

    Lifecycle (toggle-based):
        toggle() [1st press] → _start() → session_started, volume_rms, transcript_* …
        toggle() [2nd press] → _stop()  → session_stopped + transcript_ready(full_text)
    """

    session_started = Signal()
    session_stopped = Signal()
    volume_rms = Signal(float)              # 0.0–1.0, every 20 ms
    transcript_interim = Signal(str)        # live partial text
    transcript_final_segment = Signal(str)  # committed segment
    transcript_ready = Signal(str)          # complete text when session ends

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._active = False
        self._lock = threading.Lock()
        self._segments: list[str] = []
        self._interim: str = ""
        self._api_key = os.environ.get("DEEPGRAM_API_KEY", "")

    @property
    def is_active(self) -> bool:
        return self._active

    def toggle(self) -> None:
        with self._lock:
            if self._active:
                self._active = False  # signals session thread to wind down
            else:
                self._start_locked()

    def _start_locked(self) -> None:
        if not _DEEPGRAM_AVAILABLE or not self._api_key:
            return
        self._active = True
        self._segments = []
        self._interim = ""
        threading.Thread(target=self._session_thread, daemon=True).start()
        self.session_started.emit()

    # -- session thread -------------------------------------------------------

    def _session_thread(self) -> None:
        ctrl = self  # avoid name-shadowing `self` in nested closures

        try:
            client = DeepgramClient(api_key=ctrl._api_key)

            with client.listen.v1.connect(
                model="nova-3",
                language="multi",
                smart_format=True,
                interim_results=True,
                endpointing=400,
                sample_rate=_SAMPLE_RATE,
                encoding="linear16",
            ) as conn:

                # Register message handler — fires for every recognised result.
                def on_message(result) -> None:
                    if not _DEEPGRAM_AVAILABLE:
                        return
                    try:
                        if not isinstance(result, ListenV1Results):
                            return
                        alts = result.channel.alternatives
                        text = alts[0].transcript if alts else ""
                        if not text:
                            return
                        if result.is_final:
                            ctrl._segments.append(text)
                            ctrl.transcript_final_segment.emit(text)
                            ctrl._interim = ""
                        else:
                            ctrl._interim = text
                            ctrl.transcript_interim.emit(text)
                    except Exception:
                        pass

                conn.on(DgEventType.MESSAGE, on_message)

                # start_listening() is blocking — run it in its own thread so
                # our audio callback thread can concurrently call send_media().
                listener_done = threading.Event()

                def listen_loop() -> None:
                    try:
                        conn.start_listening()
                    finally:
                        listener_done.set()

                threading.Thread(target=listen_loop, daemon=True).start()

                # Mic callback — runs in sounddevice's own thread.
                stop_event = threading.Event()

                def audio_callback(indata: bytes, frames: int, time_info, status) -> None:
                    if not ctrl._active:
                        stop_event.set()
                        raise sd.CallbackStop()
                    try:
                        conn.send_media(bytes(indata))
                    except Exception:
                        stop_event.set()
                        raise sd.CallbackStop()
                    # RMS → orb
                    arr = np.frombuffer(indata, dtype=np.int16).astype(np.float32)
                    rms = float(np.sqrt(np.mean(arr ** 2))) / 32768.0
                    ctrl.volume_rms.emit(min(1.0, rms * 5.0))

                with sd.RawInputStream(
                    samplerate=_SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=_BLOCK_SIZE,
                    callback=audio_callback,
                ):
                    stop_event.wait()  # blocks until _active flips or send_media fails

                # Gracefully flush then close the WebSocket.
                try:
                    conn.send_finalize()
                    conn.send_close_stream()
                except Exception:
                    pass
                listener_done.wait(timeout=3.0)

        except Exception:
            pass
        finally:
            ctrl._active = False

        parts = list(ctrl._segments)
        if ctrl._interim:
            parts.append(ctrl._interim)
        ctrl.transcript_ready.emit(" ".join(parts).strip())
        ctrl.session_stopped.emit()


# ── Phase 2 — Orb widget ─────────────────────────────────────────────────────

class OrbWidget(QWidget):
    """Pulsing glassmorphic orb driven by voice volume.

    RMS input goes through an envelope follower (fast attack ~30 ms,
    slow release ~250 ms at 60 fps) before painting so the orb doesn't
    flicker with each audio frame.
    """

    _BASE_R = 44
    _MAX_EXTRA = 22
    _CORE = QColor(79, 70, 229)    # indigo-600
    _BRIGHT = QColor(129, 120, 255)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._level = 0.0
        self._target = 0.0
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(130, 130)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_volume(self, rms: float) -> None:
        self._target = max(0.0, min(1.0, rms))

    def _tick(self) -> None:
        diff = self._target - self._level
        self._level += diff * (0.45 if diff > 0 else 0.07)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        r = self._BASE_R + self._level * self._MAX_EXTRA

        for factor, alpha in ((2.0, 8), (1.55, 18), (1.2, 32)):
            g = QRadialGradient(cx, cy, r * factor)
            c = QColor(self._CORE)
            c.setAlpha(alpha)
            g.setColorAt(0.0, c)
            c2 = QColor(self._CORE)
            c2.setAlpha(0)
            g.setColorAt(1.0, c2)
            p.setBrush(g)
            p.setPen(Qt.NoPen)
            fr = r * factor
            p.drawEllipse(int(cx - fr), int(cy - fr), int(fr * 2), int(fr * 2))

        core_alpha = int(200 + self._level * 55)
        g = QRadialGradient(cx, cy, r)
        bright = QColor(self._BRIGHT)
        bright.setAlpha(core_alpha)
        g.setColorAt(0.0, bright)
        mid = QColor(self._CORE)
        mid.setAlpha(core_alpha)
        g.setColorAt(0.65, mid)
        g.setColorAt(1.0, QColor(55, 48, 163, core_alpha))
        p.setBrush(g)
        p.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))
        p.end()
