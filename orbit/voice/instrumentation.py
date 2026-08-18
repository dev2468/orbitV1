"""Per-utterance latency instrumentation — requirement 1 of the Deepgram
latency work: *instrument first, optimize second*. Without these numbers you
cannot tell which later fix actually helped, or whether a stage is already at
the India->US network floor (~200-300ms each way per round trip) and should
be left alone.

`UtteranceTimings` records monotonic timestamps for named stage boundaries and
formats the exact per-stage deltas the requirement asks for. It is written to
from THREE different threads — pynput's hotkey thread (hotkey_down /
hotkey_release / dispatched), PortAudio's callback thread indirectly, and
Deepgram's websocket thread (ws_ready / first_frame_sent / first_interim /
finalize_sent / last_is_final) — so every mutation takes a lock. That is cheap:
a mark is a dict write under a short lock, safe to call from the audio callback
and the hook thread.

Only one utterance is ever in flight at a time (push-to-talk is single-flight,
guarded by `VoiceRuntime._busy`), so the runtime keeps exactly one "current"
`UtteranceTimings` and routes the transcriber's `on_mark(name)` callback into
it. Marks that fire outside an utterance are simply dropped by the runtime
because there is no current timings object to route them to.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

# Stage-boundary mark names. Kept as module constants so the transcriber, the
# capture layer and the runtime all agree on the spelling without importing
# each other.
HOTKEY_DOWN = "hotkey_down"
WS_READY = "ws_ready"              # socket confirmed ready to accept this utterance's audio
FIRST_FRAME_SENT = "first_frame_sent"  # first binary audio frame actually put on the wire
FIRST_INTERIM = "first_interim"   # first interim (is_final=false) Results message with text
HOTKEY_RELEASE = "hotkey_release"
FINALIZE_SENT = "finalize_sent"   # {"type":"Finalize"} put on the wire
LAST_IS_FINAL = "last_is_final"   # most recent is_final Results — last write wins
DISPATCHED = "dispatched"         # transcript handed to run_task()
FIRST_UI = "first_ui"             # first visible UI change after dispatch (no UI exists yet)

# Marks whose LAST occurrence is the interesting one; everything else keeps its
# FIRST occurrence (e.g. "first_interim" must not be overwritten by later ones).
_LAST_WINS = frozenset({LAST_IS_FINAL})

# The report's stages: (label, from_mark, to_mark). Order matches requirement 1.
_STAGES: tuple[tuple[str, str, str], ...] = (
    ("hotkey down -> ws ready         ", HOTKEY_DOWN, WS_READY),
    ("hotkey down -> first frame sent  ", HOTKEY_DOWN, FIRST_FRAME_SENT),
    ("first frame -> first interim     ", FIRST_FRAME_SENT, FIRST_INTERIM),
    ("release     -> Finalize sent     ", HOTKEY_RELEASE, FINALIZE_SENT),
    ("Finalize    -> last is_final     ", FINALIZE_SENT, LAST_IS_FINAL),
    ("release     -> transcript ready  ", HOTKEY_RELEASE, DISPATCHED),
    ("transcript  -> first UI change   ", DISPATCHED, FIRST_UI),
)


class UtteranceTimings:
    """Monotonic marks for a single utterance plus a per-stage report.

    `mark(name)` is idempotent per name (first write wins) except for the
    names in `_LAST_WINS`, where the latest write wins — which is exactly what
    "last is_final" needs. All access is lock-guarded so the audio callback and
    websocket threads can mark freely.
    """

    def __init__(self) -> None:
        self._marks: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, name: str, *, at: Optional[float] = None) -> None:
        ts = at if at is not None else time.monotonic()
        with self._lock:
            if name in _LAST_WINS or name not in self._marks:
                self._marks[name] = ts

    def get(self, name: str) -> Optional[float]:
        with self._lock:
            return self._marks.get(name)

    def delta_ms(self, start: str, end: str) -> Optional[float]:
        with self._lock:
            a, b = self._marks.get(start), self._marks.get(end)
        if a is None or b is None:
            return None
        return (b - a) * 1000.0

    def report(self, *, prefix: str = "utterance timings") -> str:
        lines = [f"{prefix} (ms):"]
        for label, a, b in _STAGES:
            d = self.delta_ms(a, b)
            lines.append(f"  {label} {d:8.1f}" if d is not None else f"  {label}      n/a")
        return "\n".join(lines)
