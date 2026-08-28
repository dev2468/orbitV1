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
from orbit.tools.foundation import ClassifiedToolError

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
    # "vision" used to be reported unavailable here too. It is implemented
    # now (see this module's VISION TIER block), so only "ocr" remains
    # genuinely absent — that change is the point, not a slipped assertion.
    assert result.data["tiers_unavailable"] == ["ocr"]
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


# --- vision tier ------------------------------------------------------------
#
# The model call itself is always mocked here. What is NOT mocked is the
# coordinate arithmetic and the safety gate, because those are the two things
# that can be wrong in a way nobody notices: a crop-offset bug produces answers
# that are off by a small CONSTANT amount and still look plausible, and a
# confidence regression would silently open a path from a visual guess to a
# real mouse click.


def test_vision_point_to_screen_round_trips_exactly_through_crop_and_resize():
    """Deterministic forward transform -> inverse -> the original pixel.

    Values are chosen to divide evenly so 'exact' means exact, not
    'within a pixel': an off-by-the-crop-offset bug is precisely the kind
    that a tolerance-based assertion would wave through."""
    crop_origin = (137, 251)          # window's top-left on the virtual screen
    crop_w, crop_h = 800, 600         # window size in physical pixels
    scale_factor = 2                  # downscaled before upload
    sent_w, sent_h = crop_w // scale_factor, crop_h // scale_factor

    def forward(screen_x: int, screen_y: int) -> tuple[float, float]:
        """What the pipeline does TO the image, in the same order run() does:
        crop, then resize, then normalise to Gemma's 0-1000 [y, x]."""
        x_crop = screen_x - crop_origin[0]
        y_crop = screen_y - crop_origin[1]
        x_sent = x_crop / scale_factor
        y_sent = y_crop / scale_factor
        return (y_sent / sent_h * 1000.0, x_sent / sent_w * 1000.0)

    for screen_x, screen_y in [
        (137, 251),      # exact top-left corner of the crop
        (537, 551),      # middle
        (937, 851),      # bottom-right corner
        (237, 451),      # arbitrary interior point
    ]:
        y_norm, x_norm = forward(screen_x, screen_y)
        back_x, back_y = pt._vision_point_to_screen(
            y_norm, x_norm,
            sent_width=sent_w, sent_height=sent_h,
            scale_factor=scale_factor, crop_origin=crop_origin,
        )
        assert back_x == pytest.approx(screen_x, abs=1e-9), (screen_x, back_x)
        assert back_y == pytest.approx(screen_y, abs=1e-9), (screen_y, back_y)


def test_vision_point_to_screen_applies_crop_offset_not_just_scale():
    """A non-zero crop origin must actually move the answer. Guards the
    specific regression where the resize is reversed but the crop is not:
    every coordinate then comes back short by exactly the crop origin."""
    kwargs = dict(sent_width=100, sent_height=100, scale_factor=1)
    at_origin = pt._vision_point_to_screen(500, 500, crop_origin=(0, 0), **kwargs)
    offset = pt._vision_point_to_screen(500, 500, crop_origin=(300, 200), **kwargs)
    assert at_origin == (50.0, 50.0)
    assert offset == (350.0, 250.0)


def test_vision_reply_parser_handles_the_three_shapes_the_spike_actually_saw():
    """Bare JSON, fenced JSON, and a point wrapped in a sentence — all three
    came back from the real model during the grounding spike, so all three
    have to parse. Values stay in the model's own 0-1000 space."""
    bare = pt._parse_vision_reply('[{"point": [82, 967], "label": "gear"}]')
    assert bare["kind"] == "point" and bare["point"] == (82.0, 967.0)

    fenced = pt._parse_vision_reply('```json\n[{"point": [75, 26]}]\n```')
    assert fenced["kind"] == "point" and fenced["point"] == (75.0, 26.0)

    prose = pt._parse_vision_reply('The File menu is located at `{"point": [79, 30]}`.')
    assert prose["kind"] == "point" and prose["point"] == (79.0, 30.0)

    nothing = pt._parse_vision_reply("I cannot find that element in the screenshot.")
    assert nothing["kind"] is None


def test_vision_reply_parser_tolerates_the_malformed_key_the_spike_caught():
    """1 reply in 26 during the spike came back as `{"point: [73, 895],` —
    the key's closing quote misplaced, so the JSON is invalid even though
    the coordinates are perfectly good. Dropping that answer would be
    throwing away a correct location over one character."""
    parsed = pt._parse_vision_reply(
        '```json\n[\n  {"point: [73, 895],\n  label: "the search box"\n]\n```'
    )
    assert parsed["kind"] == "point"
    assert parsed["point"] == (73.0, 895.0)


