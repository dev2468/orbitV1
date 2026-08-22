"""Tests for set-of-mark grounding (Phase 2).

Three things can break here without anyone noticing, and each has a test
below that is really about that failure rather than about the happy path:

  1. **The mark offset.** Candidates carry SCREEN coordinates; the overlay is
     drawn in IMAGE coordinates. Lose the crop-origin subtraction and every
     box is drawn a constant distance from the control it labels — the model
     then answers correctly about a mislabelled picture, which looks like a
     grounding failure and is not.
  2. **Confidence leakage.** A set-of-mark answer resolves to a REAL UIA
     rectangle, which is far more precise than a guessed point and is exactly
     the kind of thing that tempts someone to call it actuatable. It is not.
     It is still a guess about WHICH box, wearing accurate bounds.
  3. **Silent loss of the fallback.** The windows this tier exists for
     (games, <canvas>) produce no candidates at all. If set-of-mark ever
     became mandatory, the tier would break precisely where it is the only
     option.

`test_vision_sourced_element_ref_is_still_refused_by_actuation` lives in
test_perception_tools.py and is deliberately not touched; the set-of-mark
equivalent here is a second lock on the same door.
"""

from __future__ import annotations

import pytest

import orbit.mcp_servers.perception_tools as pt
from orbit import db
from orbit.mcp_servers.mark_overlay import RgbCanvas, draw_marks
from orbit.tools.element_ref import ElementRef


def _candidates(n: int = 4, origin=(100, 200)):
    """Candidates in SCREEN coordinates, as candidate_source produces them."""
    ox, oy = origin
    return [
        {
            "index": i + 1,
            "bounds": (ox + 10 + i * 100, oy + 20, ox + 90 + i * 100, oy + 60),
            "role": "Button",
            "name": f"Btn{i + 1}",
            "source": "uia",
        }
        for i in range(n)
    ]


def _patch_capture(monkeypatch, candidates):
    monkeypatch.setattr(pt, "_ensure_physical_screen_coords", lambda: None)
    monkeypatch.setattr(pt, "window_snapshot", lambda h: {
        "window_handle": h, "title": "Fixture", "process_id": 1,
        "process_name": "fixture.exe", "bounds": (100, 200, 900, 800),
    })
    monkeypatch.setattr(pt, "_window_bounds_to_region",
                        lambda b: {"left": 100, "top": 200, "width": 800, "height": 600})
    monkeypatch.setattr(pt, "_grab_png", lambda region: (
        b"\x89PNG\r\n\x1a\n-fake",
        {"left": 100, "top": 200, "width": 800, "height": 600}, (800, 600)))
    monkeypatch.setattr(pt, "_grab_raw_rgb", lambda region: b"\x00" * (800 * 600 * 3))
    monkeypatch.setattr(pt, "_fit_for_inline_upload", lambda png, rgb, w, h: (png, 1))
    monkeypatch.setattr(pt, "generate_candidates", lambda h: {
        "candidates": candidates, "source": "uia" if candidates else None,
        "uia_assessment": {"usable": bool(candidates)}, "fallback_error": None,
    })


# --- index parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "reply,expected",
    [
        ('{"index": 3}', 3),
        ("The answer is box 2.", 2),
        ("2", 2),
        ('```json\n{"index": 4}\n```', 4),
        ('{"box": 1}', 1),
    ],
)
def test_index_replies_parse_from_the_shapes_models_emit(reply, expected):
    assert pt._parse_index_reply(reply, {1, 2, 3, 4}) == expected


def test_out_of_range_index_is_rejected_not_clamped():
    """Same reasoning as _in_normalised_range: a nonsense answer turned into a
    real-looking box is carried by an ElementRef that looks as trustworthy as
    a good one."""
    assert pt._parse_index_reply('{"index": 97}', {1, 2, 3}) is None


def test_unparseable_index_reply_is_none():
    assert pt._parse_index_reply("I can't tell.", {1, 2, 3}) is None
    assert pt._parse_index_reply("", {1, 2, 3}) is None


