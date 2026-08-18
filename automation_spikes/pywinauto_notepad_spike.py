"""Second spike (tech-stack-review Section 11, item 3): pywinauto driving a
real native Windows app end to end via the UI Automation tree — structured
automation before any vision fallback, matching Section 6/11 of the spec.

Opens Notepad, sets text via the UIA tree (clipboard paste, not char-by-char
type_keys — Windows 11's packaged Notepad has autocomplete/IME behavior that
corrupts fast character-simulated typing), reads it back to verify, then
force-kills the process.
"""

from __future__ import annotations

import subprocess
import sys
import time

import win32clipboard
from pywinauto import Application, Desktop
from pywinauto.timings import wait_until_passes

TEST_TEXT = "orbit pywinauto spike - structured automation check"


def _set_clipboard_text(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def _kill_stray_notepads() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "notepad.exe", "/T"],
        capture_output=True,
    )


def main() -> int:
    _kill_stray_notepads()
    time.sleep(0.3)

    Application(backend="uia").start("notepad.exe")
    app = None
    try:
        # Modern (MSIX-packaged) Windows 11 Notepad relaunches itself under a
        # different PID than the one Application.start() tracks, so
        # app.top_window() can't find it by process. Search the desktop by
        # title instead, which is process-agnostic.
        def _find_window():
            spec = Desktop(backend="uia").window(title_re=".*Notepad.*")
            if not spec.exists():
                raise RuntimeError("notepad window not found yet")
            return spec

        found = wait_until_passes(10, 0.5, _find_window)
        # Resolve to a concrete handle once, then rebind every subsequent
        # lookup to that handle rather than re-matching by title regex each
        # call — the lazy WindowSpecification re-queries by title on every
        # attribute access, which is flaky while the window is still
        # settling right after launch.
        handle = found.wrapper_object().handle
        app = Application(backend="uia").connect(handle=handle)
        win = app.window(handle=handle)
        win.wait("visible", timeout=10)

        edit = None
        for kwargs in ({"control_type": "Document"}, {"control_type": "Edit"}):
            try:
                candidate = win.child_window(**kwargs)
                candidate.wait("exists enabled visible", timeout=5)
                edit = candidate
                break
            except Exception:
                continue

        if edit is None:
            print("Could not locate the text edit control. Control tree:")
            win.print_control_identifiers()
            return 1

        win.set_focus()
        edit.set_focus()
        time.sleep(0.3)

        # Clipboard paste, not char-simulated type_keys: avoids autocomplete/
        # IME interference corrupting fast keystroke simulation.
        _set_clipboard_text(TEST_TEXT)
        edit.type_keys("^a{DEL}^v", pause=0.05)

        def _read_current_text() -> str:
            for attempt in (
                lambda: edit.window_text(),
                lambda: edit.get_value(),
                lambda: edit.iface_text.DocumentRange.GetText(-1),
            ):
                try:
                    text = attempt()
                    if text:
                        return text
                except Exception:
                    continue
            return ""

        def _read_back() -> str:
            text = _read_current_text()
            if TEST_TEXT not in text:
                raise AssertionError(f"text not yet present, got: {text!r}")
            return text

        read_text = wait_until_passes(5, 0.25, _read_back)

        ok = TEST_TEXT in read_text
        print(f"typed:     {TEST_TEXT!r}")
        print(f"read back: {read_text!r}")
        print("RESULT:", "PASS - structured UIA read/write confirmed" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        time.sleep(0.3)
        if app is not None:
            try:
                app.kill()
            except Exception:
                pass
        _kill_stray_notepads()


if __name__ == "__main__":
    sys.exit(main())