def test_vision_reply_parser_rejects_coordinates_outside_the_normalised_range():
    """The spike caught this twice in 42 answered calls: a point whose y is
    several times the top of the 0-1000 range. Translated blindly it becomes
    a screen coordinate thousands of pixels off the display, wrapped in an
    ElementRef that looks exactly as trustworthy as a good one. It has to
    read as 'no result', not as a confident wrong answer."""
    assert pt._parse_vision_reply('[{"point": [7478, 237]}]')["kind"] is None
    assert pt._parse_vision_reply('[{"point": [500, -40]}]')["kind"] is None
    assert pt._parse_vision_reply('[{"box_2d": [10, 20, 30, 4000]}]')["kind"] is None
    # the boundaries themselves are legitimate
    assert pt._parse_vision_reply('[{"point": [0, 1000]}]')["kind"] == "point"


@pytest.mark.asyncio
async def test_vision_locate_reports_a_failure_rather_than_an_off_screen_guess(monkeypatch):
    caller = db.create_task("caller")
    monkeypatch.setattr(pt, "_ensure_physical_screen_coords", lambda: None)
    monkeypatch.setattr(pt, "window_snapshot", lambda h: {
        "window_handle": h, "title": "w", "process_id": 1, "process_name": "p",
        "bounds": (0, 0, 400, 400)})
    monkeypatch.setattr(pt, "_window_bounds_to_region",
                        lambda b: {"left": 0, "top": 0, "width": 400, "height": 400})
    monkeypatch.setattr(pt, "_grab_png", lambda region: (
        b"png", {"left": 0, "top": 0, "width": 400, "height": 400}, (400, 400)))
    monkeypatch.setattr(pt, "_grab_raw_rgb", lambda region: b"")
    monkeypatch.setattr(pt, "_fit_for_inline_upload", lambda png, rgb, w, h: (png, 1))
    monkeypatch.setattr(pt.VisionLocateTool, "_call_vision_model",
                        lambda self, i, d: '[{"point": [7478, 237]}]')

    result = await pt.vision_locate_tool.execute(
        {"target_description": "a thing", "window_handle": 1}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "tool_failure"


def test_vision_reply_parser_reads_box_2d_when_one_is_returned():
    parsed = pt._parse_vision_reply('[{"box_2d": [100, 200, 300, 400]}]')
    assert parsed["kind"] == "box_2d"
    assert parsed["box"] == (100.0, 200.0, 300.0, 400.0)


@pytest.mark.asyncio
async def test_vision_locate_returns_vision_sourced_element_ref(monkeypatch):
    """End to end through the tool with only the network call replaced."""
    caller = db.create_task("caller")

    monkeypatch.setattr(pt, "_ensure_physical_screen_coords", lambda: None)
    monkeypatch.setattr(pt, "window_snapshot", lambda h: {
        "window_handle": h, "title": "Nebula Mixer", "process_id": 1,
        "process_name": "chrome.exe", "bounds": (100, 200, 900, 800),
    })
    monkeypatch.setattr(pt, "_window_bounds_to_region",
                        lambda b: {"left": 100, "top": 200, "width": 800, "height": 600})
    monkeypatch.setattr(pt, "_grab_png", lambda region: (
        b"\x89PNG\r\n\x1a\n-fake", {"left": 100, "top": 200, "width": 800, "height": 600}, (800, 600)))
    monkeypatch.setattr(pt, "_grab_raw_rgb", lambda region: b"\x00" * (800 * 600 * 3))
    monkeypatch.setattr(pt, "_fit_for_inline_upload", lambda png, rgb, w, h: (png, 1))
    monkeypatch.setattr(
        pt.VisionLocateTool, "_call_vision_model",
        lambda self, image_b64, description: '[{"point": [500, 250]}]',
    )

    result = await pt.vision_locate_tool.execute(
        {"window_handle": 4242, "target_description": "the red record button"}, task_id=caller
    )
    assert result.ok, result.error
    element = result.data["element"]
    assert element["source"] == "vision"
    assert element["confidence"] == pt.Confidence.VISION_INFERRED
    # point [y=500, x=250] of 1000 over an 800x600 crop at origin (100, 200)
    # -> centre (100 + 200, 200 + 300) = (300, 500), widened to a 40px box
    assert element["bounds"] == (280, 480, 320, 520)
    assert element["state"]["vision"]["bounds_basis"].startswith("point widened")
    assert result.confidence == pt.Confidence.VISION_INFERRED


@pytest.mark.asyncio
async def test_vision_locate_never_promotes_the_models_own_confidence(monkeypatch):
    """The model volunteering "confidence": 0.99 must NOT become the
    ElementRef confidence the actuation gate reads. It is kept for
    debugging only. This is the whole reason that field is separate."""
    caller = db.create_task("caller")
    monkeypatch.setattr(pt, "_ensure_physical_screen_coords", lambda: None)
    monkeypatch.setattr(pt, "window_snapshot", lambda h: {
        "window_handle": h, "title": "w", "process_id": 1, "process_name": "p",
        "bounds": (0, 0, 400, 400)})
    monkeypatch.setattr(pt, "_window_bounds_to_region",
                        lambda b: {"left": 0, "top": 0, "width": 400, "height": 400})
    monkeypatch.setattr(pt, "_grab_png", lambda region: (
        b"png", {"left": 0, "top": 0, "width": 400, "height": 400}, (400, 400)))
    monkeypatch.setattr(pt, "_grab_raw_rgb", lambda region: b"")
    monkeypatch.setattr(pt, "_fit_for_inline_upload", lambda png, rgb, w, h: (png, 1))
    monkeypatch.setattr(
        pt.VisionLocateTool, "_call_vision_model",
        lambda self, image_b64, description: '[{"point": [500, 500], "confidence": 0.99}]',
    )

    result = await pt.vision_locate_tool.execute(
        {"target_description": "anything", "window_handle": 1}, task_id=caller
    )
    assert result.ok
    assert result.data["element"]["confidence"] == pt.Confidence.VISION_INFERRED == 0.50
    assert result.data["element"]["state"]["vision"]["model_confidence"] == 0.99


@pytest.mark.asyncio
async def test_vision_locate_requires_a_description():
    caller = db.create_task("caller")
    result = await pt.vision_locate_tool.execute(
        {"window_handle": 1, "target_description": "  "}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "reasoning_failure"


def test_fit_for_inline_upload_leaves_a_small_image_untouched():
    """Most window crops are well under NVIDIA's documented inline ceiling
    (the spike measured 47k-142k base64 chars for 9 of 10 shots) and must be
    sent at native resolution — a needless downscale would throw away the
    detail the model needs to tell small controls apart."""
    small_png = b"x" * 1000
    sent, factor = pt._fit_for_inline_upload(small_png, b"", 10, 10)
    assert sent is small_png
    assert factor == 1


def test_fit_for_inline_upload_downscales_only_when_over_the_documented_limit():
    """A 4x4 white image downscaled 2x is still a valid PNG, and the factor
    reported back is what the coordinate translation will reverse."""
    huge = b"x" * (pt._VISION_MAX_INLINE_B64_CHARS + 10)
    raw = bytes([255]) * (4 * 4 * 3)
    sent, factor = pt._fit_for_inline_upload(huge, raw, 4, 4)
    assert factor == 2
    assert sent.startswith(b"\x89PNG\r\n\x1a\n")


def test_box_downscale_averages_pixels_and_halves_dimensions():
    # 2x2 image, one white pixel and three black -> a single 1x1 pixel at 255/4
    raw = bytes([255, 255, 255,  0, 0, 0,
                 0, 0, 0,        0, 0, 0])
    out, w, h = pt._box_downscale(raw, 2, 2, 2)
    assert (w, h) == (1, 1)
    assert out == bytes([63, 63, 63])


# --- the safety invariant this whole feature must not punch through ---------


def test_vision_sourced_element_ref_is_still_refused_by_actuation():
    """THE test. A vision-tier ElementRef — exactly what
    perception_vision_locate returns — fed into windows-control's real
    target resolver must still be refused by the confidence gate.

    NOTE: raw {x, y} dicts now bypass the gate entirely (direct coordinate
    clicks). This test covers a DIFFERENT path: an already-resolved ElementRef
    with source='vision' and confidence=VISION_INFERRED, which arrives through
    _resolve_click_target's first branch (has bounds/source/confidence keys).
    That path still hits _require_confidence and must be refused.

    Asserted against the REAL windows_control_policy.yaml floor and the REAL
    _resolve_click_target, not a stand-in: the point is that no path exists
    from perception_vision_locate's output to a real mouse click without
    the user explicitly supplying {x, y} coordinates themselves."""
    import orbit.mcp_servers.windows_control_tools as wc_tools

    vision_element = ElementRef(
        element_id="vision:4242/the red record button",
        role=None,
        name="the red record button",
        bounds=(280, 480, 320, 520),
        state={"vision": {"model": "openrouter/google/gemma-3-27b-it"}},
        source="vision",
        confidence=pt.Confidence.VISION_INFERRED,
    )

    # goes through the same door perception's uia-tier output does...
    resolved = wc_tools._resolve_click_target(vision_element.model_dump())
    assert resolved.source == "vision"
    assert resolved.confidence == pt.Confidence.VISION_INFERRED

    # ...and is stopped there.
    with pytest.raises(wc_tools.ClassifiedToolError) as excinfo:
        wc_tools._require_confidence(resolved)
    assert excinfo.value.kind == "permission_denied"


def test_vision_confidence_stays_below_the_actuation_floor():
    """Belt and braces on the two constants whose relationship is the gate:
    if either drifts, the test above could pass while the invariant it
    describes quietly stopped holding."""
    from orbit.policy import load_windows_control_policy

    floor = load_windows_control_policy()["min_actuation_confidence"]
    assert pt.Confidence.VISION_INFERRED < floor


# --- tier_order wiring ------------------------------------------------------


@pytest.mark.asyncio
async def test_find_element_does_not_use_vision_unless_it_is_asked_to(monkeypatch):
    """Vision is opt-in. A uia miss with the default tier_order must NOT
    silently spend a model call — that decision is documented on
    FindElementTool and this is what holds it in place."""
    caller = db.create_task("caller")
    called = []
    monkeypatch.setattr(pt, "resolve_uia_element", mock.Mock(
        side_effect=ClassifiedToolError("state_failure", "no element matched")))
    monkeypatch.setattr(pt.VisionLocateTool, "_call_vision_model",
                        lambda self, i, d: called.append(d) or '[{"point": [1, 1]}]')

    result = await pt.find_element_tool.execute(
        {"query": {"window_handle": 999, "name": "Nope", "description": "a thing"},
         "tier_order": None},
        task_id=caller,
    )
    assert result.ok is False
    assert called == []


@pytest.mark.asyncio
async def test_find_element_falls_back_to_vision_when_explicitly_requested(monkeypatch):
    caller = db.create_task("caller")
    monkeypatch.setattr(pt, "resolve_uia_element", mock.Mock(
        side_effect=ClassifiedToolError("state_failure", "no element matched")))
    monkeypatch.setattr(pt, "_ensure_physical_screen_coords", lambda: None)
    monkeypatch.setattr(pt, "window_snapshot", lambda h: {
        "window_handle": h, "title": "w", "process_id": 1, "process_name": "p",
        "bounds": (0, 0, 400, 400)})
    monkeypatch.setattr(pt, "_window_bounds_to_region",
                        lambda b: {"left": 0, "top": 0, "width": 400, "height": 400})
    monkeypatch.setattr(pt, "_grab_png", lambda region: (
        b"png", {"left": 0, "top": 0, "width": 400, "height": 400}, (400, 400)))
    monkeypatch.setattr(pt, "_grab_raw_rgb", lambda region: b"")
    monkeypatch.setattr(pt, "_fit_for_inline_upload", lambda png, rgb, w, h: (png, 1))
    monkeypatch.setattr(pt.VisionLocateTool, "_call_vision_model",
                        lambda self, i, d: '[{"point": [250, 750]}]')

    result = await pt.find_element_tool.execute(
        {"query": {"window_handle": 999, "name": "Nope",
                   "description": "the red record button"},
         "tier_order": ["uia", "vision"]},
        task_id=caller,
    )
    assert result.ok, result.error
    assert result.data["tiers_tried"] == ["uia", "vision"]
    assert result.data["element"]["source"] == "vision"
    assert result.data["element"]["confidence"] == pt.Confidence.VISION_INFERRED


@pytest.mark.asyncio
async def test_find_element_still_reports_ocr_unavailable(monkeypatch):
    """OCR is genuinely unimplemented and must keep saying so. Marking it
    available would be exactly the false-completeness this codebase's config
    comments keep warning about — and it is the easiest thing to break while
    editing the tier loop for vision."""
    caller = db.create_task("caller")
    fake = ElementRef(element_id="e", role="Button", name="Save", bounds=(1, 2, 3, 4),
                      source="uia", confidence=pt.Confidence.UIA_AUTOMATION_ID)
    monkeypatch.setattr(pt, "resolve_uia_element", lambda *a, **kw: fake)

    result = await pt.find_element_tool.execute(
        {"query": {"window_handle": 999, "automation_id": "SaveButton"},
         "tier_order": ["uia", "ocr", "vision"]},
        task_id=caller,
    )
    assert result.ok
    assert result.data["tiers_tried"] == ["uia"]
    # vision is real now, so ONLY ocr is unavailable
    assert result.data["tiers_unavailable"] == ["ocr"]


@pytest.mark.asyncio
async def test_find_element_without_a_locator_still_fails_the_old_way():
    """Unchanged behaviour for the default tier_order — a caller that never
    opts into vision sees exactly the error it always saw."""
    caller = db.create_task("caller")
    result = await pt.find_element_tool.execute(
        {"query": {"window_handle": 999}, "tier_order": None}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "reasoning_failure"