# --- agreement summarisation ----------------------------------------------


def test_unanimous_samples():
    a = pt._summarize_agreement([2, 2, 2])
    assert a["chosen"] == 2 and a["agreement"] == "unanimous" and a["distinct"] == 1


def test_majority_samples():
    a = pt._summarize_agreement([2, 3, 2])
    assert a["chosen"] == 2 and a["agreement"] == "majority"


def test_fully_split_samples_are_labelled_split():
    """Three different answers on an identical image. The winner is arbitrary
    and the label has to say so, because this is the case where the model is
    least worth believing."""
    a = pt._summarize_agreement([1, 2, 3])
    assert a["agreement"] == "split"
    assert a["distinct"] == 3


def test_single_sample_is_not_called_unanimous():
    """One sample agreeing with itself is not evidence. Labelling it
    "unanimous" would overstate exactly the signal this exists to report."""
    a = pt._summarize_agreement([2])
    assert a["agreement"] == "single_sample"


def test_all_unparseable_samples_report_no_answer():
    a = pt._summarize_agreement([None, None, None])
    assert a["chosen"] is None and a["agreement"] == "no_answer"


def test_partial_failures_do_not_sink_a_clear_answer():
    """A provider hiccup on one of three samples should not discard the two
    that agreed."""
    a = pt._summarize_agreement([2, None, 2])
    assert a["chosen"] == 2 and a["agreement"] == "unanimous"


# --- overlay geometry ------------------------------------------------------


def test_draw_marks_returns_a_copy_and_does_not_mutate_the_original():
    """The unmarked buffer is still needed for the point fallback."""
    rgb = bytes(b"\x00" * (40 * 30 * 3))
    out = draw_marks(rgb, 40, 30, [{"index": 1, "bounds": (5, 5, 25, 20)}], (0, 0))
    assert rgb != out
    assert rgb == bytes(b"\x00" * (40 * 30 * 3))


def test_marks_are_drawn_at_image_coordinates_not_screen_coordinates():
    """THE offset test. A candidate at screen (110,220)-(190,260) inside a
    window whose origin is (100,200) must be drawn at image (10,20)-(90,60).
    Skip the subtraction and every mark labels the wrong control."""
    # The image is big enough that the UNtranslated position would also land
    # inside it — otherwise "nothing drawn there" proves nothing, it just
    # means the buffer ended.
    w, h = 300, 300
    rgb = bytes(b"\x00" * (w * h * 3))
    cand = [{"index": 1, "bounds": (110, 120, 190, 160)}]
    out = draw_marks(rgb, w, h, cand, (100, 100))

    def px(x, y):
        i = (y * w + x) * 3
        return (out[i], out[i + 1], out[i + 2])

    # Drawn at image-space (10,20)-(90,60): its top-left corner is inked.
    assert px(10, 20) != (0, 0, 0), "no mark drawn at the translated position"
    # The untranslated box would have started at (110,120). Its corner is
    # inside this image and must be untouched.
    assert px(110, 120) == (0, 0, 0), "a mark was drawn at raw screen coordinates"


def test_marks_against_the_top_edge_stay_inside_the_image():
    """A tag drawn off-image is an unreadable mark, and an unreadable mark is
    a candidate the model cannot choose."""
    w, h = 120, 90
    rgb = bytes(b"\x00" * (w * h * 3))
    out = draw_marks(rgb, w, h, [{"index": 7, "bounds": (0, 0, 60, 30)}], (0, 0))
    assert out != rgb


def test_rgb_canvas_clips_instead_of_raising():
    c = RgbCanvas(bytearray(b"\x00" * (10 * 10 * 3)), 10, 10)
    c.fill_rect((-50, -50, 500, 500), (1, 2, 3))
    c.stroke_rect((-5, -5, 15, 15), (4, 5, 6))
    assert len(c.buf) == 10 * 10 * 3


