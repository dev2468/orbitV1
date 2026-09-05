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
from dotenv import load_dotenv
from PySide6.QtCore import (
    QAbstractNativeEventFilter,
    QObject,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui import theme

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
_MOD_NOREPEAT = 0x4000  # Windows 8+: suppress key-repeat WM_HOTKEY floods
_SAMPLE_RATE = 16_000
_BLOCK_MS = 20
_BLOCK_SIZE = _SAMPLE_RATE * _BLOCK_MS // 1000  # 320 samples per 20 ms

load_dotenv()  # ensure .env is loaded in the GUI process


# ── Phase 1 — Global hotkey ──────────────────────────────────────────────────

class _HotkeySignals(QObject):
    toggled = Signal()


class HotkeyFilter(QAbstractNativeEventFilter):
    """Intercepts Win32 WM_HOTKEY from Qt's native event loop.

    QAbstractNativeEventFilter is not a QObject, so Signal must live on a
    separate QObject (_HotkeySignals). Access via `filter.toggled`.

    Install with `QApplication.instance().installNativeEventFilter(filter)`.
    """

    def __init__(self, vk: int = _VK_F9) -> None:
        super().__init__()
        self._signals = _HotkeySignals()
        self._registered = False
        try:
            # MOD_NOREPEAT (0x4000) prevents key-repeat from firing WM_HOTKEY
            # repeatedly when the key is held down — without it every repeat
            # fires toggle(), spawning a new session thread each time.
            ok = ctypes.windll.user32.RegisterHotKey(
                None, _HOTKEY_ID, _MOD_NOREPEAT, vk
            )
            self._registered = bool(ok)
        except Exception:
            pass  # not on Windows, or another app holds the key

    @property
    def toggled(self):
        return self._signals.toggled

    def nativeEventFilter(self, event_type: bytes, message: object) -> tuple[bool, int]:
        if event_type == b"windows_generic_MSG":
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))  # type: ignore[arg-type]
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self._signals.toggled.emit()
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
        self._active = False      # True while mic+Deepgram session is live
        self._thread_alive = False  # True from thread-start to thread-exit
        self._cancelled = False   # True when the session is being discarded
        self._lock = threading.Lock()
        self._segments: list[str] = []
        self._interim: str = ""

    @property
    def is_active(self) -> bool:
        return self._active

    def toggle(self) -> None:
        with self._lock:
            if self._thread_alive:
                # Session is running — signal it to stop and keep the text
                self._active = False
            elif not self._active:
                # No session running — start one
                self._start_locked()

    def cancel(self) -> None:
        """Stop the session and throw the transcript away.

        The commit path (`toggle` a second time, or the modal's "Use
        transcript") ends with `transcript_ready` carrying the text; this one
        sets `_cancelled` so the session thread emits `session_stopped`
        *without* it. That distinction is the whole point of having two exits:
        Esc/Cancel must not drop half-heard audio into the goal box, and a
        `transcript_ready("")` would be indistinguishable from a silent
        recording that the user did mean to keep.

        A no-op when nothing is running, so a stray Esc is harmless.
        """
        with self._lock:
            if not self._thread_alive:
                return
            self._cancelled = True
            self._active = False

    def _start_locked(self) -> None:
        api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not _DEEPGRAM_AVAILABLE:
            print("[voice] deepgram-sdk not available", flush=True)
            return
        if not api_key:
            print("[voice] DEEPGRAM_API_KEY not set in .env", flush=True)
            return
        self._api_key = api_key
        self._active = True
        self._thread_alive = True
        self._cancelled = False
        self._segments = []
        self._interim = ""
        threading.Thread(target=self._session_thread, daemon=True).start()
        self.session_started.emit()

    # -- session thread -------------------------------------------------------

    def _session_thread(self) -> None:
        ctrl = self  # avoid name-shadowing `self` in nested closures

        try:
            print("[voice] connecting to Deepgram…", flush=True)
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
                    print("[voice] mic open, streaming…", flush=True)
                    stop_event.wait()  # blocks until _active flips or send_media fails

                # Gracefully flush then close the WebSocket.
                try:
                    conn.send_finalize()
                    conn.send_close_stream()
                except Exception:
                    pass
                listener_done.wait(timeout=3.0)

        except Exception as exc:
            print(f"[voice] session error: {exc}", flush=True)
        finally:
            with ctrl._lock:
                ctrl._active = False
                ctrl._thread_alive = False
                cancelled = ctrl._cancelled
            print("[voice] session ended", flush=True)

        # transcript_ready is the commit signal — skipping it on a cancel is
        # what makes Esc/Cancel discard rather than paste. session_stopped
        # still fires either way so the modal always closes.
        if not cancelled:
            parts = list(ctrl._segments)
            if ctrl._interim:
                parts.append(ctrl._interim)
            ctrl.transcript_ready.emit(" ".join(parts).strip())
        ctrl.session_stopped.emit()


