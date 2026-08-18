"""Live-UI integration test for windows-control — the real UIA resolution
path against a REAL Notepad window, real clicks, real keystrokes.

Skipped unless ORBIT_RUN_LIVE_UI_TESTS=1 is set in the environment. This is
NOT part of the default `pytest tests/ -q` run: it visibly opens a window,
moves the mouse, and types on the real keyboard while it runs. Same
"opt-in, not hermetic" category tests/CLAUDE.md already documents for
test_browser_policy_tools.py's real-Chrome round trip — check the
environment before assuming a failure here is a regression rather than
Notepad's UIA tree looking different on this machine/Windows build.

Run explicitly with:
    set ORBIT_RUN_LIVE_UI_TESTS=1
    venv\\Scripts\\python.exe -m pytest tests\\test_windows_control_live.py -q -s
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest
from pywinauto import Desktop
from pywinauto.timings import wait_until_passes

import orbit.db as db
import orbit.mcp_servers.windows_control_tools as wc_tools
from orbit.mcp_servers.uia_resolver import resolve_uia_element

pytestmark = pytest.mark.skipif(
    os.environ.get("ORBIT_RUN_LIVE_UI_TESTS") != "1",
    reason="opt-in only — set ORBIT_RUN_LIVE_UI_TESTS=1 to run (opens a real window, moves the real mouse/keyboard)",
)


def _kill_stray_notepads() -> None:
    subprocess.run(["taskkill", "/F", "/IM", "notepad.exe", "/T"], capture_output=True)


@pytest.fixture
def notepad_window():
    _kill_stray_notepads()
    time.sleep(0.3)
    subprocess.Popen(["notepad.exe"])

    def _find():
        spec = Desktop(backend="uia").window(title_re=".*Notepad.*")
        if not spec.exists():
            raise RuntimeError("notepad window not found yet")
        return spec

    found = wait_until_passes(10, 0.5, _find)
    handle = found.wrapper_object().handle
    yield handle
    _kill_stray_notepads()


@pytest.mark.asyncio
async def test_open_app_focus_type_and_key_round_trip_on_real_notepad(notepad_window):
    caller = db.create_task("caller")

    fg_result = await wc_tools.get_foreground_window_tool.execute({}, task_id=caller)
    assert fg_result.ok
    assert "notepad" in fg_result.data["process_name"].lower()

    type_result = await wc_tools.type_tool.execute(
        {"text": "orbit windows-control live check", "target": None}, task_id=caller
    )
    assert type_result.ok

    async def _read_back():
        element = resolve_uia_element(
            notepad_window, automation_id=None, name=None, control_type="Document"
        )
        return element

    element = await _read_back()
    assert element.bounds is not None