# --- the tool takes the set-of-mark path ----------------------------------


@pytest.mark.asyncio
async def test_set_of_mark_path_resolves_to_the_chosen_candidates_bounds(monkeypatch):
    caller = db.create_task("caller")
    cands = _candidates(4)
    _patch_capture(monkeypatch, cands)
    monkeypatch.setattr(
        pt.VisionLocateTool, "_call_vision_model",
        lambda self, b64, desc, prompt=pt._VISION_PROMPT: '{"index": 3}',
    )

    result = await pt.vision_locate_tool.execute(
        {"window_handle": 4242, "target_description": "the third button"}, task_id=caller
    )
    assert result.ok, result.error
    element = result.data["element"]
    assert tuple(element["bounds"]) == tuple(cands[2]["bounds"])
    assert element["state"]["vision"]["reply_format"] == "set_of_mark"
    assert element["state"]["vision"]["bounds_basis"].startswith("set-of-mark box 3")


@pytest.mark.asyncio
async def test_a_set_of_mark_result_is_still_refused_by_actuation(monkeypatch):
    """The second lock. A set-of-mark answer carries a REAL UIA rectangle, so
    it is far more precise than a guessed point — and that precision is
    exactly what makes it tempting to actuate. It must not be. Choosing the
    wrong box confidently is still choosing the wrong box."""
    import orbit.mcp_servers.windows_control_tools as wc_tools

    caller = db.create_task("caller")
    _patch_capture(monkeypatch, _candidates(4))
    monkeypatch.setattr(
        pt.VisionLocateTool, "_call_vision_model",
        lambda self, b64, desc, prompt=pt._VISION_PROMPT: '{"index": 2}',
    )

    result = await pt.vision_locate_tool.execute(
        {"window_handle": 4242, "target_description": "a button"}, task_id=caller
    )
    assert result.ok
    element = result.data["element"]
    assert element["confidence"] == pt.Confidence.VISION_INFERRED
    assert result.confidence == pt.Confidence.VISION_INFERRED

    resolved = wc_tools._resolve_click_target(element)
    with pytest.raises(wc_tools.ClassifiedToolError) as excinfo:
        wc_tools._require_confidence(resolved)
    assert excinfo.value.kind == "permission_denied"


@pytest.mark.asyncio
async def test_agreement_is_recorded_but_never_becomes_the_confidence(monkeypatch):
    """Unanimous agreement across every sample is the STRONGEST signal this
    tier can produce, and it still must not move the number the actuation
    gate reads. If any future change wires agreement into confidence, this
    is the test that fails."""
    caller = db.create_task("caller")
    _patch_capture(monkeypatch, _candidates(4))
    monkeypatch.setattr(
        pt.VisionLocateTool, "_call_vision_model",
        lambda self, b64, desc, prompt=pt._VISION_PROMPT: '{"index": 1, "confidence": 0.99}',
    )

    result = await pt.vision_locate_tool.execute(
        {"window_handle": 4242, "target_description": "a button"}, task_id=caller
    )
    vision_state = result.data["element"]["state"]["vision"]
    assert vision_state["agreement"]["agreement"] == "unanimous"
    assert vision_state["model_confidence"] == 0.99
    assert result.data["element"]["confidence"] == pt.Confidence.VISION_INFERRED


@pytest.mark.asyncio
async def test_the_model_is_sampled_the_configured_number_of_times(monkeypatch):
    from orbit.policy import load_perception_policy

    wanted = int(load_perception_policy()["vision"]["grounding_samples"])
    caller = db.create_task("caller")
    _patch_capture(monkeypatch, _candidates(4))
    calls = {"n": 0}

    def counting(self, b64, desc, prompt=pt._VISION_PROMPT):
        calls["n"] += 1
        return '{"index": 2}'

    monkeypatch.setattr(pt.VisionLocateTool, "_call_vision_model", counting)
    await pt.vision_locate_tool.execute(
        {"window_handle": 4242, "target_description": "a button"}, task_id=caller
    )
    assert calls["n"] == wanted