# ── Phase 2 — Orb widget ─────────────────────────────────────────────────────

class OrbWidget(QWidget):
    """Pulsing orb driven by voice volume — the voice modal's centrepiece.

    RMS input goes through an envelope follower (fast attack, slow release,
    ticked at 60 fps) before painting so the orb swells with speech instead
    of strobing on every 20 ms audio frame.

    The gradient's focal point sits up and left of centre (the design's
    ``circle at 36% 36%``), which is what makes it read as a lit sphere
    rather than a flat disc. Qt expresses that as a QRadialGradient whose
    focal point differs from its centre — there is no CSS to copy here.
    """

    _BASE_R = 60          # 120px sphere at rest, per the design
    _MAX_EXTRA = 8        # swell headroom; the glow carries the rest
    _HIGHLIGHT = QColor(165, 180, 252)   # #A5B4FC
    _MID = QColor(129, 140, 248)         # #818CF8
    _CORE = QColor(99, 102, 241)         # #6366F1
    _DEEP = QColor(67, 56, 202)          # #4338CA

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._level = 0.0
        self._target = 0.0
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(168, 168)   # sphere + room for the glow rings
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def set_volume(self, rms: float) -> None:
        self._target = max(0.0, min(1.0, rms))

    def start(self) -> None:
        """Begin animating. Paired with stop() so a hidden orb costs nothing."""
        self._level = 0.0
        self._target = 0.0
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._level = 0.0
        self._target = 0.0
        self.update()

    def _tick(self) -> None:
        diff = self._target - self._level
        self._level += diff * (0.45 if diff > 0 else 0.07)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        r = self._BASE_R + self._level * self._MAX_EXTRA

        # Outer glow — stands in for the design's 56px box-shadow bloom,
        # which QSS cannot express on a round widget.
        for factor, alpha in ((1.38, 10), (1.20, 18), (1.08, 30)):
            g = QRadialGradient(cx, cy, r * factor)
            inner = QColor(self._CORE)
            inner.setAlpha(int(alpha * (0.55 + 0.45 * self._level)))
            g.setColorAt(0.55, inner)
            edge = QColor(self._CORE)
            edge.setAlpha(0)
            g.setColorAt(1.0, edge)
            p.setBrush(g)
            p.setPen(Qt.NoPen)
            fr = r * factor
            p.drawEllipse(int(cx - fr), int(cy - fr), int(fr * 2), int(fr * 2))

        # Sphere. Focal point at 36%/36% of the bounding box gives the
        # off-centre highlight.
        focal_x = cx - r * 0.28
        focal_y = cy - r * 0.28
        g = QRadialGradient(cx, cy, r, focal_x, focal_y)
        g.setColorAt(0.00, self._HIGHLIGHT)
        g.setColorAt(0.20, self._MID)
        g.setColorAt(0.50, self._CORE)
        g.setColorAt(0.85, self._DEEP)
        g.setColorAt(1.00, self._DEEP)
        p.setBrush(g)
        p.setPen(Qt.NoPen)
        p.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))
        p.end()


# ── Phase 2 — Voice modal ────────────────────────────────────────────────────

