"""Tests for the screen-perception MCP server (Prompt 2).

perception_get_state/perception_capture_screenshot are exercised against
the REAL, current desktop — same "not all of this is hermetic, but safe
and read-only" category as test_browser_policy_tools.py's real-Chrome
round trip (tests/CLAUDE.md). Nothing here simulates input or opens a new
window, so unlike test_windows_control_live.py this needs no opt-in gate:
whatever happens to be in the foreground when this runs is fine to read.

perception_get_uia_tree's response-shaping (truncated flag) and
perception_find_element's tier bookkeeping are tested with the underlying
resolution mocked, so they don't depend on real window content.
"""

from __future__ import annotations

import base64
from unittest import mock

import pytest

import orbit.db as db
import orbit.mcp_servers.perception_tools as pt
from orbit.tools.element_ref import ElementRef

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_get_state_returns_foreground_window_shape():
    caller = db.create_task("caller")
    result = await pt.get_state_tool.execute({}, task_id=caller)
    assert result.ok
    fg = result.data["foreground_window"]
    assert isinstance(fg["window_handle"], int)
    assert isinstance(fg["title"], str)
    assert isinstance(fg["process_name"], str)
    assert result.data["task_status"] is None  # no task_id was asked about


@pytest.mark.asyncio
async def test_get_state_reports_status_of_a_given_task_id():
    caller = db.create_task("caller")
    other = db.create_task("some other task")
    db.update_task_status(other, "COMPLETED", result="done")

    result = await pt.get_state_tool.execute({"task_id": other}, task_id=caller)
    assert result.ok
    assert result.data["task_status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_capture_screenshot_returns_valid_png_bytes():
    caller = db.create_task("caller")
    result = await pt.capture_screenshot_tool.execute({}, task_id=caller)
    assert result.ok
    assert result.data["format"] == "png"
    raw = base64.b64decode(result.data["image_base64"])
    assert raw.startswith(_PNG_MAGIC)


@pytest.mark.asyncio
async def test_capture_screenshot_respects_explicit_region():
    caller = db.create_task("caller")
    region = {"left": 0, "top": 0, "width": 50, "height": 40}
    result = await pt.capture_screenshot_tool.execute({"region": region}, task_id=caller)
    assert result.ok
    assert result.data["region"] == region


@pytest.mark.asyncio
async def test_wait_for_visual_change_returns_shape_within_short_timeout():
    caller = db.create_task("caller")
    region = {"left": 0, "top": 0, "width": 10, "height": 10}
    result = await pt.wait_for_visual_change_tool.execute(
        {"region": region, "timeout": 0.5}, task_id=caller
    )
    assert result.ok
    assert "changed" in result.data
    assert isinstance(result.data["changed"], bool)


@pytest.mark.asyncio
async def test_get_uia_tree_reports_truncated_when_capped(monkeypatch):
    caller = db.create_task("caller")
    fake_nodes = [{"role": "Button", "name": f"n{i}", "automation_id": None, "bounds": None, "visible": True, "depth": 0} for i in range(3)]
    monkeypatch.setattr(pt, "get_uia_tree", lambda handle, max_depth, max_nodes: fake_nodes)

    result = await pt.get_uia_tree_tool.execute(
        {"window_handle": 999, "max_depth": 6, "max_nodes": 3}, task_id=caller
    )
    assert result.ok
    assert result.data["truncated"] is True
    assert len(result.data["nodes"]) == 3


@pytest.mark.asyncio
async def test_get_uia_tree_not_truncated_when_under_cap(monkeypatch):
    caller = db.create_task("caller")
    fake_nodes = [{"role": "Button", "name": "only-one", "automation_id": None, "bounds": None, "visible": True, "depth": 0}]
    monkeypatch.setattr(pt, "get_uia_tree", lambda handle, max_depth, max_nodes: fake_nodes)

    result = await pt.get_uia_tree_tool.execute(
        {"window_handle": 999, "max_depth": 6, "max_nodes": 200}, task_id=caller
    )
    assert result.ok
    assert result.data["truncated"] is False


@pytest.mark.asyncio
async def test_find_element_requires_a_locator():
    caller = db.create_task("caller")
    result = await pt.find_element_tool.execute(
        {"query": {"window_handle": 999}, "tier_order": None}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "reasoning_failure"


@pytest.mark.asyncio
async def test_find_element_reports_unavailable_tiers_and_returns_the_element(monkeypatch):
    caller = db.create_task("caller")
    fake_element = ElementRef(
        element_id="hwnd:999/{'auto_id': 'SaveButton'}",
        role="Button",
        name="Save",
        bounds=(1, 2, 3, 4),
        source="uia",
        confidence=pt.Confidence.UIA_AUTOMATION_ID,
    )
    monkeypatch.setattr(pt, "resolve_uia_element", lambda *a, **kw: fake_element)

    result = await pt.find_element_tool.execute(
        {
            "query": {"window_handle": 999, "automation_id": "SaveButton"},
            "tier_order": ["uia", "ocr", "vision"],
        },
        task_id=caller,
    )
    assert result.ok
    assert result.data["tiers_tried"] == ["uia"]
    assert result.data["tiers_unavailable"] == ["ocr", "vision"]
    assert result.data["element"]["element_id"] == fake_element.element_id
    assert result.confidence == pt.Confidence.UIA_AUTOMATION_ID


def test_perception_get_uia_tree_output_is_windows_click_compatible(monkeypatch):
    """The end-to-end contract this whole build exists for: an ElementRef
    perception_find_element returns must be directly consumable by
    windows_click's target — verified here by round-tripping ITS ACTUAL
    OUTPUT SHAPE through windows-control's _resolve_click_target, not by
    asserting the two servers happen to agree by inspection."""
    import orbit.mcp_servers.windows_control_tools as wc_tools

    fake_element = ElementRef(
        element_id="hwnd:999/{'auto_id': 'SaveButton'}",
        role="Button",
        name="Save",
        bounds=(1, 2, 3, 4),
        source="uia",
        confidence=pt.Confidence.UIA_AUTOMATION_ID,
    )
    perception_output = fake_element.model_dump()

    with mock.patch.object(wc_tools, "resolve_uia_element") as mock_resolve:
        resolved = wc_tools._resolve_click_target(perception_output)

    mock_resolve.assert_not_called()  # used directly, no second UIA lookup
    assert resolved.confidence == fake_element.confidence
    assert resolved.bounds == fake_element.bounds
    assert resolved.center() == (2, 3)
