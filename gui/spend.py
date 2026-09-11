"""Daily spend guards for the metered voice APIs.

Deepgram bills both directions — per minute of audio for Nova-3 speech-to-text,
per character for Aura text-to-speech — and **nothing else in this codebase
watches either total**. `db.get_daily_cost` was written for exactly this and
has had no callers since the old voice runtime was deleted.

That mattered more after 2026-09-07 than before it. A transcript now submits
itself, and Orbit speaks its acknowledgement and its result, so both meters
run further per interaction than when voice was a manual push-to-talk box
that only listened.

## Why not an events row

`gui/CLAUDE.md`'s standing rule is that the GUI process never writes task or
event rows to `orbit.db` — `TaskManager` owns that state and a direct write
would desync it. A spend counter is not worth an exception to that, so this
keeps its own small JSON file. It is trivially editable by the user, which is
correct: it is their key and their money, and this is a guard rail rather than
a security boundary.

## Failure behaviour is deliberately asymmetric

Every read degrades to "nothing spent" and every write to a silent no-op. A
corrupt counter file must never stop the assistant from working. Under-counting
costs money slowly and visibly; refusing to speak because a JSON file has a
stray brace is a broken app.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
USAGE_PATH = _PROJECT_ROOT / "data" / "voice_usage.json"

# Aura is billed per character. 100k/day is roughly 3 hours of speech — far
# past any real session, close enough to notice a runaway loop.
DEFAULT_TTS_CHAR_CAP = 100_000

# Nova-3 is billed per minute of audio. 3600s/day is an hour of talking, which
# no one will reach by accident and a stuck-open microphone will.
DEFAULT_STT_SECOND_CAP = 3_600


class SpendGuard:
    """One named daily budget, persisted across restarts.

    Persisted rather than in-memory because the GUI is restarted constantly
    during development, and a per-session counter resets the budget every
    time — which is the same as having no budget at all.

    `bucket` names the counter inside the shared file, so speech-in and
    speech-out are tracked separately while sharing one small file and one
    date rollover.
    """

    def __init__(
        self,
        bucket: str = "chars",
        *,
        path: Optional[Path] = None,
        daily_cap: Optional[int] = None,
        env_var: Optional[str] = None,
        default_cap: int = DEFAULT_TTS_CHAR_CAP,
    ) -> None:
        self.bucket = bucket
        self.path = path or USAGE_PATH
        if daily_cap is None:
            daily_cap = default_cap
            if env_var:
                try:
                    daily_cap = int(os.environ.get(env_var, "") or default_cap)
                except ValueError:
                    daily_cap = default_cap
        self.daily_cap = daily_cap
        self._lock = threading.Lock()

    # -- storage --------------------------------------------------------------

    def _read_all(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def spent_today(self) -> int:
        data = self._read_all()
        if str(data.get("date", "")) != date.today().isoformat():
            return 0
        try:
            return int(data.get(self.bucket, 0))
        except (TypeError, ValueError):
            return 0

    def remaining_today(self) -> Optional[int]:
        """None when the cap is disabled."""
        if self.daily_cap <= 0:
            return None
        return max(0, self.daily_cap - self.spent_today())

    def would_exceed(self, amount: int) -> bool:
        if self.daily_cap <= 0:
            return False  # cap disabled
        return self.spent_today() + amount > self.daily_cap

    def record(self, amount: int) -> None:
        if amount <= 0:
            return
        with self._lock:
            today = date.today().isoformat()
            data = self._read_all()
            # A new day resets every bucket at once, so the file never carries
            # a stale counter for a direction that happened not to be used.
            if str(data.get("date", "")) != today:
                data = {"date": today}
            try:
                current = int(data.get(self.bucket, 0))
            except (TypeError, ValueError):
                current = 0
            data[self.bucket] = current + amount
            data["date"] = today
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(data), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


def tts_guard() -> SpendGuard:
    """Characters synthesised today (Deepgram Aura)."""
    return SpendGuard(
        "tts_chars",
        env_var="ORBIT_TTS_DAILY_CHAR_CAP",
        default_cap=DEFAULT_TTS_CHAR_CAP,
    )


def stt_guard() -> SpendGuard:
    """Seconds of microphone audio streamed today (Deepgram Nova-3)."""
    return SpendGuard(
        "stt_seconds",
        env_var="ORBIT_STT_DAILY_SECOND_CAP",
        default_cap=DEFAULT_STT_SECOND_CAP,
    )
