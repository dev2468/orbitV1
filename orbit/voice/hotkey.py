"""Global push-to-talk hotkey — Section 2/11 of the architecture spec
(pynput, "global, app-state-independent").

Deliberately built on pynput.keyboard.Listener (a raw low-level OS keyboard
hook), not GlobalHotKeys: GlobalHotKeys fires once on a combo press, with
no notion of "held" or "released" — push-to-talk needs both edges. A
low-level hook fires regardless of which window (if any) has focus, which
is what "works with the app window closed or unfocused" actually requires.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from pynput import keyboard

logger = logging.getLogger("orbit.voice.hotkey")

# pynput reports the SPECIFIC side of a modifier that was pressed
# (ctrl_l / ctrl_r), not a merged generic key, at least on Windows. A combo
# string like "ctrl+space" should match either side, so both directions of
# this map are used: parsing "ctrl" accepts either physical key, and
# checking a held key against the wanted combo normalizes it back to the
# generic name first.
_SIDED_TO_GENERIC = {
    keyboard.Key.ctrl_l: "ctrl",
    keyboard.Key.ctrl_r: "ctrl",
    keyboard.Key.shift_l: "shift",
    keyboard.Key.shift_r: "shift",
    keyboard.Key.alt_l: "alt",
    keyboard.Key.alt_r: "alt",
    keyboard.Key.alt_gr: "alt",
    keyboard.Key.cmd_l: "cmd",
    keyboard.Key.cmd_r: "cmd",
}
_GENERIC_NAMED_KEYS = {
    "ctrl": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "cmd": "cmd",
    "win": "cmd",
}


def _key_to_token(key) -> Optional[str]:
    """Normalize a pynput key event into a comparable token: modifier keys
    collapse to their generic name (either side counts), regular keys use
    their pynput name/char. Returns None for keys we can't identify (some
    media keys on some drivers report as None)."""
    if key in _SIDED_TO_GENERIC:
        return _SIDED_TO_GENERIC[key]
    if isinstance(key, keyboard.Key):
        return key.name
    # KeyCode (regular character keys)
    char = getattr(key, "char", None)
    if char:
        return char.lower()
    vk = getattr(key, "vk", None)
    return f"vk{vk}" if vk is not None else None


def parse_combo(combo_str: str) -> set[str]:
    """"ctrl+space" -> {"ctrl", "space"}. Raises ValueError on an unknown
    key name rather than silently building a combo that can never fire."""
    tokens = set()
    for part in combo_str.lower().split("+"):
        part = part.strip()
        if not part:
            continue
        if part in _GENERIC_NAMED_KEYS:
            tokens.add(_GENERIC_NAMED_KEYS[part])
        elif len(part) == 1:
            tokens.add(part)
        elif hasattr(keyboard.Key, part):
            tokens.add(part)
        else:
            raise ValueError(
                f"unknown key {part!r} in hotkey combo {combo_str!r} — "
                "use names like 'ctrl', 'shift', 'alt', 'space', or a "
                "single character"
            )
    if not tokens:
        raise ValueError(f"empty hotkey combo: {combo_str!r}")
    return tokens


class PushToTalkHotkey:
    """Fires on_press_combo() the moment every key in the combo is held,
    on_release_combo() the moment any one of them lifts. Both callbacks run
    on pynput's own listener thread — callers that need to touch an asyncio
    loop or another thread must hop over themselves (see runtime.py)."""

    def __init__(
        self,
        combo: str,
        on_press_combo: Callable[[], None],
        on_release_combo: Callable[[], None],
    ) -> None:
        self._combo = parse_combo(combo)
        self._combo_str = combo
        self._on_press_combo = on_press_combo
        self._on_release_combo = on_release_combo
        self._held: set[str] = set()
        self._active = False
        self._listener: Optional[keyboard.Listener] = None

    def _handle_press(self, key) -> None:
        token = _key_to_token(key)
        if token is None:
            return
        self._held.add(token)
        if not self._active and self._combo <= self._held:
            self._active = True
            try:
                self._on_press_combo()
            except Exception:  # noqa: BLE001 — never let a callback kill the hook thread
                logger.exception("push-to-talk on_press_combo callback raised")

    def _handle_release(self, key) -> None:
        token = _key_to_token(key)
        if token is None:
            return
        self._held.discard(token)
        if self._active and not (self._combo <= self._held):
            self._active = False
            try:
                self._on_release_combo()
            except Exception:  # noqa: BLE001
                logger.exception("push-to-talk on_release_combo callback raised")

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(
            on_press=self._handle_press, on_release=self._handle_release
        )
        self._listener.start()
        logger.info("push-to-talk hotkey %r listening (global, app-state-independent)", self._combo_str)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._held.clear()
        self._active = False