@pytest.mark.asyncio
async def test_the_set_of_mark_prompt_is_the_one_actually_sent(monkeypatch):
    """Guards against the prompt being changed in the constant but never
    reaching the model call."""
    caller = db.create_task("caller")
    _patch_capture(monkeypatch, _candidates(4))
    seen: list[str] = []

    def capture(self, b64, desc, prompt=pt._VISION_PROMPT):
        seen.append(prompt)
        return '{"index": 1}'

    monkeypatch.setattr(pt.VisionLocateTool, "_call_vision_model", capture)
    await pt.vision_locate_tool.execute(
        {"window_handle": 4242, "target_description": "a button"}, task_id=caller
    )
    assert all(p is pt._VISION_SOM_PROMPT for p in seen)


# --- the fallback that must not disappear ---------------------------------


@pytest.mark.asyncio
async def test_too_few_candidates_falls_back_to_the_point_prompt(monkeypatch):
    """The no-UIA case: a <canvas> app or a game yields nothing to number.
    That is the exact window this whole tier exists for, so it must still
    work — via the original freeform-point prompt."""
    caller = db.create_task("caller")
    _patch_capture(monkeypatch, [])
    seen: list[str] = []

    def capture(self, b64, desc, prompt=pt._VISION_PROMPT):
        seen.append(prompt)
        return '{"point": [500, 250]}'

    monkeypatch.setattr(pt.VisionLocateTool, "_call_vision_model", capture)
    result = await pt.vision_locate_tool.execute(
        {"window_handle": 4242, "target_description": "the red knob"}, task_id=caller
    )
    assert result.ok, result.error
    assert seen == [pt._VISION_PROMPT], "should have used the point prompt exactly once"
    assert result.data["element"]["state"]["vision"]["bounds_basis"].startswith("point widened")
    assert result.data["element"]["bounds"] == (280, 480, 320, 520)


@pytest.mark.asyncio
async def test_unusable_mark_answers_fall_back_to_the_point_prompt(monkeypatch):
    """Marks were drawn but every sample came back unparseable. The marked
    image failing tells us nothing about whether the plain one will, so the
    call falls through rather than failing."""
    caller = db.create_task("caller")
    _patch_capture(monkeypatch, _candidates(4))
    replies = {"n": 0}

    def flaky(self, b64, desc, prompt=pt._VISION_PROMPT):
        replies["n"] += 1
        if prompt is pt._VISION_SOM_PROMPT:
            return "I cannot determine which box."
        return '{"point": [500, 250]}'

    monkeypatch.setattr(pt.VisionLocateTool, "_call_vision_model", flaky)
    result = await pt.vision_locate_tool.execute(
        {"window_handle": 4242, "target_description": "a button"}, task_id=caller
    )
    assert result.ok, result.error
    state = result.data["element"]["state"]["vision"]
    assert state["reply_format"] == "point"
    assert state["agreement"]["fell_back_to_point"] is True


@pytest.mark.asyncio
async def test_candidate_generation_failure_does_not_break_the_vision_call(monkeypatch):
    """A vision call that would have worked must not start failing because a
    candidate source did."""
    caller = db.create_task("caller")
    _patch_capture(monkeypatch, [])

    def boom(_h):
        raise RuntimeError("UIA blew up")

    monkeypatch.setattr(pt, "generate_candidates", boom)
    monkeypatch.setattr(
        pt.VisionLocateTool, "_call_vision_model",
        lambda self, b64, desc, prompt=pt._VISION_PROMPT: '{"point": [500, 250]}',
    )
    result = await pt.vision_locate_tool.execute(
        {"window_handle": 4242, "target_description": "a knob"}, task_id=caller
    )
    assert result.ok, result.error
    assert "UIA blew up" in result.data["element"]["state"]["vision"][
        "candidate_fallback_error"
    ]