class VoiceModal(QFrame):
    """Centered voice overlay: orb, live transcript, Cancel / Use transcript.

    Two exits, and they mean different things — see `VoiceController.cancel`.
    `commit_requested` ends the session keeping the text; `cancel_requested`
    throws it away. Both are signals rather than direct controller calls so
    the modal stays a dumb view that main.py wires up.

    Positioning is manual (`center_on`) because this is an overlay, not a
    laid-out child: it is reparented onto the window's central widget and
    raised above it. A QDialog would have been the obvious route and is the
    wrong one — a real modal dialog steals the keyboard, and F9 has to keep
    reaching the main window's native event filter to toggle recording off.
    """

    cancel_requested = Signal()
    commit_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("voiceModal")
        self.setFixedWidth(theme.VOICE_MODAL_W)
        self.setStyleSheet(
            f"#voiceModal {{ background: {theme.SURFACE};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_2XL}px; }}"
        )
        theme.apply_drop_shadow(self, "lg")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 44, 40, 36)
        lay.setSpacing(0)
        lay.setAlignment(Qt.AlignHCenter)

        self.orb = OrbWidget()
        lay.addWidget(self.orb, alignment=Qt.AlignHCenter)
        lay.addSpacing(20)

        self.status_label = QLabel("Listening…")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet(
            f"font-size:16px;font-weight:700;color:{theme.TEXT_PRIMARY};background:transparent;"
        )
        lay.addWidget(self.status_label)
        lay.addSpacing(4)

        self.hint_label = QLabel("Press F9 to stop  ·  Esc to cancel")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setStyleSheet(
            f"font-size:13px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        lay.addWidget(self.hint_label)
        lay.addSpacing(26)

        # -- Transcript panel ---------------------------------------------
        transcript_card = QFrame()
        transcript_card.setObjectName("voiceTranscript")
        transcript_card.setStyleSheet(
            f"#voiceTranscript {{ background: {theme.INPUT_BG};"
            f" border-radius: {theme.RADIUS_LG}px; }}"
        )
        tc_lay = QVBoxLayout(transcript_card)
        tc_lay.setContentsMargins(20, 16, 20, 18)
        tc_lay.setSpacing(8)

        tc_title = QLabel("LIVE TRANSCRIPT")
        tc_title.setStyleSheet(
            f"font-size:10px;font-weight:700;color:{theme.TEXT_TERTIARY};"
            f"letter-spacing:0.5px;background:transparent;"
        )
        tc_lay.addWidget(tc_title)

        self.transcript_label = QLabel("")
        self.transcript_label.setWordWrap(True)
        self.transcript_label.setMinimumHeight(46)
        self.transcript_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.transcript_label.setStyleSheet(
            f"font-size:15px;color:{theme.TEXT_PRIMARY};font-style:italic;background:transparent;"
        )
        tc_lay.addWidget(self.transcript_label)

        lay.addWidget(transcript_card)
        lay.addSpacing(20)

        # -- Actions ---------------------------------------------------------
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.setContentsMargins(0, 0, 0, 0)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("voiceCancelBtn")
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.setFixedHeight(44)
        self.cancel_btn.setStyleSheet(
            f"#voiceCancelBtn {{ background:{theme.INPUT_BG}; border:none;"
            f" border-radius:{theme.RADIUS_MD}px; font-size:14px; font-weight:500;"
            f" color:{theme.TEXT_SECONDARY}; }}"
            f"#voiceCancelBtn:hover {{ background:{theme.BORDER}; color:{theme.TEXT_PRIMARY}; }}"
        )
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        actions.addWidget(self.cancel_btn, stretch=1)

        self.commit_btn = QPushButton("Use transcript  →")
        self.commit_btn.setObjectName("voiceCommitBtn")
        self.commit_btn.setCursor(Qt.PointingHandCursor)
        self.commit_btn.setFixedHeight(44)
        self.commit_btn.setStyleSheet(
            f"#voiceCommitBtn {{ background:{theme.ACCENT}; border:none;"
            f" border-radius:{theme.RADIUS_MD}px; font-size:14px; font-weight:600;"
            f" color:#FFFFFF; }}"
            f"#voiceCommitBtn:hover {{ background:{theme.ACCENT_HOVER}; }}"
            f"#voiceCommitBtn:disabled {{ background:{theme.BORDER_INPUT}; color:{theme.SURFACE}; }}"
        )
        self.commit_btn.clicked.connect(self.commit_requested.emit)
        actions.addWidget(self.commit_btn, stretch=1)

        lay.addLayout(actions)

    # -- view updates ---------------------------------------------------------

    def begin(self) -> None:
        self.transcript_label.setText("")
        self.status_label.setText("Listening…")
        self.commit_btn.setEnabled(False)
        self.orb.start()

    def end(self) -> None:
        self.orb.stop()

    def set_transcript(self, text: str) -> None:
        self.transcript_label.setText(text)
        # Nothing heard yet means nothing to commit — the button would
        # otherwise offer to paste an empty string into the goal box.
        self.commit_btn.setEnabled(bool(text.strip()))

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def center_on(self, host: QWidget) -> None:
        """Position over *host*, biased slightly above centre (the design's -54%)."""
        x = (host.width() - self.width()) // 2
        y = int((host.height() - self.sizeHint().height()) * 0.42)
        self.move(max(0, x), max(0, y))


class ScrimWidget(QWidget):
    """Dim layer behind a modal surface.

    Painted rather than styled: a QSS `background: rgba(...)` on a plain
    QWidget does not composite reliably against sibling widgets in Qt, while
    a paintEvent fill always does.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._color = QColor(28, 25, 23, 46)   # theme.SCRIM

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), self._color)
        p.end()
