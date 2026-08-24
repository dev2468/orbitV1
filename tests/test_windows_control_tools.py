"""Tests for the windows-control MCP server (Prompt 1): key-combo parsing
and the destructive-combo denylist, confidence gating on raw-coordinate
and vision-tier targets, tier registration, tool_filter exclusion of
windows_focus_window, and — the load-bearing correctness fix this build
required — that windows-control tools/instructions are only ever visible
to the agent when a task runs under lane='foreground'.

Deliberately hermetic: nothing here opens a real window, launches a real
process, or sends real keystrokes/clicks. Every test either exercises
pure-logic helpers directly, or calls a BaseTool whose confidence gate /
denylist check raises BEFORE any pywinauto/win32 call would fire — which
is itself the property under test in several cases (a blocked combo or a
sub-floor-confidence click never reaches the OS layer at all). A separate,
opt-in live-UI test file exercises the real UIA path against a real
Notepad window and is not part of this file.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import orbit.db as db
import orbit.mcp_servers.windows_control_tools as wc_tools


# --- key combo parsing / denylist -------------------------------------------


def test_parse_key_combo_simple_letter():
    assert wc_tools._parse_key_combo("s") == "s"


def test_parse_key_combo_ctrl_s():
    assert wc_tools._parse_key_combo("Ctrl+S") == "^s"


def test_parse_key_combo_named_key():
    assert wc_tools._parse_key_combo("Enter") == "{ENTER}"
    assert wc_tools._parse_key_combo("F5") == "{F5}"


def test_parse_key_combo_multiple_modifiers():
    assert wc_tools._parse_key_combo("Ctrl+Shift+Esc") == "^+{ESC}"


def test_parse_key_combo_win_key_is_unsupported():
    with pytest.raises(Exception) as exc_info:
        wc_tools._parse_key_combo("Win+L")
    assert exc_info.value.kind == "reasoning_failure"


def test_parse_key_combo_unrecognized_key_raises():
    with pytest.raises(Exception) as exc_info:
        wc_tools._parse_key_combo("Ctrl+Nonsense")
    assert exc_info.value.kind == "reasoning_failure"


def test_canonical_combo_normalizes_order_and_case():
    assert wc_tools._canonical_combo("Alt+F4") == wc_tools._canonical_combo("f4+alt")


def test_blocked_combos_match_regardless_of_case_or_order():
    for combo in ("Alt+F4", "alt+f4", "F4+Alt", "  ALT + F4  "):
        assert wc_tools._is_blocked_combo(combo)


def test_non_blocked_combo_is_allowed():
    assert not wc_tools._is_blocked_combo("Ctrl+S")


@pytest.mark.asyncio
async def test_key_tool_refuses_blocked_combo_without_touching_the_os():
    caller = db.create_task("caller")
    with patch.object(wc_tools.pywinauto_keyboard, "send_keys") as mock_send:
        result = await wc_tools.key_tool.execute({"key_combo": "Alt+F4"}, task_id=caller)
    assert result.ok is False
    assert result.error.kind == "permission_denied"
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_key_tool_sends_allowed_combo():
    caller = db.create_task("caller")
    with patch.object(wc_tools.pywinauto_keyboard, "send_keys") as mock_send:
        result = await wc_tools.key_tool.execute({"key_combo": "Ctrl+S"}, task_id=caller)
    assert result.ok
    mock_send.assert_called_once()
    assert mock_send.call_args.args[0] == "^s"


# --- confidence gating --------------------------------------------------------


def test_raw_coordinate_target_resolves_to_vision_tier():
    element = wc_tools._resolve_click_target({"x": 100, "y": 200})
    assert element.source == "vision"
    assert element.confidence == wc_tools.Confidence.VISION_INFERRED
    assert element.center() == (100, 200)


def test_pre_resolved_element_ref_is_used_as_is_not_re_resolved():
    """Contract 3: a target that already looks like an ElementRef (has
    bounds/source/confidence — e.g. straight out of a prior
    perception_find_element call) must be used directly rather than
    triggering a second UIA lookup."""
    with patch.object(wc_tools, "resolve_uia_element") as mock_resolve:
        element = wc_tools._resolve_click_target(
            {
                "element_id": "hwnd:123/{'auto_id': 'SaveButton'}",
                "role": "Button",
                "name": "Save",
                "bounds": (10, 20, 30, 40),
                "source": "uia",
                "confidence": wc_tools.Confidence.UIA_AUTOMATION_ID,
            }
        )
    mock_resolve.assert_not_called()
    assert element.source == "uia"
    assert element.confidence == wc_tools.Confidence.UIA_AUTOMATION_ID
    assert element.bounds == (10, 20, 30, 40)
    assert element.center() == (20, 30)


def test_require_confidence_blocks_below_floor():
    from orbit.tools.element_ref import ElementRef

    low_confidence = ElementRef(element_id="x", bounds=(0, 0, 1, 1), source="vision", confidence=0.5)
    with pytest.raises(Exception) as exc_info:
        wc_tools._require_confidence(low_confidence)
    assert exc_info.value.kind == "permission_denied"


def test_require_confidence_allows_uia_match():
    from orbit.tools.element_ref import ElementRef

    high_confidence = ElementRef(
        element_id="x", bounds=(0, 0, 1, 1), source="uia", confidence=wc_tools.Confidence.UIA_NAME_MATCH
    )
    wc_tools._require_confidence(high_confidence)  # must not raise


@pytest.mark.asyncio
async def test_click_tool_refuses_raw_coordinates_without_touching_the_os():
    caller = db.create_task("caller")
    with patch.object(wc_tools.pywinauto_mouse, "click") as mock_click:
        result = await wc_tools.click_tool.execute({"target": {"x": 10, "y": 10}}, task_id=caller)
    assert result.ok is False
    assert result.error.kind == "permission_denied"
    mock_click.assert_not_called()


@pytest.mark.asyncio
async def test_click_tool_target_missing_locator_is_reasoning_failure():
    caller = db.create_task("caller")
    result = await wc_tools.click_tool.execute(
        {"target": {"window_handle": 12345}}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "reasoning_failure"


@pytest.mark.asyncio
async def test_drag_tool_refuses_if_either_endpoint_is_low_confidence():
    caller = db.create_task("caller")
    with patch.object(wc_tools.pywinauto_mouse, "press") as mock_press:
        result = await wc_tools.drag_tool.execute(
            {"from_target": {"x": 1, "y": 1}, "to_target": {"x": 2, "y": 2}}, task_id=caller
        )
    assert result.ok is False
    assert result.error.kind == "permission_denied"
    mock_press.assert_not_called()


# --- windows_open_app (mocked launch — never actually opens anything) ------


@pytest.mark.asyncio
async def test_open_app_tool_calls_startfile_and_reports_the_target():
    caller = db.create_task("caller")
    with patch.object(wc_tools.os, "startfile") as mock_startfile:
        result = await wc_tools.open_app_tool.execute(
            {"app_name_or_path": "notepad"}, task_id=caller
        )
    assert result.ok
    assert result.data == {"launched": "notepad"}
    mock_startfile.assert_called_once_with("notepad")


@pytest.mark.asyncio
async def test_open_app_tool_surfaces_launch_failure_as_state_failure():
    caller = db.create_task("caller")
    with patch.object(wc_tools.os, "startfile", side_effect=OSError("not found")):
        result = await wc_tools.open_app_tool.execute(
            {"app_name_or_path": "definitely_not_a_real_app.exe"}, task_id=caller
        )
    assert result.ok is False
    assert result.error.kind == "state_failure"


# --- tiering / exposure -------------------------------------------------------


def test_windows_focus_window_is_registered_high_tier():
    from orbit.policy import load_risk_tiers

    tiers = load_risk_tiers()
    assert tiers.get("windows_focus_window") == "high"


def test_windows_focus_window_is_not_in_the_exposed_tool_filter():
    from orbit.skills import windows_control as windows_control_skill

    toolset = windows_control_skill.build_toolset(task_id="probe")
    assert "windows_focus_window" not in toolset.tool_filter
    assert "windows_get_foreground_window" in toolset.tool_filter


# --- lane gating (the correctness fix this build required) ------------------


def test_headless_agent_has_no_windows_control_tools_or_instructions():
    from orbit.agent import build_agent

    agent = build_agent(task_id="probe", lane="headless")
    server_args = [
        t.connection_params.server_params.args
        for t in agent.tools
        if hasattr(t, "connection_params")
    ]
    assert not any("windows_control_server" in str(args) for args in server_args)
    assert "windows_get_foreground_window" not in agent.instruction


def test_foreground_agent_has_windows_control_tools_and_instructions():
    from orbit.agent import build_agent

    agent = build_agent(task_id="probe", lane="foreground")
    server_args = [
        t.connection_params.server_params.args
        for t in agent.tools
        if hasattr(t, "connection_params")
    ]
    assert any("windows_control_server" in str(args) for args in server_args)
    assert "windows_get_foreground_window" in agent.instruction


# --- clipboard image tool ---------------------------------------------------


def _tiny_png() -> bytes:
    """Minimal valid 1x1 red PNG — no dependencies beyond stdlib."""
    import struct as s
    import zlib

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return s.pack(">I", len(data)) + c + s.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", s.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00\xff\x00\x00"  # filter=none + R G B
    idat = _chunk(b"IDAT", zlib.compress(raw))
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


@pytest.mark.asyncio
async def test_clipboard_copy_image_rejects_neither_arg():
    caller = db.create_task("caller")
    result = await wc_tools.clipboard_copy_image_tool.execute({}, task_id=caller)
    assert result.ok is False
    assert result.error.kind == "reasoning_failure"


@pytest.mark.asyncio
async def test_clipboard_copy_image_rejects_both_args():
    caller = db.create_task("caller")
    result = await wc_tools.clipboard_copy_image_tool.execute(
        {"image_path": "x.png", "image_base64": "abc"}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "reasoning_failure"


@pytest.mark.asyncio
async def test_clipboard_copy_image_rejects_nonexistent_file():
    caller = db.create_task("caller")
    result = await wc_tools.clipboard_copy_image_tool.execute(
        {"image_path": "C:\\definitely_not_a_real_file.png"}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "state_failure"


@pytest.mark.asyncio
async def test_clipboard_copy_image_from_file(tmp_path):
    caller = db.create_task("caller")
    png_file = tmp_path / "test.png"
    png_file.write_bytes(_tiny_png())

    result = await wc_tools.clipboard_copy_image_tool.execute(
        {"image_path": str(png_file)}, task_id=caller
    )
    assert result.ok
    assert result.data["copied"] is True
    assert result.data["width"] == 1
    assert result.data["height"] == 1


@pytest.mark.asyncio
async def test_clipboard_copy_image_from_base64():
    import base64

    caller = db.create_task("caller")
    b64 = base64.b64encode(_tiny_png()).decode()

    result = await wc_tools.clipboard_copy_image_tool.execute(
        {"image_base64": b64}, task_id=caller
    )
    assert result.ok
    assert result.data["width"] == 1
    assert result.data["height"] == 1


def test_clipboard_copy_image_is_registered_low_tier():
    from orbit.policy import load_risk_tiers

    tiers = load_risk_tiers()
    assert tiers.get("windows_clipboard_copy_image") == "low"


def test_clipboard_copy_image_is_in_the_tool_filter():
    from orbit.skills import windows_control as wc_skill

    toolset = wc_skill.build_toolset(task_id="probe")
    assert "windows_clipboard_copy_image" in toolset.tool_filter
